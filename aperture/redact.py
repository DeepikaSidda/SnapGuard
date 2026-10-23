"""Painting over the regions the gate withheld.

Two modes, and the difference matters more than it looks.

`blur` is reversible in principle: a heavy Gaussian still carries some information, and
box-blurred text has been recovered before. It looks gentler, which is exactly why it is
the wrong default for a credential.

`block` paints solid and is not reversible. That is the default here.

Either way the redaction happens on *our* copy. The withheld pixels are never sent and
then filtered at the far end -- they do not leave the machine at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

Box = tuple[int, int, int, int]
Mode = Literal["block", "blur"]

# Readable against both dark and light screenshots, and obviously deliberate.
MARK_FILL = (18, 18, 22)
MARK_EDGE = (220, 64, 64)


@dataclass
class Redaction:
    """What was painted over, so it can be reported and counted."""

    image: Image.Image
    boxes: list[Box]
    mode: Mode

    @property
    def count(self) -> int:
        return len(self.boxes)

    def covered_pixels(self) -> int:
        """Pixels painted over, counted once each.

        This used to be a sum of box areas, which was fine while a redaction was two or
        three large rectangles and quietly wrong afterwards. Regions overlap by 6% by
        design -- so a line of text crossing a seam produces two bars that share pixels
        -- and the mesh turns one region into a dozen bars. Summing areas inflated the
        number, and the number in question is the one reported to a user who has already
        complained three times that too much of their screen is covered. Overstating it
        is not a safe direction to be wrong in.
        """
        if not self.boxes:
            return 0
        mask = np.zeros((self.image.height, self.image.width), dtype=bool)
        for left, top, right, bottom in self.boxes:
            if right > left and bottom > top:
                mask[top:bottom, left:right] = True
        return int(mask.sum())

    def fraction_of(self, original: Image.Image) -> float:
        total = original.width * original.height
        return self.covered_pixels() / total if total else 0.0


def redact(
    image: Image.Image,
    boxes: Sequence[Box],
    *,
    mode: Mode = "block",
    label: str | None = "REDACTED",
) -> Redaction:
    """Return a copy of `image` with `boxes` painted over.

    `label` is written into each box so a person looking at the result knows a tool did
    this deliberately, rather than wondering whether their screenshot is corrupted.
    """
    out = image.convert("RGB").copy()
    draw = ImageDraw.Draw(out)

    for box in boxes:
        left, top, right, bottom = _clamp(box, out.size)
        if right <= left or bottom <= top:
            continue

        if mode == "blur":
            region = out.crop((left, top, right, bottom))
            # Radius scaled to the region so small boxes are blurred as thoroughly as
            # large ones. Still not safe for text; see the module docstring.
            radius = max(8, min(right - left, bottom - top) // 6)
            out.paste(region.filter(ImageFilter.GaussianBlur(radius)), (left, top))
        else:
            draw.rectangle([left, top, right, bottom], fill=MARK_FILL)

        # Decoration scaled to the box. Since the mesh landed, a redaction is often a
        # dozen narrow bars rather than one rectangle, and a 3px border with a label
        # stamped in it would be more ink than the thing it surrounds -- and the border
        # is drawn INSIDE the box, so on a 14px-tall bar it would cover the blacked-out
        # pixels with a bright colour and draw the eye to exactly nothing.
        wide = right - left >= 140
        tall = bottom - top >= 30
        draw.rectangle(
            [left, top, right - 1, bottom - 1],
            outline=MARK_EDGE,
            width=3 if (wide and tall) else 1,
        )
        if label and wide and tall:
            draw.text((left + 10, top + 8), label, fill=MARK_EDGE)

    return Redaction(image=out, boxes=[_clamp(b, out.size) for b in boxes], mode=mode)


def resolve_boxes(
    flagged_tiles: Sequence[Box],
    precise: Sequence[Box],
) -> tuple[list[Box], int]:
    """Decide what to actually paint over, preferring precision to tiles.

    The naive version was `precise + flagged_tiles`, which blacked out the exact location
    of a secret *and* the whole quarter of the screen it sat in. On a real capture that
    destroyed a file tree, an editor pane and a lot of empty space along with four
    characters of credential -- the user's screenshot came back half gone.

    The obvious fix is wrong, and it was tried: "a flagged tile containing a precise box
    is redacted at that box only". It produced a beautiful screenshot with three
    credentials still legible. The scanner had matched one of four secrets in that tile
    -- two wrapped across lines so the patterns missed them, and a Stripe key was shorter
    than its pattern required -- and narrowing to the one hit left the other three
    exposed.

    The model's answer is about the TILE. `credential 0.824` means "there is a credential
    in this region", not "there is exactly one and it is here". A precise hit is evidence
    of *a* secret, never evidence that it is the only one.

    So the boxes are the UNION:

      * every flagged tile is redacted whole, because that is the only claim the model
        actually made;
      * every precise box is redacted too, including ones outside any flagged tile --
        the scanner found something the model missed, which is the case the two lanes
        exist to cover for each other.

    Over-redaction is the safe failure here. The fixes for covering too much are a finer
    grid, so the tile is smaller, and `mesh.tighten_all`, which drops the blank parts of
    a tile. Neither one trusts a localiser that only sees patterns it has been taught.

    Returns the boxes to paint, and how many precise hits refined a flagged tile.
    """
    resolved: list[Box] = list(flagged_tiles)
    refined = 0
    for box in precise:
        resolved.append(box)
        if any(_contained(box, tile) for tile in flagged_tiles):
            refined += 1
    return resolved, refined


def _contained(box: Box, tile: Box, min_overlap: float = 0.5) -> bool:
    """Does `box` sit mostly inside `tile`?

    Not strict containment: OCR boxes are padded and tiles overlap slightly, so a box
    straddling a seam by a few pixels should still count as belonging to the tile.
    """
    left = max(box[0], tile[0])
    top = max(box[1], tile[1])
    right = min(box[2], tile[2])
    bottom = min(box[3], tile[3])
    if right <= left or bottom <= top:
        return False
    overlap = (right - left) * (bottom - top)
    area = (box[2] - box[0]) * (box[3] - box[1])
    return bool(area) and overlap / area >= min_overlap


def hold_placeholder(size: tuple[int, int], message: str) -> Image.Image:
    """The image that sits on the clipboard while the gate is still thinking.

    This is the fail-closed half of the design: the moment an image appears we replace
    it, so a paste during the inspection window yields this rather than the original. On
    a CPU the inspection takes seconds, which is long enough to matter.
    """
    width, height = max(size[0], 480), max(size[1], 160)
    img = Image.new("RGB", (width, height), (24, 24, 28))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width - 1, height - 1], outline=MARK_EDGE, width=4)
    draw.text((28, height // 2 - 18), "APERTURE", fill=MARK_EDGE)
    draw.text((28, height // 2 + 2), message, fill=(230, 230, 238))
    return img


def _clamp(box: Box, size: tuple[int, int]) -> Box:
    width, height = size
    left, top, right, bottom = box
    return (
        max(0, min(int(left), width)),
        max(0, min(int(top), height)),
        max(0, min(int(right), width)),
        max(0, min(int(bottom), height)),
    )
