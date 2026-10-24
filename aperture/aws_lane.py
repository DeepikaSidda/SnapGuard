"""The cloud recognition lane: Amazon Textract for words, Amazon Comprehend for names.

WHY THIS EXISTS, AND WHAT IT COST TO LEARN.

Aperture had two lanes. A deterministic one -- OCR plus thirteen regexes -- and the 2B
model. On two real captures of the author's own AWS keys the deterministic lane matched
*nothing* while the model caught both, and `ARTICLE.md` drew the obvious conclusion: that
hand-written patterns lose to a model on real data.

That conclusion was wrong, and this module is how it was found out. Run the same capture
through Textract and the key comes back as a 57-character token, and the existing regex
matches it on the first try. **The patterns were never the problem. The OCR was.**
rapidocr could not read the glyphs, so the regexes were being handed an empty string and
blamed for not finding anything in it.

Which leaves a real gap worth filling with a real service:

    the model says      "a credential is somewhere in region 8"     -> a region to cover
    this lane says      "characters 412-469 of that line"           -> a word to cover

Region-level answers are the whole reason the redaction covers too much. A word-level
answer covers a word.

THE DIVISION OF LABOUR, STATED PLAINLY.

Comprehend names what a span of text is. It does not and cannot decide whether a screen
may be sent somewhere, because that is not a property of the text. On the first capture
it was tried on, it labelled "Madurai" an ADDRESS at 0.67 -- part of the phrase "AWS User
Group Madurai" -- which is the same over-flagging that both Nova models showed on the
eval set. So:

    recognition   Textract + Comprehend   what is on screen, and exactly where
    decision      Strands Decider 2B      whether it may leave, and whether it is wanted

"Is this a private conversation", "is this an identity document", "does this region bear
on what the user asked" are not entity-extraction questions. Decider keeps them.

WHAT THIS SENDS, WHICH IS THE POINT TO BE HONEST ABOUT.

Everything. The whole screenshot, credentials included, goes to Textract. That is the
exact opposite of what the clipboard guard does by default, and it is why this lane is
opt-in behind `--aws-lane` and off unless asked for.

It is not incoherent, it is a different threat model, and a common one: *I trust my own
AWS account; I do not trust the third-party AI tool I am about to paste this into.* Under
that model this lane is free precision. Under the stricter model -- nothing leaves this
machine -- it is unacceptable, and that is the default.

Two facts that shape the code:

  Textract ignores text under 15px tall. A 1024x472 capture has ~8px text, so small
  captures are scaled up before sending or the service reads nothing. This is very
  probably the same reason the local OCR failed.
  https://docs.aws.amazon.com/textract/latest/dg/limits-document.html

  DetectDocumentText is billed per page and Comprehend per 100 characters, so a capture
  costs a fraction of a cent -- but it is not $0.00, and every other number in this
  project is. Reported per run so it cannot hide.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Sequence

from PIL import Image

from .scanner import PATTERNS, Secret, _mask

Box = tuple[int, int, int, int]

# Below this, Textract will not see the text at all, so the image is scaled up until the
# estimated text height clears it. Measured against a capture whose text was ~8px.
MIN_TEXT_PX = 15

# Entity types worth acting on. Comprehend returns plenty that are not interesting on a
# screenshot -- DATE_TIME fires on "1 Nov" and on every log timestamp -- and a redaction
# that covers the clock is the over-covering complaint in a new costume.
#
# Mapped to Aperture's own rule names so the rest of the pipeline does not have to learn
# Comprehend's vocabulary.
ENTITY_KINDS: dict[str, str] = {
    "AWS_ACCESS_KEY": "credential",
    "AWS_SECRET_KEY": "credential",
    "PASSWORD": "credential",
    "CREDIT_DEBIT_NUMBER": "credential",
    "CREDIT_DEBIT_CVV": "credential",
    "PIN": "credential",
    "BANK_ACCOUNT_NUMBER": "credential",
    "BANK_ROUTING": "credential",
    "SSN": "identity",
    "PASSPORT_NUMBER": "identity",
    "DRIVER_ID": "identity",
    "LICENSE_PLATE": "identity",
    "EMAIL": "personal_contact",
    "PHONE": "personal_contact",
    "ADDRESS": "personal_contact",
    "NAME": "personal_contact",
}

# Comprehend is confident when it is right about credentials (0.96 on the capture this
# was built against) and much less so on the ones it invents (0.67 on "Madurai"). A floor
# in between keeps the first and drops the second. Deliberately higher for the chattier
# entity types, which fire on ordinary interface text.
MIN_SCORE = 0.80
MIN_SCORE_BY_KIND = {"personal_contact": 0.90}


@dataclass
class Word:
    """One word Textract read, with where it sits in the image and in the text."""

    text: str
    box: Box
    confidence: float
    start: int
    end: int


@dataclass
class Reading:
    """What the lane found, and what it cost."""

    words: list[Word]
    text: str
    secrets: list[Secret]
    textract_ms: float
    comprehend_ms: float
    cost_usd: float
    upscale: float

    @property
    def total_ms(self) -> float:
        return self.textract_ms + self.comprehend_ms


class AwsLaneUnavailable(RuntimeError):
    """boto3 missing, no credentials, or the service refused."""


def _scale_for(image: Image.Image, estimated_text_px: float) -> float:
    """How much to enlarge so Textract can see the text.

    Text height is estimated from the image height rather than measured, because
    measuring it needs the OCR that is about to run. A 1080p screen has ~14px UI text,
    and the ratio holds across the scaled captures this was tested on.
    """
    if estimated_text_px >= MIN_TEXT_PX:
        return 1.0
    # A little past the floor rather than exactly on it: 15px is where detection starts,
    # not where it is reliable.
    return min(4.0, round(MIN_TEXT_PX * 1.4 / max(1.0, estimated_text_px), 2))


def estimate_text_height(image: Image.Image) -> float:
    """Rough UI text height for an image of this size. ~14px on a 1080p screen."""
    return max(1.0, image.height * 14.0 / 1080.0)


def read(
    image: Image.Image,
    *,
    region_name: str = "us-east-1",
    use_comprehend: bool = True,
    min_score: float = MIN_SCORE,
) -> Reading:
    """Send `image` to Textract, name what came back, and return boxes in image pixels.

    Raises `AwsLaneUnavailable` rather than returning nothing, because a lane that fails
    silently is worse than no lane: the caller would treat "the service was unreachable"
    as "there are no secrets here".
    """
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:  # pragma: no cover
        raise AwsLaneUnavailable("boto3 is not installed") from exc

    upscale = _scale_for(image, estimate_text_height(image))
    sent = image.convert("RGB")
    if upscale != 1.0:
        sent = sent.resize(
            (round(sent.width * upscale), round(sent.height * upscale)), Image.LANCZOS
        )

    buffer = io.BytesIO()
    sent.save(buffer, format="PNG")
    payload = buffer.getvalue()

    import time

    try:
        textract = boto3.client("textract", region_name=region_name)
        started = time.perf_counter()
        reply = textract.detect_document_text(Document={"Bytes": payload})
        textract_ms = (time.perf_counter() - started) * 1000
    except (BotoCoreError, ClientError) as exc:
        raise AwsLaneUnavailable(f"Textract: {exc}") from exc

    words, text = _assemble(reply, sent.size, upscale)

    secrets: list[Secret] = []
    # The deterministic patterns first, on text that is finally readable. This is the
    # lane that was being blamed for failures that belonged to the OCR.
    secrets += _regex_hits(words, text)

    comprehend_ms = 0.0
    cost = 0.0015  # DetectDocumentText, per page
    if use_comprehend and text.strip():
        try:
            client = boto3.client("comprehend", region_name=region_name)
            started = time.perf_counter()
            # The API takes a bounded amount of text; a screenshot rarely approaches it.
            pii = client.detect_pii_entities(Text=text[:4500], LanguageCode="en")
            comprehend_ms = (time.perf_counter() - started) * 1000
        except (BotoCoreError, ClientError) as exc:
            raise AwsLaneUnavailable(f"Comprehend: {exc}") from exc
        secrets += _entity_hits(words, text, pii.get("Entities", []), min_score)
        units = max(3, -(-len(text[:4500]) // 100))  # 300-character minimum
        cost += units * 0.0001

    return Reading(
        words=words,
        text=text,
        secrets=_dedupe(secrets),
        textract_ms=textract_ms,
        comprehend_ms=comprehend_ms,
        cost_usd=cost,
        upscale=upscale,
    )


def _assemble(
    reply: dict, sent_size: tuple[int, int], upscale: float
) -> tuple[list[Word], str]:
    """Textract blocks -> words with pixel boxes in the ORIGINAL image, plus the text.

    Offsets are tracked per word while the text is built, so a character span from
    Comprehend can be turned back into pixels. Reconstructing the offsets afterwards by
    searching the text for the word would mismatch on any word that appears twice.

    Geometry comes back normalised to the image that was sent, so it is multiplied by
    that image's size and then divided by `upscale` to land on the original.
    """
    blocks = {b["Id"]: b for b in reply.get("Blocks", [])}
    width, height = sent_size

    def to_pixels(block: dict) -> Box:
        geometry = block["Geometry"]["BoundingBox"]
        left = geometry["Left"] * width / upscale
        top = geometry["Top"] * height / upscale
        return (
            int(left),
            int(top),
            int(left + geometry["Width"] * width / upscale),
            int(top + geometry["Height"] * height / upscale),
        )

    words: list[Word] = []
    pieces: list[str] = []
    cursor = 0

    for block in reply.get("Blocks", []):
        if block["BlockType"] != "LINE":
            continue
        children = [
            blocks[i]
            for relationship in block.get("Relationships", [])
            if relationship["Type"] == "CHILD"
            for i in relationship["Ids"]
            if blocks.get(i, {}).get("BlockType") == "WORD"
        ]
        if not children:
            continue
        for index, child in enumerate(children):
            if index:
                pieces.append(" ")
                cursor += 1
            token = child["Text"]
            words.append(
                Word(
                    text=token,
                    box=to_pixels(child),
                    confidence=float(child.get("Confidence", 0.0)),
                    start=cursor,
                    end=cursor + len(token),
                )
            )
            pieces.append(token)
            cursor += len(token)
        pieces.append("\n")
        cursor += 1

    return words, "".join(pieces)


def _boxes_for_span(words: Sequence[Word], start: int, end: int) -> list[Box]:
    """Every word box overlapping the character span [start, end)."""
    return [w.box for w in words if w.start < end and w.end > start]


def _union(boxes: Sequence[Box]) -> Box | None:
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _regex_hits(words: Sequence[Word], text: str) -> list[Secret]:
    """Aperture's own thirteen patterns, against text Textract could actually read."""
    out: list[Secret] = []
    for kind, pattern in PATTERNS:
        for match in pattern.finditer(text):
            # The captured group when there is one: it is the secret rather than the
            # label in front of it, so the box lands on the value.
            index = match.lastindex or 0
            box = _union(_boxes_for_span(words, match.start(index), match.end(index)))
            if box:
                out.append(
                    Secret(
                        kind=kind,
                        redacted_sample=_mask(match.group(index)),
                        box=_pad(box),
                        confidence=1.0,
                    )
                )
    return out


