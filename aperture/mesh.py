"""Shrinking a flagged region to the cells that actually have something in them.

A user reported the same thing four times: the redaction covers too much of the screen.
Three rounds of fixes all worked on the same axis -- make the unit the model judges
smaller -- and each one helped and none of them finished the job:

    whole frame                56% of the reported screen covered
    2x2 grid + subdivide       24%
    6x4 grid                   15%

The fourth report came with a picture of graph paper, and the suggestion that the thing
should work like that: a fine mesh, and you fill in the squares that matter.

That is right, and it cannot be done by asking the model about squares. A graph-paper
mesh over a 1080p screen is several hundred cells, and a local decision costs ~12s on
this CPU, so that road ends at an hour per screenshot. The way to get there is to stop
asking the model for something it was never answering:

    THE MODEL'S ANSWER IS ABOUT A REGION.  "credential 0.915 on region 3" means there is
    a credential somewhere in region 3. It is not a location, and this project has
    already been burned once for treating a locator's output as if it bounded the
    secret: narrowing a flagged region to the regex scanner's matched box produced a
    screenshot with three credentials still legible, because the scanner had matched one
    secret of four. See `redact.resolve_boxes`.

    BUT WHERE THE PIXELS ARE IS NOT A JUDGEMENT.  It is arithmetic. Within a region the
    model flagged, the squares holding ink can be found exactly, for free, with no model
    and no OCR and nothing that can be fooled by an unfamiliar key format.

So: mesh the flagged region into small squares, measure each square, and paint the ones
that are not blank. The claim being made is still "everything in this region goes" --
every square with any content in it is covered -- it is just that the empty half of a
code pane is no longer part of "everything".

Why this is not the mistake that was reverted:

    narrowing to a regex hit   covers the secrets the scanner RECOGNISED
    narrowing to inked cells   covers every pixel in the region that is not background

The first is a guess about which text is sensitive. The second makes no guess about text
at all. A secret cannot hide in the blank space between lines, in an unusual format, or
wrapped across a line break, because none of those are blank.

Failure modes, and all three fail safe:

    a photo, a face, a gradient   high variation everywhere, so the cell is covered
    anti-aliased glyph edges      every inked cell is dilated outward by one cell
    the region is dense           above `max_fraction` inked, the whole box is used
"""

from __future__ import annotations

from typing import Sequence

import numpy as np
from PIL import Image

Box = tuple[int, int, int, int]

# 14px squares. Text in a screenshot is 11-16px tall, so a cell is about one character:
# fine enough that a single line of credentials costs a bar rather than a block, coarse
# enough that a flagged region yields tens of boxes and not thousands.
DEFAULT_CELL = 14

# A cell counts as inked above EITHER of these. Two measures rather than one because
# they fail differently: standard deviation catches texture and dense glyphs, peak-to-
# peak catches a single thin stroke on a flat background, which barely moves the
# deviation of a 14x14 patch. Calibrated against measured screenshots:
#
#   flat editor background      std 0.0-0.4   ptp  0-2
#   subtle panel gradient       std 0.9-2.1   ptp  3-9
#   one grey sidebar glyph      std 7.2       ptp 71
#   body text                   std 20-60     ptp 90-200
#
# The thresholds sit in the gap, close to the background end, because the cost of a
# false positive here is 196 extra covered pixels and the cost of a false negative is a
# legible character.
STD_FLOOR = 3.0
PTP_FLOOR = 24.0

# Above this inked fraction, the mesh is not buying anything and the region is painted
# whole: tidier to look at, and a dense region is exactly where a mesh is most likely to
# leave an island of background between two covered runs.
MAX_FRACTION = 0.72


