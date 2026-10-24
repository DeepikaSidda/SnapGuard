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

# 4x3, wider than it is tall on purpose: screens are 16:9, so more columns than rows
# gives roughly square tiles rather than the letterbox strips a square grid would cut.
#
# This was 6x4 for a day, chosen when a flagged region WAS the redaction, so small
# regions meant small redactions. `mesh.tighten_all` removed that reason -- it reduces a
# flagged region to the cells holding ink, at pixel resolution -- and the grid stopped
# controlling tightness without anybody noticing. What it still controls is the request
# count, which is the entire latency bill, and a user asked twice for the scan to be
# faster. Measured, with the mesh on, on two real captures:
#
#            light capture            dark editor
#   grid     calls  time  covered     calls  time  covered
#   2x2          6   65s    8.73%         6   73s    3.26%
#   3x2          7   88s    8.72%         7  103s    3.25%
#   4x3          8  121s    2.38%        11  192s    3.37%     <- this
#   6x4         15  201s    1.65%        18  216s    3.38%
#
# On the dark editor coverage is flat across every grid -- 3.25% to 3.38% -- while time
# triples, which is the mesh doing all the work and the grid buying nothing. The light
# capture is the only case where a finer grid helps, because a coarse region there traps
# prose that the local OCR is too weak to whitelist, and 8.7% of a screen was the
# original complaint.
#
# So 4x3: 40% faster than 6x4 on the case a user actually reported, at 2.38% against
# 1.65%, and no worse anywhere measured. `--columns 2 --rows 2` is three times faster
# again if you can live with the light-page number.
#
# Two things that still follow from tiles this size:
#
#   Nothing is thrown away. A 4x3 tile of a 1080p screen is 480x360, which the server
#   downscales 1.1x -- effectively nothing, against 2.1x for a 2x2 grid. Past about 3x3
#   the grid stops being a cost/accuracy trade and becomes free accuracy; see section 13.
#
#   A hit is bounded. One of 12 regions is 8% of the display, and the mesh then reduces
#   that to the parts of it holding ink.
#
# The cost is requests, and less than it looks: the coarse pass screens four regions per
# request, so 12 regions is three requests, and the fine pass only runs on the batches
# that come back hot. A clean screen is 4 calls.
#
# See research/README.md sections 13 and 18 for the full history -- the earlier
# measurement that moved this from 2x2, and the later one that moved it back down.
DEFAULT_COLUMNS = 6
DEFAULT_ROWS = 4

# Tiles overlap slightly so a line of text sitting on a seam is whole in one of them.
DEFAULT_OVERLAP = 0.06

# No tile shorter than this on either side, whatever grid was asked for.
#
# The grid above is sized for a screen. Applied literally to a small image it is absurd:
# a 320x200 clipboard crop cut 6x4 gives 53x50 tiles, which cost 6 coarse requests plus
# a fine pass over anything hot -- four minutes, on an image the model could have judged
# whole in twelve seconds. That happened, on a real clipboard, while a user was waiting
# to test, and their screenshot sat queued behind it.
#
# 140 because the 6x4 grid measured on a 1024x576 screen produces 180x152 tiles and that
# configuration is the one with evidence behind it; the floor has to sit below it. A
# 320x200 image comes out 2x1, a 1024x576 stays 6x4, a 1920x1080 stays 6x4.
MIN_TILE_SIDE = 140


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


def fit_grid(
    size: tuple[int, int],
    columns: int,
    rows: int,
    *,
    min_side: int = MIN_TILE_SIDE,
) -> tuple[int, int]:
    """Reduce a grid so no tile ends up shorter than `min_side` on either side.

    A grid is a request, not an instruction. Cutting a thumbnail into 24 regions costs
    24 regions' worth of requests and tells you nothing a single look would not have.
    """
    width, height = size
    return (
        max(1, min(columns, width // min_side or 1)),
        max(1, min(rows, height // min_side or 1)),
    )


def tile_grid(
    image: Image.Image,
    *,
    columns: int = DEFAULT_COLUMNS,
    rows: int = DEFAULT_ROWS,
    overlap: float = DEFAULT_OVERLAP,
) -> list[Tile]:
    """Cut `image` into a grid of overlapping tiles, numbered left-to-right, top-down.

    The grid is reduced to fit small images -- see `fit_grid`. Doing it here rather than
    at the call sites is deliberate: the gate, the overlay and the agent's buffer all
    build their tiles through this function, and they have to agree. They did not once,
    and the result was an explain figure with the credentials still legible in it.
    """
    if columns < 1 or rows < 1:
        raise ValueError("columns and rows must both be at least 1")
    if not 0.0 <= overlap < 0.5:
        raise ValueError("overlap must be in [0, 0.5)")

    columns, rows = fit_grid(image.size, columns, rows)
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
