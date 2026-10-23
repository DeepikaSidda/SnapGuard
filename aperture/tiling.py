"""Cutting a screenshot into tiles, and why it is not optional.

The server scales every image so its longest side is at most 448 px. A 1920x1080
screenshot therefore arrives at 448x252 -- a 4.3x reduction, at which small text is a
grey smear. Measured on a drawn screenshot containing a fake AWS key:

    full 1920x1080 frame   credential_visible = 0.643   (weak, barely above chance)
    cropped 1000x360 region credential_visible = 0.884   (confident, correct)
    control crop, code but no secret               0.186   (confident, correct)

Cutting the frame into a grid before asking costs nothing and buys back the detail. A
6x4 grid of a 1920x1080 screen gives 320x270 tiles, which arrive untouched: the server
downscales nothing, because they are already inside 448px.

Tiles also give the gate something the whole frame cannot: *region-level* verdicts. One
corner of your screen can be withheld while the rest is sent, which is the difference
between "we blocked your screenshot" and "we sent you the part you needed".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Sequence

from PIL import Image

from .decider import IMAGE_LONG_SIDE, MAX_IMAGES_PER_REQUEST

# 6x4 is the default, and it is wider than it is tall on purpose: screens are 16:9, so
# six columns by four rows gives roughly square tiles (320x270 on a 1080p display)
# rather than the letterbox strips a square grid would cut. Two things follow from
# tiles that size:
#
#   Nothing is thrown away. 320x270 is already under the server's 448px long side, so
#   the tile arrives at full resolution -- downscale 1.0x, against 2.1x for a 2x2 grid.
#
#   A hit is small. One of 24 regions is 4% of the display, so covering a credential
#   costs a thumbnail-sized box instead of a quarter of the screen.
#
# The cost is requests, and less than it looks: the coarse pass screens four regions
# per request, so 24 regions is six requests, and the fine pass only runs on the
# batches that come back hot. Measured against the alternatives on the case a user
# reported, with the covered area as the thing to minimise:
#
#   2x2 + refinement   24% covered   11 calls      <- the previous default
#   3x3                26% covered    7 calls
#   4x4                22% covered    9 calls
#   6x4                15% covered   11 calls      <- this
#
# Same request count as the old default, a third less of the screen covered, and no
# leak in any of them. See research/README.md section 13.
DEFAULT_COLUMNS = 6
DEFAULT_ROWS = 4

# Tiles overlap slightly so a line of text sitting on a seam is whole in one of them.
DEFAULT_OVERLAP = 0.06


@dataclass
class Tile:
    """One region of a screenshot, with the coordinates to put it back."""

    index: int
    row: int
    column: int
    box: tuple[int, int, int, int]  # left, top, right, bottom in the original image
    image: Image.Image

    @property
    def width(self) -> int:
        return self.box[2] - self.box[0]

    @property
    def height(self) -> int:
        return self.box[3] - self.box[1]

    @property
    def downscale(self) -> float:
        """How much detail the server will throw away for this tile. Lower is better."""
        return max(self.width, self.height) / IMAGE_LONG_SIDE

    @property
    def pixels(self) -> int:
        return self.width * self.height

    def __str__(self) -> str:
        return f"tile {self.index} (r{self.row}c{self.column}, {self.width}x{self.height})"


def tile_grid(
    image: Image.Image,
    *,
    columns: int = DEFAULT_COLUMNS,
    rows: int = DEFAULT_ROWS,
    overlap: float = DEFAULT_OVERLAP,
) -> list[Tile]:
    """Cut `image` into a grid of overlapping tiles, numbered left-to-right, top-down."""
    if columns < 1 or rows < 1:
        raise ValueError("columns and rows must both be at least 1")
    if not 0.0 <= overlap < 0.5:
        raise ValueError("overlap must be in [0, 0.5)")

    width, height = image.size
    step_x, step_y = width / columns, height / rows
    pad_x, pad_y = step_x * overlap, step_y * overlap

    tiles: list[Tile] = []
    for row in range(rows):
        for column in range(columns):
            left = max(0, int(column * step_x - pad_x))
            top = max(0, int(row * step_y - pad_y))
            right = min(width, int((column + 1) * step_x + pad_x))
            bottom = min(height, int((row + 1) * step_y + pad_y))
            box = (left, top, right, bottom)
            tiles.append(
                Tile(
                    index=len(tiles),
                    row=row,
                    column=column,
                    box=box,
                    image=image.crop(box),
                )
            )
    return tiles


def batched(
    tiles: Sequence[Tile], size: int = MAX_IMAGES_PER_REQUEST
) -> Iterator[list[Tile]]:
    """Group tiles into request-sized batches.

    The server takes at most four images per request, and the state and images are
    encoded once per request, so filling each batch is strictly cheaper than not.
    """
    if size < 1:
        raise ValueError("batch size must be at least 1")
    for start in range(0, len(tiles), size):
        yield list(tiles[start : start + size])


def whole_frame(image: Image.Image) -> Tile:
    """The entire image as a single tile, for the context questions.

    Some judgements are about the screen as a whole rather than any region of it -- "is
    this a banking page", "is this a private conversation". Those were measured at 0.98
    and 0.99 on the full frame, so they do not need the grid, and asking them once is
    cheaper than asking them nine times.
    """
    width, height = image.size
    return Tile(
        index=-1,
        row=-1,
        column=-1,
        box=(0, 0, width, height),
        image=image,
    )
