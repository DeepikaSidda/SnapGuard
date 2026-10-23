"""The annotated view must not leak what the gate withheld.

This guards a mistake that was actually made. The first version of `overlay.annotate`
dimmed the withheld regions instead of destroying them, so the image produced to
*explain* a redaction rendered the credentials it was claiming to protect, legibly. The
intended use of that image is to put it in an article or a video, which would have made
the explain mode the leak.

The test is adversarial rather than structural: it runs the OCR engine over the withheld
region of the annotated image and asserts that no secret pattern can be matched in what
comes back. Checking "did we call GaussianBlur" would pass while the property it is
supposed to protect quietly broke.

Needs a live decider server. Skipped otherwise.

    .\\.venv\\Scripts\\python.exe -m pytest tests/test_overlay_redacts.py -s
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aperture.decider import Decider, DeciderUnavailable  # noqa: E402
from aperture.overlay import annotate  # noqa: E402
from aperture.patterns import Policy, RegionGate, Verdict  # noqa: E402
from aperture.questions import CONTEXT_RULES, REGION_RULES, SCREEN_KIND  # noqa: E402
from aperture.scanner import ocr_available, read_text_boxes, scan_text  # noqa: E402
from aperture.tiling import tile_grid  # noqa: E402

SCREEN = Path(__file__).resolve().parents[1] / "evalset" / "out" / "deploy_failure.png"

# Text the renderer itself draws over the region. Recovering these is expected and
# harmless; recovering anything else is not.
ANNOTATION_WORDS = {
    "region", "withheld", "sent", "skipped", "unreadable", "credential",
    "personal_contact", "private_conversation", "financial_records",
    "identity_document", "relevant", "legible", "not", "-",
}


def _is_annotation(text: str) -> bool:
    """Is this OCR run one of our own labels rather than screen content?"""
    tokens = [t for t in text.replace("-", " ").lower().split() if t]
    if not tokens:
        return True
    for token in tokens:
        # Scores read back as 'credential0.82' once OCR drops the space, and a caption
        # clipped by the region boundary comes back with the partial glyph attached:
        # 'credential' -> 'cre('. Strip anything that is not a letter from both ends.
        stripped = token.strip("0123456789.,:;_()[]{}|/\\<>'\"")
        if stripped in ANNOTATION_WORDS or token.replace(".", "").isdigit():
            continue
        # Then treat a prefix of one of our own words as one of our own words: 'cre' is
        # not recoverable screen content by any reading.
        if len(stripped) >= 2 and any(w.startswith(stripped) for w in ANNOTATION_WORDS):
            continue
        return False
    return True


def _keep_only(image: Image.Image, boxes) -> Image.Image:
    """A copy of `image` with everything outside `boxes` blanked out.

    Cropping each region separately and OCR-ing the crops reads the withheld area, but
    it also chops every line of text at the region seam -- and at a 6x4 grid a line of
    credentials spans three regions, so no crop contains a whole key and the pattern
    scanner has nothing to match. Blanking the surroundings instead keeps the withheld
    text intact and still reads nothing but the withheld text.
    """
    out = Image.new("RGB", image.size, (0, 0, 0))
    source = image.convert("RGB")
    for box in boxes:
        out.paste(source.crop(box), box[:2])
    return out


@pytest.fixture(scope="module")
def annotated():
    if not ocr_available():
        pytest.skip("needs the ocr extra to attempt recovery")
    decider = Decider()
    try:
        decider.require_vision()
    except (DeciderUnavailable, Exception) as exc:  # noqa: BLE001
        pytest.skip(f"no vision server: {exc}")

    image = Image.open(SCREEN)
    gate = RegionGate(
        decider,
        rules=REGION_RULES,
        context_rules=CONTEXT_RULES,
        info_questions=SCREEN_KIND,
        policy=Policy(fail_closed=True),
    )
    result = gate.inspect(image)
    assert result.verdict is Verdict.REDACT, "the fixture screen should be redacted"
    assert result.flagged, "something should have been flagged to withhold"

    # The gate's own grid, not a hardcoded one. This test used to build a 2x2 grid by
    # hand; when the default became 6x4 the two stopped corresponding and `annotate`
    # rendered an image with nothing destroyed in it. `annotate` now refuses a mismatch
    # (see test_a_mismatched_grid_is_refused), and the right grid here is whatever the
    # gate used.
    tiles = tile_grid(image, columns=gate.columns, rows=gate.rows)
    return image, annotate(image, result, tiles), result, tiles


def test_the_original_really_does_leak(annotated):
    """Control. If the source image has no detectable secret, the test below is vacuous."""
    image, _, result, _ = annotated
    visible = _keep_only(image, [t.box for t in result.flagged])
    text = " ".join(t for t, _ in read_text_boxes(visible))
    assert scan_text(text), (
        "no secret pattern found in the original withheld regions, so this test cannot "
        f"demonstrate anything. read back: {text[:200]!r}"
    )


def test_no_secret_survives_in_the_annotated_view(annotated):
    """The property that matters: the explain image must not carry the secret."""
    _, shown, result, _ = annotated
    visible = _keep_only(shown, [t.box for t in result.flagged])
    matched = scan_text(" ".join(t for t, _ in read_text_boxes(visible)))
    assert not matched, f"the annotated view still leaks: {matched}"


def test_only_our_own_labels_are_recoverable(annotated):
    """Nothing of the underlying screen should come back, labels aside."""
    _, shown, result, _ = annotated
    visible = _keep_only(shown, [t.box for t in result.flagged])
    leaked = [t for t, _ in read_text_boxes(visible) if not _is_annotation(t)]
    print(f"\n  recoverable non-label text: {leaked or 'none'}")
    assert not leaked, f"screen content recovered from a withheld region: {leaked}"


def test_a_mismatched_grid_is_refused(annotated):
    """The bug that a grid change surfaced: tiles that are not the result's tiles.

    Passing them produced an explain figure in which the withheld branch never matched
    anything, so the credentials were rendered in full. Drawing nothing is not an
    acceptable response to an inconsistent argument when the thing being drawn is a
    redaction.
    """
    image, _, result, _ = annotated
    wrong = tile_grid(image, columns=2, rows=2)
    with pytest.raises(ValueError, match="do not match"):
        annotate(image, result, wrong)
