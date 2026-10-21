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
        return sum((r - l) * (b - t) for l, t, r, b in self.boxes)

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

        draw.rectangle([left, top, right - 1, bottom - 1], outline=MARK_EDGE, width=3)
        if label:
            draw.text((left + 10, top + 8), label, fill=MARK_EDGE)

    return Redaction(image=out, boxes=[_clamp(b, out.size) for b in boxes], mode=mode)


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
