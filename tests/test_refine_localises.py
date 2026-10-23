"""Refinement must narrow the redaction without letting a secret through.

This pins a mistake that was made and caught by a user looking at the output. The gate
flags a region, and a 2x2 grid makes a region a quarter of a 1080p screen -- so hiding
one line of text destroyed a file tree, an editor pane and a lot of empty space.

The tempting fix was to trust the deterministic scanner's exact box and cover only that.
Measured, it produced a screenshot with THREE credentials still legible: the scanner had
matched one secret of four, because two wrapped across lines and a Stripe key was shorter
than its pattern required.

So the property has two halves, and a test that only checks one of them is worse than no
test:

  1. the redacted area must be smaller than the whole region  (the user's complaint)
  2. no secret may survive in it                              (the thing that matters)

Driven with a stub decider, because the policy has to hold regardless of what the model
answers on any given afternoon.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aperture.decider import Answer, Reply, noul  # noqa: E402
from aperture.patterns import Policy, RegionGate, Rule, Verdict  # noqa: E402
from aperture.redact import resolve_boxes  # noqa: E402

CREDENTIAL = Rule(
    name="credential",
    question=noul("Is a credential visible?", true="yes", false="no"),
    threshold=0.50,
    on_match=Verdict.REDACT,
    describes="a credential is visible",
)


class TopLeftOnly:
    """Fires only on content in the upper-left quarter of whatever it is shown.

    Simulates a model that can localise: the full region fires, and when the region is
    subdivided only the sub-region actually holding the content fires.
    """

    def __init__(self) -> None:
        self.calls = 0

    def ask(self, state, questions, images=()):  # noqa: ANN001
        self.calls += 1
        img = images[0].convert("L")
        # The marker is a bright block; look for it in the upper-left quadrant.
        w, h = img.size
        patch = img.crop((0, 0, w // 2, h // 2))
        bright = sum(1 for p in patch.getdata() if p > 200)
        value = 0.9 if bright > 40 else 0.05
        answers = {
            name: Answer(name, "noul", value, value, "true" if value >= 0.5 else "false")
            for name in questions
        }
        return Reply(answers=answers, input_tokens=0, latency_ms=0.0, model="stub")


def _screen() -> Image.Image:
    """A 1200x800 screen with a bright 'secret' block in the top-left of each region."""
    img = Image.new("RGB", (1200, 800), (30, 30, 36))
    d = ImageDraw.Draw(img)
    for i in range(0, 800, 26):
        d.text((16, i), "ordinary content on this line", fill=(120, 120, 130))
    # The 'secret': a bright block near the top-left.
    d.rectangle([40, 40, 240, 120], fill=(250, 250, 250))
    return img


def _area(boxes) -> int:
    return sum((r - l) * (b - t) for l, t, r, b in boxes)


def test_refinement_narrows_the_box():
    """The user's complaint: a flagged region covered a quarter of the screen."""
    screen = _screen()
    coarse = RegionGate(TopLeftOnly(), rules=[CREDENTIAL], columns=2, rows=2,
                        policy=Policy(fail_closed=True), refine_flagged=False)
    fine = RegionGate(TopLeftOnly(), rules=[CREDENTIAL], columns=2, rows=2,
                      policy=Policy(fail_closed=True), refine_flagged=True)

    without = coarse.inspect(screen)
    with_refine = fine.inspect(screen)

    assert without.verdict is Verdict.REDACT
    assert with_refine.verdict is Verdict.REDACT
    assert _area(with_refine.boxes()) < _area(without.boxes()), (
        "refinement should cover less than the whole region"
    )
    assert with_refine.refined, "refinement should have recorded tighter boxes"
    assert any("narrowed" in note for note in with_refine.notes)


def test_refinement_still_covers_the_content():
    """The half that matters: the thing that fired must stay inside the boxes."""
    screen = _screen()
    gate = RegionGate(TopLeftOnly(), rules=[CREDENTIAL], columns=2, rows=2,
                      policy=Policy(fail_closed=True), refine_flagged=True)
    result = gate.inspect(screen)

    secret = (40, 40, 240, 120)
    covered = any(
        l <= secret[0] and t <= secret[1] and r >= secret[2] and b >= secret[3]
        for l, t, r, b in result.boxes()
    )
    assert covered, f"the secret at {secret} is not inside {result.boxes()}"


def test_refinement_costs_nothing_on_a_clean_screen():
    """Only flagged regions are refined, so a clean screen must not pay for it."""
    clean = Image.new("RGB", (1200, 800), (30, 30, 36))
    d = ImageDraw.Draw(clean)
    for i in range(0, 800, 26):
        d.text((16, i), "nothing interesting here at all", fill=(120, 120, 130))

    off = TopLeftOnly()
    on = TopLeftOnly()
    RegionGate(off, rules=[CREDENTIAL], columns=2, rows=2, refine_flagged=False).inspect(clean)
    RegionGate(on, rules=[CREDENTIAL], columns=2, rows=2, refine_flagged=True).inspect(clean)
    assert on.calls == off.calls, "refinement should not run when nothing is flagged"


def test_resolve_boxes_keeps_the_whole_region_despite_a_precise_hit():
    """The regression that leaked three credentials.

    A scanner hit is evidence of *a* secret, never evidence that it is the only one, so
    it must not shrink the region the model flagged.
    """
    region = (960, 0, 1920, 540)
    precise = (1025, 172, 1374, 200)
    boxes, refined = resolve_boxes([region], [precise])

    assert region in boxes, (
        "the flagged region must still be covered; narrowing to the scanner's single "
        "hit left three other credentials legible"
    )
    assert precise in boxes
    assert refined == 1


def test_resolve_boxes_keeps_hits_outside_any_flagged_region():
    """The scanner catching what the model missed is the point of having two lanes."""
    boxes, _ = resolve_boxes([(0, 0, 100, 100)], [(500, 500, 600, 520)])
    assert (500, 500, 600, 520) in boxes