def ink_mask(
    image: Image.Image,
    box: Box,
    *,
    cell: int = DEFAULT_CELL,
) -> tuple[np.ndarray, int, int]:
    """Which `cell`-sized squares of `box` contain anything other than background.

    Returns the boolean mask and its shape, in cells. Dilated by one cell in every
    direction, so a glyph whose anti-aliased edge spills into the next square does not
    leave a sliver of itself visible.
    """
    left, top, right, bottom = box
    crop = image.convert("L").crop((left, top, right, bottom))
    width, height = crop.size
    if width < 1 or height < 1:
        return np.zeros((0, 0), dtype=bool), 0, 0

    columns = max(1, -(-width // cell))  # ceil
    rows = max(1, -(-height // cell))

    # Pad to a whole number of cells with the edge value, so the reshape is exact and
    # the padding cannot invent contrast at the border.
    padded = np.pad(
        np.asarray(crop, dtype=np.float32),
        ((0, rows * cell - height), (0, columns * cell - width)),
        mode="edge",
    )
    blocks = padded.reshape(rows, cell, columns, cell).transpose(0, 2, 1, 3)
    flat = blocks.reshape(rows, columns, cell * cell)

    std = flat.std(axis=2)
    ptp = flat.max(axis=2) - flat.min(axis=2)
    inked = (std >= STD_FLOOR) | (ptp >= PTP_FLOOR)

    return _dilate(inked), rows, columns


def _dilate(mask: np.ndarray) -> np.ndarray:
    """Grow the mask by one cell in all eight directions."""
    if mask.size == 0:
        return mask
    out = mask.copy()
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            out |= np.roll(np.roll(mask, dy, axis=0), dx, axis=1) & _keep(mask, dy, dx)
    return out


def _keep(mask: np.ndarray, dy: int, dx: int) -> np.ndarray:
    """A guard so `np.roll` cannot wrap content from one edge round to the other."""
    keep = np.ones_like(mask)
    if dy > 0:
        keep[:dy, :] = False
    elif dy < 0:
        keep[dy:, :] = False
    if dx > 0:
        keep[:, :dx] = False
    elif dx < 0:
        keep[:, dx:] = False
    return keep


def tighten(
    image: Image.Image,
    box: Box,
    *,
    cell: int = DEFAULT_CELL,
    max_fraction: float = MAX_FRACTION,
) -> list[Box]:
    """Replace one flagged region with the parts of it that are not blank.

    Returns `[box]` unchanged when meshing would not help -- a dense region, or an
    empty one, or a region too small to mesh. Never returns less than the ink.
    """
    left, top, right, bottom = box
    if right - left < cell * 2 or bottom - top < cell * 2:
        return [box]

    mask, rows, columns = ink_mask(image, box, cell=cell)
    if mask.size == 0:
        return [box]

    inked = int(mask.sum())
    if inked == 0 or inked / mask.size > max_fraction:
        # Nothing found, or nearly everything found. Either way the honest answer is the
        # region the model actually judged.
        return [box]

    # Merge runs of inked cells along each row. Row-runs rather than arbitrary
    # rectangles because text is horizontal: a line of code becomes one bar, which is
    # both the smallest useful description and the one that looks deliberate.
    boxes: list[Box] = []
    for row in range(rows):
        start = None
        for column in range(columns + 1):
            lit = column < columns and bool(mask[row, column])
            if lit and start is None:
                start = column
            elif not lit and start is not None:
                boxes.append(
                    (
                        left + start * cell,
                        top + row * cell,
                        min(right, left + column * cell),
                        min(bottom, top + (row + 1) * cell),
                    )
                )
                start = None

    return _merge_vertically(boxes) or [box]


def _merge_vertically(boxes: Sequence[Box]) -> list[Box]:
    """Stack bars that share both edges, so a paragraph is one box and not twelve rows."""
    merged: list[Box] = []
    for box in sorted(boxes, key=lambda b: (b[0], b[2], b[1])):
        if merged:
            last = merged[-1]
            if last[0] == box[0] and last[2] == box[2] and last[3] >= box[1]:
                merged[-1] = (last[0], last[1], last[2], max(last[3], box[3]))
                continue
        merged.append(box)
    return merged


def join_adjacent(boxes: Sequence[Box], *, overlap: float = 0.6) -> list[Box]:
    """Fuse bars that meet, so a line of text crossing a region seam is one box.

    Regions overlap by 6% by design, and a line of credentials wide enough to span two
    of them comes back as two bars with a shared edge and a duplicated middle. Left
    alone that is only cosmetic -- the pixels are covered either way -- but it draws two
    borders and two labels through the middle of one redaction, which looks like a bug.

    Only boxes that already touch are fused, and the result always contains both, so
    this can only ever cover more. `overlap` is how much of the shorter box's height
    has to be shared before two bars are considered the same line.
    """
    out = [tuple(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        for i in range(len(out)):
            for j in range(i + 1, len(out)):
                fused = _fuse(out[i], out[j], overlap)
                if fused is not None:
                    out[i] = fused
                    del out[j]
                    changed = True
                    break
            if changed:
                break
    return [tuple(b) for b in out]  # type: ignore[misc]


def _fuse(a: Box, b: Box, overlap: float) -> Box | None:
    """The union of `a` and `b` if they are the same line of content, else None."""
    shared = min(a[3], b[3]) - max(a[1], b[1])
    shorter = min(a[3] - a[1], b[3] - b[1])
    if shorter <= 0 or shared / shorter < overlap:
        return None
    # Horizontally touching or overlapping. One cell of slack, so two bars separated by
    # a single blank square still join rather than leaving a hairline of screen showing
    # between two redactions.
    if min(a[2], b[2]) + DEFAULT_CELL < max(a[0], b[0]):
        return None
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def tighten_all(
    image: Image.Image,
    boxes: Sequence[Box],
    *,
    cell: int = DEFAULT_CELL,
    enabled: bool = True,
) -> tuple[list[Box], float]:
    """Mesh every flagged region, and report how much of the original area survived.

    The ratio is worth logging: it is the one number that tells a user whether the thing
    they complained about actually got better on their screen rather than on a fixture.
    """
    if not enabled or not boxes:
        return list(boxes), 1.0

    out: list[Box] = []
    for box in boxes:
        out.extend(tighten(image, box, cell=cell))
    out = join_adjacent(out)

    # Union area, not a sum of areas: the regions this started from overlap by 6%, so
    # summing would compare an inflated "after" against an inflated "before" and the
    # ratio reported to the user would be fiction.
    before = _union_area(boxes, image.size)
    after = _union_area(out, image.size)
    return out, (after / before if before else 1.0)


def _union_area(boxes: Sequence[Box], size: tuple[int, int]) -> int:
    if not boxes:
        return 0
    width, height = size
    mask = np.zeros((height, width), dtype=bool)
    for left, top, right, bottom in boxes:
        left, top = max(0, int(left)), max(0, int(top))
        right, bottom = min(width, int(right)), min(height, int(bottom))
        if right > left and bottom > top:
            mask[top:bottom, left:right] = True
    return int(mask.sum())
