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

# 6px squares, and this was 14 until it was measured.
#
# 14 was reasoned rather than measured: screenshot text is 11-16px tall, so a cell is
# about one character. That holds at the scale it was chosen for and breaks at smaller
# ones. The same page captured at 1024x472 instead of 1805x832 has ~8px text, so a 14px
# cell spans a line plus its leading, dilating by one cell merges adjacent lines, and the
# mesh kept 82% of the flagged area -- barely better than doing nothing.
#
# Swept across three real captures at three scales, holding the model's flagged regions
# fixed. The number is the fraction of the frame still covered:
#
#   cell   1024x472 light   1805x832 light   1920x1080 dark   boxes
#      4            5.8%           21.2% !            1.8%   1-34
#      6            8.8%            6.7%             2.4%   12-25
#      8            9.6%            9.7%             3.1%   14-20
#     10           11.5%            8.4%             4.5%    5-17
#     14           21.5%           12.5%             4.5%    3-9
#
# No leak at any size, which is expected: the gradient test runs per pixel, so cell size
# changes how tightly ink is boxed, not whether it is found.
#
# 6 is best or near-best at every scale. 4 is better on two and catastrophic on the
# third -- that 21.2% is the dense-region ceiling tripping, because dilating by 4px
# around every antialiased edge on a busy light page covers more than 72% of the cells,
# so it gave up and painted rectangles. Small cells make the ceiling easier to hit, and 6
# sits below that cliff on all three.
#
# A scaling rule keyed to image size was the obvious alternative and is not needed: a
# single value wins across a 2.3x range of capture sizes, and a fixed number has no
# heuristic to be wrong.
DEFAULT_CELL = 6

# A cell counts as inked above EITHER of these.
#
# The measure is the steepest step between NEIGHBOURING pixels in the cell, not the
# spread of its values. That distinction is the whole thing, and the first version got it
# wrong: it used standard deviation and peak-to-peak, which worked on the dark editor
# screenshots it was built against and then did nothing at all on the first light-themed
# web page a user tested. Soft card shadows, 1px grey borders and a cream-on-white panel
# move the spread of a cell well past any floor low enough to catch grey text, so nearly
# every cell read as inked, the dense-region ceiling tripped, and the mesh fell back to
# painting whole rectangles. 24% of their screen, which is what they had complained
# about in the first place.
#
# A gradient separates them because the thing that makes text legible is a sharp edge:
#
#   flat background                       step  0-1
#   soft shadow under a card              step  0-3    (10 levels spread over 40px)
#   cream panel on a white page           step  0-2
#   1px grey border                       step 40-70
#   antialiased body text                 step 90-200
#
# Nothing a human can read has a shallow gradient, and nothing with a shallow gradient
# needs covering. A border lights up its own cells and no more, which is correct: it is
# content, it is just thin.
#
# The peak-to-peak floor stays as a second measure with a high floor, for anything with
# strong contrast that somehow has no steep step. Thresholds sit near the background end
# of each gap: a false positive here costs 196 extra covered pixels and a false negative
# costs a legible character.
GRADIENT_FLOOR = 30.0
PTP_FLOOR = 70.0

# Above this inked fraction, the mesh is not buying anything and the region is painted
# whole: tidier to look at, and a dense region is exactly where a mesh is most likely to
# leave an island of background between two covered runs.
MAX_FRACTION = 0.72

# How far outside its OCR box a revealed line is flattened. See `_pixel_mask`.
REVEAL_PAD = 2


