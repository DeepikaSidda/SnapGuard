"""Which text lines may be left visible inside a region the model flagged.

`mesh.tighten_all` covers every pixel of a flagged region that is not background, which
is safe and, a user pointed out four reports in, more than is needed: covering the key is
the job, covering the words "Your team's API key" above it is not.

`scanner.looks_like_ordinary_language` is the whitelist that decides. It is the only
place in this project where something inside a flagged region is deliberately left
visible, so its failure direction matters more than its accuracy:

    a secret judged ordinary   -> REVEALED. a leak.
    a label judged secret-like -> covered. a user complains it covers too much.

So the tests are asymmetric on purpose. Every credential shape is asserted to be
covered. Labels are asserted to be revealed because that is the feature, but a label
wrongly covered is a cosmetic fault and a secret wrongly revealed is not.

Pure string logic, no OCR engine and no model needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aperture.scanner import looks_like_ordinary_language as ordinary  # noqa: E402


# ---- must be covered: anything that could be a secret ------------------------

SECRETS = [
    "AKIAIOSFODNN7EXAMPLE",
    "AKIAEXAMPLEKEYID1234",
    "dcd_live_EXAMPLEa1B2c3D4e5F6g7H8i9J0kL1mN2oP3qR4sT5uV6wX7",
    "EXAMPLEKEYa1B2c3D4e5F6g7H8i9J0kL1mN2oP3q",
    "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI",
    "wJalrXUtnFEMI",
    "/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "STRIPE_SECRET_KEY=sk_live_51H8zQ2eZvKY",
    "sk_test_REDACTEDFIXTURE",
    "DATABASE_URL=postgres://admin:Hunter2!",
    "@db.acme.internal:5432/payments",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
    "ghp_EXAMPLE000000000000000000000000000",
    "Password: hunter2xyzQ",
    # The dangerous one: a secret sitting in the middle of a sentence. A test that
    # tolerated a fraction of odd tokens would reveal this whole line.
    "Your AWS key is AKIAIOSFODNN7EXAMPLE now",
]


@pytest.mark.parametrize("text", SECRETS)
def test_secret_shapes_are_never_revealed(text: str):
    assert not ordinary(text), f"would have been left visible: {text!r}"


# ---- should be revealed: labels and ordinary interface text -------------------

LABELS = [
    "Your team's API key",
    "Lets your team use the AI models the user group hosts for this hackathon, for "
    "free. Everyone on the team shares this one key.",
    "Monthly allowance",
    "Each question the Decider answers uses one.",
    "requests per minute",
    "characters per request",
    "Resets on 1 Nov",
    "Free plan",
    "0 of 2,000 answers used",
    "8,000",
    "Keep it private, like a password: don't post it in chats or commit it to GitHub.",
    "Access key ID",
    "do not commit",
    "production.env",
    "Copy",
    "Hide",
    "MEMBERS",
]


@pytest.mark.parametrize("text", LABELS)
def test_labels_are_revealed(text: str):
    assert ordinary(text), f"would have been covered: {text!r}"


# ---- the OCR glitch that made this rule too strict ---------------------------


def test_one_misread_short_word_does_not_condemn_a_sentence():
    """The real failure. OCR read 'API' as 'APl' and the whole line was covered.

    A per-token veto means one bad glyph anywhere in two hundred characters hides the
    lot. Short uppercase words are exactly where OCR confuses I, l and 1.
    """
    glitched = (
        "Lets your team use the Al models the user group hosts for this hackathon, "
        "for free. Everyone on the team shares this one key. Paste it when the kit's "
        "setup asks for your AWS User Group Madurai APl key."
    )
    assert ordinary(glitched)


@pytest.mark.parametrize(
    "token", ["APl", "ID", "Al", "GitHub", "PayPal", "macOS", "iPhone", "JavaScript"]
)
def test_product_names_and_ocr_noise_are_tolerated(token: str):
    """One or two stray capitals is a product name, not key material.

    `GitHub` is the case that matters: it appears in the warning text on the very page a
    user was testing, and an earlier version of this rule covered that whole sentence.
    """
    assert ordinary(f"the {token} field")


@pytest.mark.parametrize("token", ["wJalrXUtnFEMI", "eZvKYlo2C", "xQ2eZvKYlo"])
def test_scattered_capitals_still_veto(token: str):
    """Three or more stray capitals is entropy, and gets the line covered."""
    assert not ordinary(f"the {token} field")


def test_known_limitation_a_short_all_caps_token_is_revealed():
    """Documented, not fixed. Asserted so it cannot change silently.

    An all-capitals alphabetic token of 14 characters or fewer reads as ordinary
    language, because that is what `MEMBERS`, `TERMINAL` and `ACHIEVEMENTS` look like
    and all three are on screens this tool is pointed at. A truncated key fragment like
    `AKIAIOSFODNN` is indistinguishable from them by shape alone.

    Why it is tolerable: no real credential is that short. An AWS access key ID is
    exactly 20 characters and a secret key 40, both past the length veto. The regex fast
    lane also matches key formats independently and its boxes are unioned in, never
    substituted. A twelve-character run of letters is not a usable secret.
    """
    assert ordinary("the AKIAIOSFODNN field")
    assert not ordinary("AKIAIOSFODNN7EXAMPLE"), "the real 20-char key must be covered"


# ---- edges -------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", " ", "x", "  \t "])
def test_nothing_readable_is_not_revealed(text: str):
    """Fail closed: if there is no readable text, there is no reason to reveal."""
    assert not ordinary(text)


# ---- the confidence floor for short words -----------------------------------


def test_short_pure_letter_words_get_a_lower_floor():
    """Measured: Textract read 'AWS' at 50% and 'Al' at 73%, both correctly.

    Short uppercase words are where OCR is least certain, so a 90% floor stamped a black
    square over one word of a heading. Twice.
    """
    from aperture.scanner import confidence_floor_for

    for token in ("AI", "AWS", "ID", "OK", "Copy", "Hide", "Free"):
        assert confidence_floor_for(token, 90.0) < 90.0, token


def test_anything_with_a_digit_keeps_the_full_floor():
    """The safety argument. A short secret -- a PIN, a door code -- has digits in it."""
    from aperture.scanner import confidence_floor_for

    for token in ("1234", "0000", "a1b2", "99", "sk_1", "pin7"):
        assert confidence_floor_for(token, 90.0) == 90.0, token


def test_long_words_keep_the_full_floor():
    from aperture.scanner import confidence_floor_for

    for token in ("allowance", "hunter2xyz", "wJalrXUtnFEMI"):
        assert confidence_floor_for(token, 90.0) == 90.0, token


def test_a_phrase_keeps_the_full_floor():
    """The relaxation is for a single short token, not for any short line."""
    from aperture.scanner import confidence_floor_for

    assert confidence_floor_for("a b", 90.0) == 90.0
    assert confidence_floor_for("Your team's API key", 90.0) == 90.0


def test_the_floor_is_never_raised():
    """A caller passing a low default must not have it pushed up by this."""
    from aperture.scanner import confidence_floor_for

    assert confidence_floor_for("AI", 30.0) == 30.0


# ---- filenames and identifiers ----------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Deepu_accessKeys .csv",
        "production.env",
        "aperture_gate.egg-info",
        "test_ordinary_language.py",
        "pyproject.toml",
        "DEMO_SCRIPT.md",
        "requirements-dev.txt",
        "src/aperture/mesh.py",
        "looks_like_ordinary_language",
        "MAX_FRACTION",
        "read_text_runs",
    ],
)
def test_filenames_and_identifiers_are_revealed(text: str):
    """A user spotted this: the tab name `Deepu_accessKeys .csv` was covered while the
    identical filename stayed visible in the sidebar beside it.

    The length veto is a proxy for entropy and a filename beats it by being long. Judging
    the parts between separators keeps the veto pointed at key material, which does not
    break into short word-like pieces.
    """
    assert ordinary(text), f"would have been covered: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "sk_test_REDACTEDFIXTURE",
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI",
        "dcd_live_EXAMPLEa1B2c3D4e5F6g7H8i9J0kL1mN2oP3qR4sT5uV6wX7",
        "/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "ghp_EXAMPLE000000000000000000000000000",
        "xoxb-EXAMPLE-0000-000000000",
    ],
)
def test_separators_do_not_smuggle_a_key_through(text: str):
    """The risk the split introduces, asserted directly.

    Splitting on separators must not turn a key into a list of harmless pieces. It does
    not, because at least one piece of real key material is always long, mixed
    letters-and-digits, or scattered with capitals.
    """
    assert not ordinary(text), f"would have been revealed: {text!r}"
