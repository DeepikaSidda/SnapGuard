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
        # Scores read back as 'credential0.82' once OCR drops the space.
        stripped = token.rstrip("0123456789.")
        if stripped in ANNOTATION_WORDS or token.replace(".", "").isdigit():
            continue
        return False
    return True


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

    tiles = tile_grid(image, columns=2, rows=2)
    return image, annotate(image, result, tiles), result, tiles


def test_the_original_really_does_leak(annotated):
    """Control. If the source image has no detectable secret, the test below is vacuous."""
    image, _, result, tiles = annotated
    box = result.flagged[0].box
    text = " ".join(t for t, _ in read_text_boxes(image.crop(box)))
    assert scan_text(text), (
        "no secret pattern found in the original withheld region, so this test cannot "
        "demonstrate anything"
    )


def test_no_secret_survives_in_the_annotated_view(annotated):
    """The property that matters: the explain image must not carry the secret."""
    _, shown, result, tiles = annotated
    box = result.flagged[0].box
    recovered = " ".join(t for t, _ in read_text_boxes(shown.crop(box)))
    matched = scan_text(recovered)
    assert not matched, f"the annotated view still leaks: {matched}"


def test_only_our_own_labels_are_recoverable(annotated):
    """Nothing of the underlying screen should come back, labels aside."""
    _, shown, result, tiles = annotated
    box = result.flagged[0].box
    leaked = [
        text
        for text, _ in read_text_boxes(shown.crop(box))
        if not _is_annotation(text)
    ]
    print(f"\n  recoverable non-label text: {leaked or 'none'}")
    assert not leaked, f"screen content recovered from a withheld region: {leaked}"
