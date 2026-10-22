"""The coarse screen must not be able to clear a frame that has a secret in it.

This pins a real false negative. A screenshot with a live-looking AWS key passed cleanly
through a `guard` run: the batched "does ANY of these regions contain a credential"
question answered just under the rule's 0.50 threshold, all four tiles were cleared, and
the fine pass -- which scores that tile at 0.824 -- never ran.

Measured on the fixture screen:

    coarse, batch of 4 containing the key   0.505
    rule threshold                          0.500      margin 0.005
    fine pass on the dirty tile alone       0.824
    coarse, batch of 4 clean regions        0.061

So the batched question is only good for recognising an obviously clean batch. The gate
now judges it against `coarse_clear_below` (0.25), far from any rule threshold, and
anything above that is examined region by region.

The test drives this with a stub decider rather than the real model, because the point is
the *policy* -- that a borderline coarse answer cannot clear a region -- and that has to
hold at every value, not just the one the model happened to return today.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aperture.decider import Answer, Reply, noul  # noqa: E402
from aperture.patterns import Policy, RegionGate, Rule, Verdict  # noqa: E402

CREDENTIAL = Rule(
    name="credential",
    question=noul("Is a credential visible?", true="yes", false="no"),
    any_question=noul("Does ANY of these show a credential?", true="yes", false="no"),
    threshold=0.50,
    on_match=Verdict.REDACT,
    describes="a credential is visible",
)


class StubDecider:
    """Answers the coarse question with `coarse`, and single regions with `fine`.

    Distinguishes the two by how many images the request carries, which is exactly how
    the gate uses them.
    """

    def __init__(self, coarse: float, fine: float) -> None:
        self.coarse = coarse
        self.fine = fine
        self.coarse_calls = 0
        self.fine_calls = 0

    def ask(self, state, questions, images=()):  # noqa: ANN001
        batched = len(images) > 1
        if batched:
            self.coarse_calls += 1
        else:
            self.fine_calls += 1
        value = self.coarse if batched else self.fine
        answers = {
            name: Answer(name, "noul", value, value, "true" if value >= 0.5 else "false")
            for name in questions
        }
        return Reply(answers=answers, input_tokens=0, latency_ms=0.0, model="stub")


def _screen() -> Image.Image:
    img = Image.new("RGB", (1200, 800), (24, 24, 30))
    d = ImageDraw.Draw(img)
    # Enough variation that looks_blank() does not short-circuit the gate.
    for i in range(0, 800, 24):
        d.text((20, i), f"line {i} some content here", fill=(210, 210, 220))
    return img


def _gate(decider) -> RegionGate:  # noqa: ANN001
    return RegionGate(
        decider,
        rules=[CREDENTIAL],
        policy=Policy(fail_closed=True),
        columns=2,
        rows=2,
    )


@pytest.mark.parametrize("coarse", [0.505, 0.499, 0.48, 0.40, 0.30, 0.26])
def test_a_borderline_coarse_answer_cannot_clear_a_dirty_frame(coarse: float):
    """At every value near the old threshold, the fine pass must still run.

    0.499 is the value that caused the real miss: just under 0.50, so the old code
    cleared all four regions without looking at any of them.
    """
    decider = StubDecider(coarse=coarse, fine=0.824)
    result = _gate(decider).inspect(_screen())

    assert decider.fine_calls > 0, (
        f"coarse={coarse} cleared the frame with no fine pass; this is the bug"
    )
    assert result.verdict is Verdict.REDACT
    assert result.flagged, "the dirty regions should have been flagged"


def test_a_confidently_clean_batch_still_skips_the_fine_pass():
    """The optimisation has to survive the fix, or the gate costs twice as much.

    Clean batches measured 0.061 on the real model.
    """
    decider = StubDecider(coarse=0.061, fine=0.0)
    result = _gate(decider).inspect(_screen())

    assert decider.coarse_calls == 1
    assert decider.fine_calls == 0, "a clean batch should not need region-by-region work"
    assert result.verdict is Verdict.PASS
    assert len(result.cleared) == 4


def test_the_clearing_threshold_is_far_from_the_rule_threshold():
    """Guards against someone 'simplifying' the two thresholds back into one."""
    gate = _gate(StubDecider(0.0, 0.0))
    assert gate.coarse_clear_below <= 0.30
    assert gate.coarse_clear_below < CREDENTIAL.threshold - 0.15, (
        "the coarse clearing threshold must stay well below the rule threshold; "
        "they were 0.005 apart when a credential slipped through"
    )


def test_a_hot_coarse_batch_is_recorded_in_the_notes():
    """The reason a batch was drilled into should be visible, not silent."""
    decider = StubDecider(coarse=0.505, fine=0.824)
    result = _gate(decider).inspect(_screen())
    assert any("coarse screen" in note for note in result.notes), result.notes
