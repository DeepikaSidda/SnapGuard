"""Does upscaling before the local OCR recover what Textract found?

Section 15 established two things. The thirteen regexes in `scanner.py` have no defect --
given readable text they match a key on the first try. And the local OCR could not
produce readable text from the captures that mattered, while Textract could, having first
been handed an enlarged image because it refuses text under 15px.

The obvious question was never asked: what happens if rapidocr is handed an enlarged
image too? If the answer is "the same thing", then the fast lane has been broken for a
week by one missing `resize` call, and the cloud lane is a convenience rather than the
only route to precision.

Two numbers per scale, because the OCR feeds two different consumers:

    secrets   regex hits. This is detection -- 0 is the bug being chased.
    labels    lines that pass `looks_like_ordinary_language`, which is what lets a
              heading stay visible while the key under it is covered. On a small capture
              this collapses to nothing, because the OCR cannot read the labels either.

No model and no network: this is the OCR and some regexes, so it costs seconds.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from aperture.scanner import (  # noqa: E402
    looks_like_ordinary_language,
    read_text_runs,
    scan_text,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evalset" / "out"
QUARANTINE = Path(r"C:\Users\sidda\Downloads\aperture-quarantine")

CASES = (
    ("light 1805x832", QUARANTINE / "2026-10-23T165053.218_withhold.png", "dcd_live"),
    ("small 1024x472", OUT / "_small_source.png", "dcd_live"),
    ("dark 1920x1080", OUT / "deploy_failure.png", "AKIA"),
)

SCALES = (1.0, 1.5, 2.0, 3.0)

# What the cloud lane managed on the same three, for reference.
TEXTRACT = {"light 1805x832": 1, "small 1024x472": 1, "dark 1920x1080": 4}


def main() -> int:
    for label, path, marker in CASES:
        if not path.exists():
            print(f"  skip {label}: {path.name} missing")
            continue

        original = Image.open(path).convert("RGB")
        print(f"\n  {label}   {original.width}x{original.height}"
              f"   (Textract found {TEXTRACT.get(label, '?')})")
        print(f"    {'scale':>6}{'runs':>7}{'secrets':>9}{'labels':>8}"
              f"{'marker read':>13}{'time':>8}")
        print("    " + "-" * 51)

        for scale in SCALES:
            image = original
            if scale != 1.0:
                image = original.resize(
                    (round(original.width * scale), round(original.height * scale)),
                    Image.LANCZOS,
                )
            started = time.perf_counter()
            runs = read_text_runs(image)
            elapsed = time.perf_counter() - started

            text = "\n".join(t for t, _, _ in runs)
            secrets = scan_text(text)
            labels = [
                t for t, _, s in runs
                if s >= 0.60 and looks_like_ordinary_language(t)
            ]
            # Did the OCR physically read the secret, regardless of whether a pattern
            # matched it? Separates "could not read" from "read but did not match".
            saw_marker = marker.lower() in text.lower().replace(" ", "")

            print(f"    {scale:>6.1f}{len(runs):>7}{len(secrets):>9}{len(labels):>8}"
                  f"{str(saw_marker):>13}{elapsed:>7.1f}s"
                  + (f"   {secrets}" if secrets else ""))

    print("\n  'marker read' is the honest test of the OCR alone: it asks whether the")
    print("  characters came back at all, before any pattern is applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