def _entity_hits(
    words: Sequence[Word],
    text: str,
    entities: Sequence[dict],
    min_score: float,
) -> list[Secret]:
    out: list[Secret] = []
    for entity in entities:
        kind = ENTITY_KINDS.get(entity.get("Type", ""))
        if not kind:
            continue
        floor = MIN_SCORE_BY_KIND.get(kind, min_score)
        if float(entity.get("Score", 0.0)) < floor:
            continue
        start, end = int(entity["BeginOffset"]), int(entity["EndOffset"])
        # Spans of three characters or fewer are noise. Measured, Comprehend labelled a
        # lone "5" -- the port number in `:5432/payments` -- an AWS_SECRET_KEY at 1.00,
        # which earns a 14x17px box over a digit and nothing else.
        if end - start <= 3:
            continue
        box = _union(_boxes_for_span(words, start, end))
        if box:
            out.append(
                Secret(
                    kind=f"{kind}:{entity['Type'].lower()}",
                    # Masked like every other finding. Comprehend hands back the span
                    # verbatim and writing it into a log would make this module the leak.
                    redacted_sample=_mask(text[start:end]),
                    box=_pad(box),
                    confidence=float(entity.get("Score", 0.0)),
                )
            )
    return out


def reveal_boxes(reading: Reading) -> list[Box]:
    """Word boxes safe to leave visible, for `mesh.tighten_all` to carve out.

    This is the same whitelist the local pipeline uses -- reveal only what reads as
    ordinary language -- with a far better source under it. Two differences matter:

      WORDS, NOT LINES.  rapidocr gives a line box, so `DATABASE_URL=postgres://admin:
      Hunter2!` is one unit and has to be covered whole. Textract gives every word
      separately, so the label can stay and the value can go.

      IT CAN ACTUALLY READ.  The local OCR returned nothing at all on two captures of
      real AWS keys. An unreadable secret is not revealed either way -- nothing unread
      lands in a reveal box -- but it does mean the local lane reveals almost nothing on
      a small capture, because it cannot confidently read the labels either.

    Words Textract read with low confidence are not revealed. On the capture this was
    built against the credential itself came back at 55% confidence while every ordinary
    word was above 99%, so the confidence floor is doing real work here and not just
    paperwork.
    """
    from .scanner import confidence_floor_for, looks_like_ordinary_language

    return [
        word.box
        for word in reading.words
        if word.confidence >= confidence_floor_for(word.text, 90.0)
        and looks_like_ordinary_language(word.text)
    ]


def _pad(box: Box, amount: int = 3) -> Box:
    """Word boxes hug the glyphs, and a redaction that hugs the glyphs looks broken."""
    return (box[0] - amount, box[1] - amount, box[2] + amount, box[3] + amount)


def _dedupe(secrets: Sequence[Secret]) -> list[Secret]:
    """One box per location, keeping the first lane that found it.

    A key found by both the regex and Comprehend is one secret, not two, and drawing two
    labels over it would misreport the count.
    """
    out: list[Secret] = []
    for secret in secrets:
        if not any(_close(secret.box, kept.box) for kept in out):
            out.append(secret)
    return out


def _close(a: Box, b: Box, slack: int = 6) -> bool:
    return all(abs(a[i] - b[i]) <= slack for i in range(4))
