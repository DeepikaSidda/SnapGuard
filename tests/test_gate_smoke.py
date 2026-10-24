"""End-to-end smoke test for the gate, against a live decider server.

Not a unit test: it needs a server on :8099 and takes minutes on CPU. It exists because
the pieces can each look right and still compose wrongly, and because a measured verdict
on known screens is the only evidence that matters.

    .\\.venv\\Scripts\\python.exe -m pytest tests/test_gate_smoke.py -s

Skipped automatically when no server is listening.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from snapguard.decider import Decider, DeciderUnavailable, looks_blank  # noqa: E402
from snapguard.patterns import Policy, RegionGate, Verdict  # noqa: E402
from snapguard.questions import (  # noqa: E402
    CONTEXT_RULES,
    REGION_RULES,
    SCREEN_KIND,
)

SCREENS = Path(__file__).resolve().parents[1] / "evalset" / "out"

# Ground truth: may this screen be sent to a cloud model as it stands?
EXPECTED = {
    "ide_env": Verdict.REDACT,       # a secrets file in one pane; the rest is fine
    "bank": Verdict.WITHHOLD,        # the whole screen is the problem
    "dm": Verdict.WITHHOLD,          # ditto
    "terminal_error": Verdict.PASS,  # a stack trace, nothing private
    "docs": Verdict.PASS,            # public documentation
    # One screen per rule, so a rule cannot quietly stop working. These two were added
    # after the rules shipped untested; see research/README.md section 9.
    "id_card": Verdict.WITHHOLD,     # identity_document: licence, portrait, DOB
    "contacts": Verdict.REDACT,      # personal_contact: emails, phones, addresses
}


@pytest.fixture(scope="module")
def decider() -> Decider:
    client = Decider()
    try:
        client.require_vision()
    except (DeciderUnavailable, Exception) as exc:  # noqa: BLE001
        pytest.skip(f"no vision server available: {exc}")
    return client


@pytest.fixture(scope="module")
def gate(decider: Decider) -> RegionGate:
    return RegionGate(
        decider,
        rules=REGION_RULES,
        context_rules=CONTEXT_RULES,
        info_questions=SCREEN_KIND,
        policy=Policy(fail_closed=True),
    )


def test_blank_images_are_caught_without_asking_the_model():
    """The documented trap: the model answers blank images confidently at chance."""
    assert looks_blank(Image.new("RGB", (400, 400), "white"))
    assert looks_blank(Image.new("RGB", (400, 400), (17, 17, 17)))
    assert not looks_blank(Image.open(SCREENS / "ide_env.png"))


def test_blank_frame_fails_closed(gate: RegionGate):
    result = gate.inspect(Image.new("RGB", (800, 600), "white"))
    assert result.verdict is Verdict.UNREADABLE
    assert result.decider_calls == 0, "a blank frame should cost nothing to reject"


@pytest.mark.parametrize("name", list(EXPECTED))
def test_screens_get_the_right_verdict(gate: RegionGate, name: str):
    image = Image.open(SCREENS / f"{name}.png")
    result = gate.inspect(image)

    print(f"\n  {name}")
    print(f"    screen_kind : {result.info.get('screen_kind')} "
          f"({result.info.get('screen_kind_confidence')})")
    for key, value in result.context.items():
        print(f"    {key:21s}: {value:.3f}")
    for finding in result.findings:
        print(f"    FIRED {finding}")
    print(f"    -> {result.summary()}")
    print(f"    cleared {len(result.cleared)} regions, flagged {len(result.flagged)}")

    assert result.verdict is EXPECTED[name], (
        f"{name}: expected {EXPECTED[name]}, got {result.verdict}. "
        f"context={result.context}"
    )
