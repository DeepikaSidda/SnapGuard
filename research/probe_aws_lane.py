"""Would Amazon Textract and Comprehend find what our local lanes miss?

The complaint this answers: the fast lane keeps finding nothing. On two real captures of
the author's own AWS keys, OCR plus thirteen regexes matched zero secrets while the 2B
model caught both. The model's answer is region-level, so the redaction is region-level,
and that is where the over-covering comes from. A word-level answer would let us cover a
word.

Three things under test, in order, because each one only matters if the last worked:

  1. can Textract read the key at all                 (local OCR could not)
  2. can Comprehend name it                           (our regexes could not)
  3. do the boxes land on the key and nothing else    (the actual goal)

NOTE ON WHAT THIS SENDS. This uploads the screenshot -- including the credential in it --
to Textract and Comprehend in your own AWS account. That is a deliberate trade and the
opposite of what `snapguard guard` does by default, which is why this lives in research/
and is not wired into the tool. Run it on a capture you are willing to send.

    .\\.venv\\Scripts\\python.exe research/probe_aws_lane.py <image.png>
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import io  # noqa: E402

from PIL import Image  # noqa: E402

REGION = "us-east-1"

# Textract will not look at text shorter than this, so a small capture has to be scaled
# up before it is sent. https://docs.aws.amazon.com/textract/latest/dg/limits-document.html
TEXTRACT_MIN_TEXT_PX = 15


def as_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def main(path: Path, upscale: float) -> int:
    import boto3

    image = Image.open(path).convert("RGB")
    if upscale != 1.0:
        image = image.resize(
            (round(image.width * upscale), round(image.height * upscale)),
            Image.LANCZOS,
        )
    payload = as_png(image)
    print(f"  image      : {path.name} at {image.width}x{image.height} "
          f"({len(payload) / 1024:.0f} KB, upscale {upscale}x)")

    # ---- 1. Textract: pixels -> words with boxes --------------------------------
    textract = boto3.client("textract", region_name=REGION)
    started = time.perf_counter()
    try:
        reply = textract.detect_document_text(Document={"Bytes": payload})
    except Exception as exc:  # noqa: BLE001
        print(f"  TEXTRACT FAILED: {type(exc).__name__}: {str(exc)[:200]}")
        return 1
    textract_ms = (time.perf_counter() - started) * 1000

    words = [b for b in reply["Blocks"] if b["BlockType"] == "WORD"]
    lines = [b for b in reply["Blocks"] if b["BlockType"] == "LINE"]
    print(f"  textract   : {len(words)} words, {len(lines)} lines in {textract_ms:.0f}ms")

    longest = sorted(words, key=lambda b: -len(b["Text"]))[:5]
    print("  longest tokens it read (a key should be in here):")
    for block in longest:
        text = block["Text"]
        shown = text if len(text) <= 24 else f"{text[:12]}...{text[-6:]}"
        print(f"      {len(text):>3} chars  conf {block['Confidence']:.1f}  {shown!r}")

    # ---- 2. our own regexes, now on Textract's text -----------------------------
    from snapguard.scanner import scan_text

    full_text = "\n".join(b["Text"] for b in lines)
    print(f"  our regexes on Textract's text: {scan_text(full_text) or 'nothing'}")

    # ---- 3. Comprehend: text -> named entities with offsets ----------------------
    comprehend = boto3.client("comprehend", region_name=REGION)
    started = time.perf_counter()
    try:
        pii = comprehend.detect_pii_entities(Text=full_text[:4500], LanguageCode="en")
    except Exception as exc:  # noqa: BLE001
        print(f"  COMPREHEND FAILED: {type(exc).__name__}: {str(exc)[:200]}")
        return 1
    comprehend_ms = (time.perf_counter() - started) * 1000

    entities = pii.get("Entities", [])
    print(f"  comprehend : {len(entities)} PII entities in {comprehend_ms:.0f}ms")
    for entity in entities:
        span = full_text[entity["BeginOffset"]:entity["EndOffset"]]
        shown = span if len(span) <= 20 else f"{span[:8]}...{span[-4:]}"
        print(f"      {entity['Type']:<22} conf {entity['Score']:.2f}  {shown!r}")

    if not entities:
        print("      -- nothing. Comprehend is trained on prose PII; a bare API key on")
        print("         a dashboard may not look like any of its entity types.")

    print(f"\n  total cloud latency: {textract_ms + comprehend_ms:.0f}ms, against "
          f"~170000ms for the local gate on this machine")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        raise SystemExit(2)
    scale = float(args[1]) if len(args) > 1 else 1.0
    raise SystemExit(main(Path(args[0]), scale))
