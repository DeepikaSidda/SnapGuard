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


def read_text_boxes(image: Image.Image) -> list[tuple[str, Box]]:
    """OCR the image into (text, box) pairs. Empty if OCR is unavailable."""
    engine = _engine()
    if engine is None:
        return []

    import numpy as np

    result, _ = engine(np.array(image.convert("RGB")))
    if not result:
        return []

    out: list[tuple[str, Box]] = []
    for entry in result:
        # rapidocr returns [box_points, text, score]
        points, text = entry[0], entry[1]
        xs = [int(p[0]) for p in points]
        ys = [int(p[1]) for p in points]
        out.append((str(text), (min(xs), min(ys), max(xs), max(ys))))
    return out


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
