"""The mesh must cover every inked pixel of a flagged region, and little else.

A user asked, four reports in, for the redaction to work like graph paper: a fine mesh,
fill in the squares that matter. `mesh.tighten` does that by measuring pixels rather than
asking the model, so the property to pin is not "does it find the secret" -- it makes no
attempt to find the secret -- but the stronger and simpler one:

    NOTHING THAT IS NOT BACKGROUND SURVIVES INSIDE A FLAGGED REGION.

That is what makes the mesh safe where narrowing to a regex hit was not. A regex hit is a
claim about which text is sensitive and it was wrong on real data (three credentials left
legible, see test_refine_localises.py). The mesh makes no claim about text at all: a
secret cannot hide in blank space, so dropping only blank space cannot expose one.

Two halves, and a test with only the first is worse than no test:

  1. the covered area shrinks                     (the complaint)
  2. no non-background pixel escapes              (the thing that matters)

No decider server and no OCR needed: this is arithmetic on pixels.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aperture.mesh import DEFAULT_CELL, ink_mask, tighten, tighten_all  # noqa: E402
from aperture.redact import redact  # noqa: E402

BACKGROUND = (30, 30, 36)


def _font(size: int = 13):
    for name in ("consola.ttf", "DejaVuSansMono.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _pane() -> tuple[Image.Image, tuple[int, int, int, int]]:
    """An editor pane with two lines of text near the top and blank space below.

    Shaped like the case the user reported: a region the model flags because of one line
    in it, where most of the area is empty.
    """
    img = Image.new("RGB", (420, 320), BACKGROUND)
    d = ImageDraw.Draw(img)
    d.text((12, 10), "AKIAEXAMPLEKEYID1234", font=_font(), fill=(206, 145, 120))
    d.text((12, 30), "EXAMPLEKEYa1B2c3D4e5F6g7", font=_font(), fill=(206, 145, 120))
    return img, (0, 0, 420, 320)


def _area(boxes) -> int:
    return sum((r - l) * (b - t) for l, t, r, b in boxes)


def _ink_coordinates(image: Image.Image) -> list[tuple[int, int]]:
    """Every pixel that differs from the flat background colour."""
    arr = np.asarray(image.convert("RGB"), dtype=np.int16)
    base = np.asarray(BACKGROUND, dtype=np.int16)
    differs = np.abs(arr - base).sum(axis=2) > 12
    ys, xs = np.nonzero(differs)
    return list(zip(xs.tolist(), ys.tolist()))


# ---- 1. the complaint --------------------------------------------------------


def test_the_mesh_shrinks_a_mostly_empty_region():
    image, box = _pane()
    boxes = tighten(image, box)

    assert boxes != [box], "a region that is mostly blank should not be painted whole"
    assert _area(boxes) < _area([box]) * 0.4, (
        f"expected a big reduction, got {_area(boxes) / _area([box]):.0%} of the region"
    )


def test_tighten_all_reports_the_reduction():
    image, box = _pane()
    _, ratio = tighten_all(image, [box])
    assert 0.0 < ratio < 0.4


# ---- 2. the thing that matters ----------------------------------------------


def test_every_inked_pixel_is_covered():
    """The safety property. Not 'the secret is covered' -- everything non-blank is."""
    image, box = _pane()
    boxes = tighten(image, box)

    uncovered = [
        (x, y)
        for x, y in _ink_coordinates(image)
        if not any(l <= x < r and t <= y < b for l, t, r, b in boxes)
    ]
    assert not uncovered, f"{len(uncovered)} inked pixels escaped, e.g. {uncovered[:5]}"


def test_nothing_survives_after_painting():
    """End to end: paint the meshed boxes and confirm the region is flat again."""
    image, box = _pane()
    boxes = tighten(image, box)
    painted = redact(image, boxes, mode="block", label=None).image

    # Everything that was ink must now be either the block fill or the border colour.
    before = set(_ink_coordinates(image))
    still_original = [
        (x, y) for x, y in before if painted.getpixel((x, y)) == image.getpixel((x, y))
    ]
    assert not still_original, (
        f"{len(still_original)} original pixels survived the redaction"
    )


def test_ink_in_every_corner_is_found():
    """A region is flagged as a whole, so the mesh cannot be biased toward the middle."""
    img = Image.new("RGB", (300, 300), BACKGROUND)
    d = ImageDraw.Draw(img)
    for x, y in ((2, 2), (292, 2), (2, 292), (292, 292), (148, 148)):
        d.rectangle([x, y, x + 5, y + 5], fill=(250, 250, 250))

    boxes = tighten(img, (0, 0, 300, 300))
    for x, y in _ink_coordinates(img):
        assert any(l <= x < r and t <= y < b for l, t, r, b in boxes), (
            f"ink at {(x, y)} was not covered"
        )


def test_a_single_thin_stroke_is_caught():
    """The case standard deviation alone misses: one hairline on a flat background."""
    img = Image.new("RGB", (200, 200), BACKGROUND)
    ImageDraw.Draw(img).line([(100, 20), (100, 40)], fill=(240, 240, 240), width=1)

    mask, _, _ = ink_mask(img, (0, 0, 200, 200))
    assert mask.any(), "a 1px stroke on a flat background must register as ink"

    boxes = tighten(img, (0, 0, 200, 200))
    for x, y in _ink_coordinates(img):
        assert any(l <= x < r and t <= y < b for l, t, r, b in boxes)


# ---- fail-safe behaviour -----------------------------------------------------


def test_a_dense_region_is_painted_whole():
    """Above the inked-fraction ceiling, meshing buys nothing and risks gaps."""
    img = Image.new("RGB", (300, 300), BACKGROUND)
    d = ImageDraw.Draw(img)
    for y in range(0, 300, 14):
        d.text((4, y), "dense text across the entire region here", font=_font(11),
               fill=(200, 200, 210))

    box = (0, 0, 300, 300)
    assert tighten(img, box) == [box]


def test_an_empty_region_is_painted_whole():
    """Fail closed. If no ink is found, trust the region the model judged."""
    img = Image.new("RGB", (300, 300), BACKGROUND)
    box = (0, 0, 300, 300)
    assert tighten(img, box) == [box]


def test_a_tiny_region_is_left_alone():
    img = Image.new("RGB", (300, 300), BACKGROUND)
    box = (0, 0, DEFAULT_CELL, DEFAULT_CELL * 2)
    assert tighten(img, box) == [box]


def test_a_photo_region_is_covered_whole():
    """Noise everywhere is high variation everywhere, which is the safe answer."""
    rng = np.random.default_rng(0)
    noise = rng.integers(0, 256, size=(200, 200, 3), dtype=np.uint8)
    img = Image.fromarray(noise)
    box = (0, 0, 200, 200)
    assert tighten(img, box) == [box]


def test_disabled_is_a_passthrough():
    image, box = _pane()
    boxes, ratio = tighten_all(image, [box], enabled=False)
    assert boxes == [box]
    assert ratio == 1.0


def test_mesh_does_not_wrap_ink_around_an_edge():
    """`_dilate` uses np.roll, which wraps. Ink on the right edge must not light up the
    left edge of the same row, or a redaction would grow a phantom bar."""
    img = Image.new("RGB", (300, 300), BACKGROUND)
    ImageDraw.Draw(img).rectangle([290, 140, 299, 160], fill=(250, 250, 250))

    mask, _, columns = ink_mask(img, (0, 0, 300, 300))
    assert mask[:, 0].sum() == 0, "ink at the right edge wrapped round to the left"
    assert mask[:, columns - 1].any()


@pytest.mark.parametrize("cell", [4, 6, 8, 14, 24])
def test_the_property_holds_at_every_cell_size(cell: int):
    image, box = _pane()
    boxes = tighten(image, box, cell=cell)
    for x, y in _ink_coordinates(image):
        assert any(l <= x < r and t <= y < b for l, t, r, b in boxes), (
            f"cell={cell}: ink at {(x, y)} escaped"
        )


# ---- joining bars across a region seam --------------------------------------


def test_joining_two_bars_can_only_cover_more():
    """`join_adjacent` is cosmetic, so the one thing it must never do is uncover."""
    from aperture.mesh import join_adjacent

    a = (100, 50, 300, 64)
    b = (290, 50, 480, 64)
    joined = join_adjacent([a, b])

    assert len(joined) == 1
    for box in (a, b):
        assert any(
            j[0] <= box[0] and j[1] <= box[1] and j[2] >= box[2] and j[3] >= box[3]
            for j in joined
        ), f"{box} is no longer covered"


def test_bars_on_different_lines_are_not_joined():
    from aperture.mesh import join_adjacent

    boxes = [(100, 50, 300, 64), (100, 200, 300, 214)]
    assert len(join_adjacent(boxes)) == 2


def test_bars_far_apart_on_the_same_line_are_not_joined():
    """Two separate words on one line should not become one bar across the gap."""
    from aperture.mesh import join_adjacent

    boxes = [(100, 50, 200, 64), (600, 50, 700, 64)]
    assert len(join_adjacent(boxes)) == 2


def test_joining_survives_the_pixel_property():
    """The safety property again, this time after the cosmetic pass."""
    image, box = _pane()
    boxes, _ = tighten_all(image, [box])
    for x, y in _ink_coordinates(image):
        assert any(l <= x < r and t <= y < b for l, t, r, b in boxes), (
            f"ink at {(x, y)} escaped after joining"
        )


# ---- the coverage number ----------------------------------------------------


def test_coverage_counts_overlapping_boxes_once():
    """Summing box areas double-counted the overlap and overstated the damage."""
    image = Image.new("RGB", (100, 100), BACKGROUND)
    overlapping = [(0, 0, 60, 100), (40, 0, 100, 100)]
    assert redact(image, overlapping, label=None).covered_pixels() == 100 * 100


def test_coverage_never_exceeds_the_image():
    image = Image.new("RGB", (100, 100), BACKGROUND)
    r = redact(image, [(0, 0, 100, 100), (0, 0, 100, 100), (10, 10, 90, 90)], label=None)
    assert r.fraction_of(image) == 1.0


# ---- extending along a line across a region seam ----------------------------


def _seam_screen():
    """A long credential line, and only its right-hand half flagged.

    Reproduces what the fine grid does to a wide line: the model is shown each piece
    separately, the piece without the high-entropy value in it looks clean and is
    cleared, and painting only the piece that fired leaves the prefix readable.
    """
    img = Image.new("RGB", (600, 120), BACKGROUND)
    ImageDraw.Draw(img).text(
        (20, 50), "STRIPE_SECRET_KEY=sk_test_REDACTEDFIXTURE", font=_font(13),
        fill=(206, 145, 120),
    )
    # The gate flagged only the right half.
    return img, (300, 30, 600, 90)


def test_extension_covers_the_rest_of_the_line():
    from aperture.mesh import extend_along_ink, tighten

    image, flagged = _seam_screen()
    bars = tighten(image, flagged)
    extended = extend_along_ink(image, bars)

    reached = min(b[0] for b in extended)
    assert reached <= 26, (
        f"the bar should have grown back to the start of the line, stopped at {reached}"
    )


def test_extension_covers_every_inked_pixel_on_the_flagged_line():
    """The property, stated over the line rather than the region."""
    from aperture.mesh import extend_along_ink, tighten

    image, flagged = _seam_screen()
    boxes = extend_along_ink(image, tighten(image, flagged))

    on_the_line = [
        (x, y) for x, y in _ink_coordinates(image) if flagged[1] <= y < flagged[3]
    ]
    assert on_the_line, "fixture should have ink on the flagged rows"
    uncovered = [
        (x, y)
        for x, y in on_the_line
        if not any(l <= x < r and t <= y < b for l, t, r, b in boxes)
    ]
    assert not uncovered, f"{len(uncovered)} pixels of the line escaped: {uncovered[:5]}"


def test_extension_stops_at_a_gutter():
    """It must not run away across the whole screen at the first gap."""
    from aperture.mesh import extend_along_ink, tighten

    img = Image.new("RGB", (600, 120), BACKGROUND)
    d = ImageDraw.Draw(img)
    d.text((400, 50), "secret=abc123", font=_font(13), fill=(230, 230, 230))
    # Unrelated content far to the left, past a wide gap.
    d.text((20, 50), "ok", font=_font(13), fill=(230, 230, 230))

    boxes = extend_along_ink(img, tighten(img, (380, 30, 600, 90)))
    assert min(b[0] for b in boxes) > 120, (
        "extension crossed a wide gap and swallowed unrelated content"
    )


def test_extension_can_only_grow_a_box():
    from aperture.mesh import extend_along_ink

    image, flagged = _seam_screen()
    original = [(320, 40, 400, 70)]
    grown = extend_along_ink(image, original)
    assert grown[0][0] <= 320 and grown[0][2] >= 400
    assert grown[0][1] == 40 and grown[0][3] == 70, "vertical extent must not change"