def ink_mask(
    image: Image.Image,
    box: Box,
    *,
    cell: int = DEFAULT_CELL,
    reveal: Sequence[Box] = (),
) -> tuple[np.ndarray, int, int]:
    """Which `cell`-sized squares of `box` contain anything other than background.

    Returns the boolean mask and its shape, in cells. Dilated by one cell in every
    direction, so a glyph whose anti-aliased edge spills into the next square does not
    leave a sliver of itself visible.

    `reveal` are boxes of text lines that may stay visible -- see
    `scanner.ordinary_language_boxes`. A cell is excused only if it sits ENTIRELY inside
    one of them, and the exclusion happens before dilation, so a covered glyph's halo
    still spreads over the edge of a revealed label rather than the other way round.
    Both choices are the same choice: when in doubt, cover.
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
    def per_cell(plane: np.ndarray):
        blocks = plane.reshape(rows, cell, columns, cell).transpose(0, 2, 1, 3)
        return blocks.reshape(rows, columns, cell * cell)

    # Steepest step to a neighbour, horizontally or vertically, at every pixel.
    gradient = np.zeros_like(padded)
    np.maximum(
        gradient[:, :-1], np.abs(np.diff(padded, axis=1)), out=gradient[:, :-1]
    )
    np.maximum(
        gradient[:-1, :], np.abs(np.diff(padded, axis=0)), out=gradient[:-1, :]
    )

    if len(reveal):
        # Flatten the revealed lines out of both measures, at PIXEL resolution.
        #
        # The first version of this excused whole cells, and only cells lying entirely
        # within an OCR box. At a 6px cell a 16px line of text has one or two such rows
        # and its remaining rows stayed inked, so every "revealed" label got a bar drawn
        # over it anyway. Suppressing pixels instead means a cell straddling a label and
        # a key still fires -- from the key's half, which is the half that matters.
        keep = _pixel_mask(reveal, (left, top), padded.shape, pad=REVEAL_PAD)
        gradient[keep] = 0.0
        padded = np.where(keep, float(np.median(padded)), padded)

    values = per_cell(padded)
    inked = (per_cell(gradient).max(axis=2) >= GRADIENT_FLOOR) | (
        values.max(axis=2) - values.min(axis=2) >= PTP_FLOOR
    )

    return _dilate(inked), rows, columns


def _pixel_mask(
    reveal: Sequence[Box],
    origin: tuple[int, int],
    shape: tuple[int, int],
    *,
    pad: int,
) -> np.ndarray:
    """Pixels belonging to a revealed line, in the cropped frame's coordinates.

    `pad` grows each box outward. OCR boxes hug the glyphs, and the outermost pixels of
    an anti-aliased glyph sit just beyond the box -- enough gradient to rebuild a bar
    over the line the box was meant to reveal. Growing by 2px costs, at worst, two
    pixels off the top of whatever is on the next line, which is not legible.
    """
    offset_x, offset_y = origin
    height, width = shape
    keep = np.zeros(shape, dtype=bool)
    for left, top, right, bottom in reveal:
        x0 = max(0, left - offset_x - pad)
        y0 = max(0, top - offset_y - pad)
        x1 = min(width, right - offset_x + pad)
        y1 = min(height, bottom - offset_y + pad)
        if x1 > x0 and y1 > y0:
            keep[y0:y1, x0:x1] = True
    return keep


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
    reveal: Sequence[Box] = (),
) -> list[Box]:
    """Replace one flagged region with the parts of it that are not blank.

    Returns `[box]` unchanged when meshing would not help -- a dense region, or an
    empty one, or a region too small to mesh. Never returns less than the ink.
    """
    left, top, right, bottom = box
    if right - left < cell * 2 or bottom - top < cell * 2:
        return [box]

    # Two masks: everything that is here, and that minus the lines allowed to stay.
    raw, rows, columns = ink_mask(image, box, cell=cell)
    if raw.size == 0 or not raw.any():
        # Nothing visible at all. Not an invitation to reveal the region -- the mesh
        # simply has nothing to say, so defer to the region the model judged.
        return [box]

    mask = raw
    if len(reveal):
        mask, _, _ = ink_mask(image, box, cell=cell, reveal=reveal)
        if not mask.any():
            # Every inked pixel in this region belongs to a line OCR read confidently as
            # ordinary language. Nothing is covered here.
            #
            # Two other answers were tried first and both were worse. Returning `[box]`
            # painted the region whole, which took the demo fixture from 3.6% of the
            # frame to 11.2% -- revealing nothing is not the same as covering
            # everything. Falling back to the unrevealed ink covered the tail of an
            # English sentence that happened to be the only thing in two regions, which
            # is the complaint this feature exists to answer.
            #
            # Why this is safe despite the model having flagged the region: the region
            # verdict does not locate anything, and the per-line reading is the more
            # specific evidence about what text is actually there. More importantly, a
            # secret OCR CANNOT read is not inside any revealed box, so it stays
            # covered -- which is the case that matters, because OCR missed this user's
            # real AWS keys twice while the model caught them both times. What this
            # does risk is a short, low-entropy secret that reads as language, and that
            # is the documented cost of the whole feature. `--cover-labels` turns it
            # off.
            return []

    # The density ceiling, judged on what would actually be painted.
    #
    # Measuring `raw` here instead was tried for one build and is wrong in a way worth
    # recording: a cream panel with borders and a full-width input box is dense before
    # its labels are excused and sparse afterwards, so testing the pre-exclusion mask
    # disabled meshing on precisely the regions where revealing labels helps most. The
    # user's heading came back covered and the frame went from 7.6% to 10.5%. The
    # question this ceiling asks is "would a mesh here be a comb of bars with hairlines
    # between them", and only the final mask can answer it.
    if int(mask.sum()) / mask.size > max_fraction:
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

    return _drop_rules(image, _merge_vertically(boxes), cell) or [box]


def _drop_rules(
    image: Image.Image, boxes: Sequence[Box], cell: int
) -> list[Box]:
    """Remove isolated borders, dividers and scrollbars.

    A 1px panel border has a strong gradient across it and none along it, so every cell
    down its length reads as ink and `_merge_vertically` stacks them into a tall thin
    bar. A user reported it as "there is no need of the vertical block but it covered" --
    an 8x140px stripe down the edge of a card, hiding nothing.

    This removes coverage, so the test for it is strict and has three parts.

      ELONGATED.  Four times longer than it is wide, or the reverse.

      TRANSLATION-INVARIANT ALONG ITS LENGTH.  This is the real discriminator and the
      reason the first attempt failed. Width alone cannot separate a border from a narrow
      column of text -- dilation grows a one-cell core to three cells, 18px at the
      default, which is also about three characters. But a vertical rule is *the same
      all the way down*: every row of pixels is identical, so the spread of the row means
      is essentially zero. Text is not: rows alternate between glyph middles and the gaps
      between lines, and the spread is an order of magnitude larger. Measured on a drawn
      fixture, a 1px border scores under 1 and a line of credentials scores over 20.

    There was a third condition -- that the box touch nothing else -- on the reasoning
    that a sliver abutting a covered box might be the edge of something the mesh is
    hiding, and dropping it would leave a 6px strip of a credential showing. It was
    redundant, and it kept every artefact it was supposed to be guarding against. On the
    capture a user reported, four boxes survived the flatness test only because they
    touched something:

        (48, 274, 66, 365)   18x91  flat, touching 1   the card's left border
        (48, 166, 66, 250)   18x84  flat, touching 1   the same border, above it
        (515, 346, 551, 352) 36x6   flat, touching 2   the input field's underline
        (108, 346, 138, 352) 30x6   flat, touching 2   more of the same underline

    The redundancy: flatness is already a statement about the pixels *inside* the box. If
    every row is identical, there is no writing in there to expose, whoever its
    neighbours are. The sliver-on-the-edge-of-a-credential case cannot be flat, because a
    glyph edge inside the box is exactly what makes the row means vary. So the flatness
    test does all the work the isolation test was added for, and does it directly.
    """
    if len(boxes) < 2:
        return list(boxes)

    grey = image.convert("L")
    return [box for box in boxes if not _is_rule(grey, box)]


def _is_rule(grey: Image.Image, box: Box, flatness: float = 3.0) -> bool:
    """Is this box a plain line rather than something written?"""
    width, height = box[2] - box[0], box[3] - box[1]
    if width < 1 or height < 1:
        return False

    vertical = height >= width * 4
    horizontal = width >= height * 4
    if not (vertical or horizontal):
        return False

    patch = np.asarray(grey.crop(box), dtype=np.float32)
    if patch.size == 0:
        return False
    # Along the long axis: a rule looks the same at every step, writing does not.
    means = patch.mean(axis=1) if vertical else patch.mean(axis=0)
    return bool(means.std() < flatness)


def _touches(a: Box, b: Box, slack: int) -> bool:
    return (
        a[0] - slack < b[2]
        and b[0] - slack < a[2]
        and a[1] - slack < b[3]
        and b[1] - slack < a[3]
    )


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


def join_adjacent(
    boxes: Sequence[Box], *, overlap: float = 0.6, bridge: int = DEFAULT_CELL
) -> list[Box]:
    """Fuse bars that meet, so a line of text crossing a region seam is one box.

    Regions overlap by 6% by design, and a line of credentials wide enough to span two
    of them comes back as two bars with a shared edge and a duplicated middle. Left
    alone that is only cosmetic -- the pixels are covered either way -- but it draws two
    borders and two labels through the middle of one redaction, which looks like a bug.

    Only boxes that already touch are fused, and the result always contains both, so
    this can only ever cover more. `overlap` is how much of the shorter box's height
    has to be shared before two bars are considered the same line.

    `bridge` is how wide a gap may be closed, and it must be 0 whenever labels are being
    revealed. A tidying pass that is allowed to span a small gap will happily span the
    gap left by a revealed heading and cover it again, which is how "keep the label
    visible" quietly became "keep nothing visible" the first time this ran.
    """
    out = [tuple(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        for i in range(len(out)):
            for j in range(i + 1, len(out)):
                fused = _fuse(out[i], out[j], overlap, bridge)
                if fused is not None:
                    out[i] = fused
                    del out[j]
                    changed = True
                    break
            if changed:
                break
    return [tuple(b) for b in out]  # type: ignore[misc]


def _fuse(a: Box, b: Box, overlap: float, bridge: int) -> Box | None:
    """The union of `a` and `b` if they are the same line of content, else None."""
    shared = min(a[3], b[3]) - max(a[1], b[1])
    shorter = min(a[3] - a[1], b[3] - b[1])
    if shorter <= 0 or shared / shorter < overlap:
        return None
    # Horizontally touching or overlapping, plus `bridge` pixels of slack so two bars
    # separated by a hairline of background join rather than showing a seam.
    if min(a[2], b[2]) + bridge < max(a[0], b[0]):
        return None

    union = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))

    # A fusion may not invent area. Without this, the pass cascades: fusing by bounding
    # box makes the result taller than either input, the taller box then overlaps bars
    # on neighbouring lines, those fuse too, and it runs away. Measured on a light page
    # with 22 tight bars covering 2.8% of the frame, the tidying pass alone collapsed
    # them into 2 boxes covering 10.5% -- four times the damage, from a step whose only
    # purpose was to avoid drawing two borders where one would do.
    #
    # Allowing a tenth lets genuinely adjacent bars whose edges differ by a pixel or two
    # merge, and stops anything else.
    if _box_area(union) > (_box_area(a) + _box_area(b)) * 1.1:
        return None
    return union


def _box_area(box: Box) -> int:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def extend_along_ink(
    image: Image.Image,
    boxes: Sequence[Box],
    *,
    cell: int = DEFAULT_CELL,
    gap: int = 3,
    reveal: Sequence[Box] = (),
) -> list[Box]:
    """Grow each bar sideways while the line of content it sits on keeps going.

    This repairs a false negative that the fine grid creates. A line of credentials is
    wider than a region, so it is cut across a seam, and the model is then shown each
    piece separately. Asked about the piece reading `STRIPE_SECRET_KEY=sk_live_` with the
    value chopped off, it finds nothing high-entropy and clears the region -- correctly,
    for what it was shown. The neighbouring piece holding the value fires. Paint only the
    region that fired and the screenshot comes back with `sk_live_` legible, and with
    `@db.acme.internal:5432/p` legible on the line above, which happened on this
    project's own demo fixture and is how it was found.

    Covering the rest of the line is the conservative reading of what the model actually
    said. It flagged a region because a credential is in it; a credential does not stop
    being one at a tile boundary. So each bar grows left and right for as long as ink
    continues on the same rows, bridging gaps up to `gap` cells -- wide enough to cross
    the spaces inside a line, narrow enough to stop at a gutter or a column of
    whitespace.

    This deliberately overrides a `clean` verdict on the neighbouring region. That is the
    point: that verdict was reached on a crop the seam had already ruined.
    """
    if not boxes:
        return []

    mask, rows, columns = ink_mask(
        image, (0, 0, image.width, image.height), cell=cell, reveal=reveal
    )
    if mask.size == 0:
        return list(boxes)

    out: list[Box] = []
    for left, top, right, bottom in boxes:
        row_from = max(0, min(rows - 1, top // cell))
        row_to = max(0, min(rows - 1, (bottom - 1) // cell))
        band = mask[row_from : row_to + 1, :]

        start = max(0, min(columns - 1, left // cell))
        end = max(0, min(columns - 1, (right - 1) // cell))

        while start > 0:
            window = band[:, max(0, start - gap) : start]
            if not window.any():
                break
            start = max(0, start - gap)
        while end < columns - 1:
            window = band[:, end + 1 : min(columns, end + 1 + gap)]
            if not window.any():
                break
            end = min(columns - 1, end + gap)

        # Then trim back to the outermost inked cell, so bridging a gap cannot leave a
        # bar hanging over blank space.
        lit = band[:, start : end + 1].any(axis=0).nonzero()[0]
        if lit.size:
            start, end = start + int(lit[0]), start + int(lit[-1])

        out.append(
            (
                min(left, start * cell),
                top,
                max(right, min(image.width, (end + 1) * cell)),
                bottom,
            )
        )
    return out


def tighten_all(
    image: Image.Image,
    boxes: Sequence[Box],
    *,
    cell: int = DEFAULT_CELL,
    enabled: bool = True,
    reveal_labels: bool = True,
    reveal_boxes: Sequence[Box] | None = None,
) -> tuple[list[Box], float]:
    """Mesh every flagged region, and report how much of the original area survived.

    The ratio is worth logging: it is the one number that tells a user whether the thing
    they complained about actually got better on their screen rather than on a fixture.

    With `reveal_labels`, text lines that OCR read confidently as ordinary language are
    left visible, so a heading like "Your team's API key" survives while the key below it
    does not. This is the one place in the project where something is deliberately NOT
    covered inside a region the model flagged, so the reasoning is worth stating:

      * it is a whitelist. A line is revealed only on a positive, confident reading that
        looks like language. Unread, low-confidence, and unlike-language all stay
        covered, so every failure of the OCR or of the test defaults to covered.
      * it does not try to decide which text is sensitive. That is the mistake this
        project already made -- narrowing to the regex scanner's matched box left three
        credentials legible. This asks a different question with a safe default.
      * the cost is real: a short, low-entropy secret reads as ordinary language. A four
        digit PIN or a password like `hunter2` would be revealed. `--cover-labels` turns
        this off, and anything the model flagged is then covered whole.
    """
    if not enabled or not boxes:
        return list(boxes), 1.0

    reveal: list[Box] = []
    if reveal_labels:
        if reveal_boxes is not None:
            # Supplied by the caller, which in practice means Textract word boxes via
            # `aws_lane.reveal_boxes`. Word-level rather than line-level, so a label can
            # be revealed while the value beside it on the same line is covered.
            reveal = list(reveal_boxes)
        else:
            from .scanner import ordinary_language_boxes

            reveal = ordinary_language_boxes(image)

    out: list[Box] = []
    for box in boxes:
        out.extend(tighten(image, box, cell=cell, reveal=reveal))

    if not out:
        # Revealing a region is one thing; revealing the entire screen on the strength
        # of an OCR reading is another. If nothing at all survived, the model's regions
        # are restored. Bounds the blast radius of every judgement made in this module.
        return list(boxes), 1.0

    out = extend_along_ink(image, out, cell=cell, reveal=reveal)
    out = join_adjacent(out, bridge=0 if reveal else cell)

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
