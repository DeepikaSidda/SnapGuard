"""Does a whole-frame "this screen is a list of people" question separate cleanly?

The problem it is meant to fix: at a 6x4 grid the contacts screen only fires
`personal_contact` on 4 regions of 24, so the other 20 -- holding phone numbers, street
addresses and names -- would be sent. At 2x2 all four regions fired and nothing was sent.
The finer grid traded protection away on this rule, because `personal_contact` is a
judgement about a pane of records and a small crop holding `+44 770` is not one.

The other three whole-frame rules already work this way, for the same reason stated in
`questions.py`: if the screen *is* a bank statement, there is no clean part to send. A
screen that *is* a contacts list has no clean part either.

What has to be true for this to ship:

    contacts    fires clearly
    the other 11 eval screens  do not fire, with margin

The failure to watch for is a screen with one incidental email on it -- a git log, a
README with a support address -- being withheld whole. `docs`, `terminal_error` and
`sha_soup` are in the set to catch that.

One request per screen, so about three minutes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from aperture.decider import Decider, noul  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "evalset" / "out"

# Truth: should a whole-frame "this is a sheet of people's details" question fire?
TRUTH = {
    "contacts": True,
    "id_card": False,       # one person, a document, not a list
    "dm": False,            # a conversation, caught by its own rule
    "bank": False,
    "ide_env": False,
    "deploy_failure": False,
    "terminal_error": False,
    "docs": False,
    "sha_soup": False,
    "public_metrics": False,
    "dark_editor": False,
    "token_in_url": False,
}

CANDIDATES = {
    # Narrow and plural: the thing being asked about is a *collection*, which is what
    # makes it different from the existing per-region `personal_contact` rule.
    "contact_sheet": noul(
        "Does this screen show a list or table of several people's personal contact "
        "details, such as their phone numbers, email addresses or home addresses?",
        true="a set of records holding contact details for multiple people is on screen",
        false="no list of people's contact details is shown",
    ),
    # A looser wording, to see whether the plural framing is doing the work or whether
    # the model would answer the same to anything mentioning contact details.
    "any_contact": noul(
        "Are people's personal contact details visible anywhere on this screen?",
        true="contact details for at least one person are visible",
        false="no personal contact details are visible",
    ),
}

STATE = "A screenshot of an application window on a computer."


def main() -> int:
    decider = Decider()
    decider.require_vision()
    print(decider.banner())

    names = [n for n in TRUTH if (OUT / f"{n}.png").exists()]
    print(f"\n  {'screen':<16}{'truth':>7}" +
          "".join(f"{k:>16}" for k in CANDIDATES))
    print("  " + "-" * (23 + 16 * len(CANDIDATES)))

    scores: dict[str, dict[str, float]] = {}
    for name in names:
        image = Image.open(OUT / f"{name}.png")
        reply = decider.ask(STATE, CANDIDATES, [image])
        scores[name] = {k: reply.value(k) for k in CANDIDATES}
        marks = "".join(f"{scores[name][k]:>16.3f}" for k in CANDIDATES)
        print(f"  {name:<16}{str(TRUTH[name]):>7}{marks}")

    print()
    for key in CANDIDATES:
        yes = [scores[n][key] for n in names if TRUTH[n]]
        no = [scores[n][key] for n in names if not TRUTH[n]]
        if not yes or not no:
            continue
        gap = min(yes) - max(no)
        print(f"  {key}: positives {min(yes):.3f}+, highest negative {max(no):.3f}, "
              f"margin {gap:+.3f}")
        if gap > 0:
            print(f"      a threshold anywhere in ({max(no):.3f}, {min(yes):.3f}) "
                  f"separates them; midpoint {(min(yes) + max(no)) / 2:.2f}")
        else:
            print("      NO clean threshold exists. Do not ship this wording.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
