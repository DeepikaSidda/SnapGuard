"""The fast lane: OCR plus regex, for secrets that have a known shape.

This module exists because of an honest admission. For finding an AWS access key, a
regex beats a 2B vision model outright. `AKIA` followed by sixteen uppercase alphanumerics
is a fixed, documented format. A pattern match over OCR'd text is faster, exact, returns
coordinates, and never has a confidence of 0.6.

So the two halves of Aperture are split along the line of what each is actually good at:

    code     things with a known shape - keys, tokens, card numbers, DSNs
    Decider  things with no shape      - "this is a private conversation",
                                         "this is somebody's payroll page"

A regex cannot answer the second kind, and a decision model should not be wasted on the
first. Nothing here needs a model, a network, or a GPU, and it finishes in milliseconds,
which is what makes the clipboard hold short enough to live with.

OCR is optional. Without it the gate still works -- it just loses the exact localisation
and leans entirely on Decider.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Sequence

from PIL import Image

Box = tuple[int, int, int, int]


@dataclass(frozen=True)
class Secret:
    """A structured secret found in the image, with where it is."""

    kind: str
    # Never the secret itself. Storing the matched text would turn our own log into the
    # leak we are preventing.
    redacted_sample: str
    box: Box
    confidence: float = 1.0

    def __str__(self) -> str:
        return f"{self.kind} at {self.box}"


# --------------------------------------------------------------------------- patterns
#
# Each pattern is anchored enough to avoid firing on ordinary prose. The cost of a false
# positive here is a blacked-out region, so they are allowed to be slightly eager -- but
# not so eager that every hex string in a stack trace trips them.

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    ("aws_secret_access_key", re.compile(
        r"(?i)aws_secret_access_key\s*[=:]\s*[\'\"]?([A-Za-z0-9/+=]{40})")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("stripe_secret_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("private_key_header", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY")),
    ("connection_string", re.compile(
        r"\b(?:postgres|postgresql|mysql|mongodb(?:\+srv)?|redis|amqp)://"
        r"[^\s:/@]+:[^\s:/@]+@[^\s/]+")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}=*")),
    ("password_assignment", re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\s*[=:]\s*"
        r"[\'\"]?([^\s\'\"]{8,})")),
    # The community decider API's own key format, since this project handles one.
    ("decider_api_key", re.compile(r"\bdcd_live_[A-Za-z0-9]{20,}\b")),
)


def _mask(text: str) -> str:
    """A sample safe to log: first four characters, then the length."""
    head = text[:4]
    return f"{head}...({len(text)} chars)"


# --------------------------------------------------------------------------- OCR


@lru_cache(maxsize=1)
def _engine():
    """Load the OCR engine once, or return None if it is not installed.

    rapidocr-onnxruntime is used rather than Tesseract because it installs from pip with
    no system binary. For a pattern meant to be dropped into someone else's project,
    "pip install" versus "install this .exe first" decides whether they bother.
    """
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return None
    try:
        return RapidOCR()
    except Exception:  # noqa: BLE001 - a broken model download should not be fatal
        return None


def ocr_available() -> bool:
    return _engine() is not None


def read_text_runs(image: Image.Image) -> list[tuple[str, Box, float]]:
    """OCR the image into (text, box, confidence) triples."""
    engine = _engine()
    if engine is None:
        return []

    import numpy as np

    result, _ = engine(np.array(image.convert("RGB")))
    if not result:
        return []

    out: list[tuple[str, Box, float]] = []
    for entry in result:
        # rapidocr returns [box_points, text, score]
        points, text = entry[0], entry[1]
        score = float(entry[2]) if len(entry) > 2 else 0.0
        xs = [int(p[0]) for p in points]
        ys = [int(p[1]) for p in points]
        out.append((str(text), (min(xs), min(ys), max(xs), max(ys)), score))
    return out


def read_text_boxes(image: Image.Image) -> list[tuple[str, Box]]:
    """OCR the image into (text, box) pairs. Empty if OCR is unavailable."""
    return [(text, box) for text, box, _ in read_text_runs(image)]


def looks_like_ordinary_language(text: str) -> bool:
    """Is this a label a person wrote, rather than something a generator produced?

    Used to decide which lines inside a flagged region may stay visible. The direction
    of this test is the whole safety argument, so it is worth being explicit: it is a
    WHITELIST. A line is revealed only if this returns True. Anything OCR could not read,
    read with low confidence, or read as something that does not look like language stays
    covered, which means every new failure mode defaults to covered.

    What it rejects, and why each one matters:

      a long unbroken token          a key, a token, a hash, a URL with a secret in it
      letters mixed with digits      the shape of nearly every generated credential
      capitals inside a word         `sk_test_REDACTEDFIXTURE`, `AKIAEXAMPLEKEY`
      mostly punctuation or symbols  base64 and hex tails

    What it is not: a judgement about whether the *content* is sensitive. "Your team's
    API key" is revealed because it is a sentence, not because this decided the heading
    is harmless. A short low-entropy secret -- a four digit PIN, a password like
    `hunter2` -- reads as ordinary language and WOULD be revealed. That is the real cost
    of this feature and the reason `--cover-labels` exists to turn it off.
    """
    stripped = text.strip()
    if len(stripped) < 2:
        return False

    tokens = [t for t in stripped.split() if t]
    if not tokens:
        return False

    for token in tokens:
        outer = token.strip(".,:;!?()[]{}<>'\"`|/\\-_=+*%&#@~^·•")
        if not outer:
            continue

        # Plain numbers, including thousands separators and decimals, are fine.
        if not any(c.isalpha() for c in outer) and outer.replace(",", "").replace(
            ".", ""
        ).isdigit():
            continue

        # Judge the PARTS between separators, not the whole run.
        #
        # The vetoes below are all proxies for entropy, and a filename defeats every one
        # of them by being long: `Deepu_accessKeys` is sixteen characters, so the length
        # test covered it -- while the identical filename stayed visible in the editor's
        # sidebar, because that region was not flagged. A user spotted the inconsistency,
        # which is the right way to notice that a rule is arbitrary.
        #
        # Splitting first keeps every veto working on the thing it was written for, since
        # key material does not break into short word-like pieces:
        #
        #   Deepu_accessKeys .csv        -> Deepu | accessKeys | csv       all fine
        #   sk_test_REDACTEDFIXTURE     -> sk | live | 51H8xQ2eZvKYlo2C    last rejected
        #   AWS_SECRET_ACCESS_KEY=wJa..  -> ... | wJalrXUtnFEMI            stray capitals
        #   AKIAIOSFODNN7EXAMPLE         -> one part, 20 chars             length
        #
        # It also stops covering ordinary snake_case identifiers in source code, which
        # was the same fault wearing different clothes.
        for core in re.split(r"[._/\-]+", outer):
            if not core or not _part_looks_like_language(core):
                return False

    return True


def _part_looks_like_language(core: str) -> bool:
    """One separator-free piece of a token. See `looks_like_ordinary_language`."""
    letters = sum(c.isalpha() for c in core)
    digits = sum(c.isdigit() for c in core)

    # A bare number between separators -- a version, a port, a date part.
    if not letters:
        return core.replace(",", "").isdigit()

    if len(core) > 14:
        return False
    if digits and len(core) > 5:
        return False
    if letters / len(core) < 0.6:
        return False

    # Scattered capitals, which is the shape of `wJalrXUtnFEMI`. Counted rather than
    # merely detected, because two earlier versions of this rule were both wrong:
    #
    #   "any capital after the first" rejected `GitHub`, `PayPal` and `macOS`, so the
    #   sentence "don't commit it to GitHub" was covered as a suspected secret.
    #
    #   adding an exemption for short tokens did not help either, because the thing
    #   being measured was never length. `API` misread by OCR as `APl` has one stray
    #   capital; a key has seven.
    #
    # So: up to two capitals outside the first position is a product name, more is
    # entropy. Everything longer is already rejected above -- over 14 characters by
    # length, letters mixed with digits by the second test, base64 and hex tails by the
    # third -- so this only has to separate CamelCase words from key material.
    strays = sum(1 for c in core[1:] if c.isupper())
    if strays > 2 and core != core.upper():
        return False

    return True


def ordinary_language_boxes(
    image: Image.Image,
    *,
    min_confidence: float = 0.60,
) -> list[Box]:
    """Boxes of the text lines that may be left visible inside a flagged region.

    The box is OCR's own, untrimmed. An earlier version pulled each one in by two pixels
    on the theory that a box overhanging into the next line would leave two pixels of a
    credential showing. It backfired: the consumer suppresses ink inside these boxes, so
    trimming left a two-pixel ring of every label's glyphs outside the box, still inked,
    which was enough to rebuild a bar over the whole line. Trimming made labels *more*
    covered, not less. The slack belongs at the other end -- see `mesh.ink_mask`.
    """
    return [
        box
        for text, box, score in read_text_runs(image)
        if score >= confidence_floor_for(text, min_confidence)
        and looks_like_ordinary_language(text)
    ]


# Below this many characters, a pure-letter token gets a lower confidence bar. See
# `confidence_floor_for`.
SHORT_TOKEN = 4
SHORT_TOKEN_CONFIDENCE = 55.0


def confidence_floor_for(text: str, default: float) -> float:
    """How confidently a line must have been read before it may be left visible.

    Short words are where OCR is least sure, and the default floor was covering them.
    Measured on a real capture, with the floor at 90:

        'AWS'  read at 50.0, correct, covered
        'Al'   read at 73.2, correct, covered   (the page says "AI")
        every ordinary long word   above 99

    So a heading kept a black square stamped over one word of it, twice, which is the
    over-covering complaint at its smallest scale.

    The relaxation is restricted to tokens of four characters or fewer that are *all
    letters*, and that restriction is the entire safety argument. The thing being
    protected against is a short low-entropy secret -- a PIN, a door code, a short
    password -- and those contain digits or symbols, so they keep the full floor. What
    this lets through is `AI`, `AWS`, `ID`, `OK`, `No`, `Copy`, `Hide`: four letters of
    pure alphabet is 457,000 possibilities and nothing anybody issues as a credential.

    Still a weakening, and the honest version of the limitation already in §14: a
    four-letter all-alphabetic password like `pass` would now be revealed at 55%
    confidence where before it needed 90%. `--cover-labels` turns the whole mechanism
    off.
    """
    tokens = [t for t in text.split() if t]
    if (
        len(tokens) == 1
        and len(tokens[0]) <= SHORT_TOKEN
        and tokens[0].isalpha()
    ):
        return min(default, SHORT_TOKEN_CONFIDENCE)
    return default


# --------------------------------------------------------------------------- the scan


def scan(image: Image.Image, *, pad: int = 6) -> list[Secret]:
    """Find structured secrets in `image` and return them with boxes.

    `pad` grows each box slightly, because OCR boxes hug the glyphs and a redaction that
    hugs the glyphs looks like a mistake.
    """
    findings: list[Secret] = []
    for text, box in read_text_boxes(image):
        for kind, pattern in PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            # Prefer the captured group when the pattern has one: it is the secret
            # itself rather than the label in front of it.
            matched = match.group(match.lastindex or 0)
            findings.append(
                Secret(
                    kind=kind,
                    redacted_sample=_mask(matched),
                    box=_grow(box, pad, image.size),
                )
            )
            break  # one finding per text run is enough to condemn it
    return findings


def scan_text(text: str) -> list[str]:
    """Pattern kinds present in a plain string. Used by the tests and the benchmark."""
    return [kind for kind, pattern in PATTERNS if pattern.search(text)]


def boxes_of(secrets: Sequence[Secret]) -> list[Box]:
    return [s.box for s in secrets]


def _grow(box: Box, pad: int, size: tuple[int, int]) -> Box:
    width, height = size
    left, top, right, bottom = box
    return (
        max(0, left - pad),
        max(0, top - pad),
        min(width, right + pad),
        min(height, bottom + pad),
    )
