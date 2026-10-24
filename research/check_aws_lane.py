"""Does the cloud lane actually cover the key and nothing else?

`probe_aws_lane.py` established that Textract can read a key our local OCR could not, and
that Comprehend names it. That is recognition. This checks the thing that matters: do the
boxes land on the secret, is the secret gone afterwards, and is everything else still
readable.

The comparison is against the local pipeline's own best result on the same captures, so
the question is concrete -- how much of the screen does each approach cover to hide the
same key.

Sends the images to AWS. See the warning in `snapguard/aws_lane.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from snapguard.aws_lane import AwsLaneUnavailable, estimate_text_height, read  # noqa: E402
from snapguard.redact import redact  # noqa: E402
from snapguard.scanner import read_text_boxes, scan, scan_text  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evalset" / "out"
QUARANTINE = Path(r"C:\Users\sidda\Downloads\snapguard-quarantine")

CASES = (
    ("light 1805x832", QUARANTINE / "2026-10-23T165053.218_withhold.png", "dcd_live",
     "allowance"),
    ("small light 1024x472", OUT / "_small_source.png", "dcd_live", "allowance"),
    ("dark editor 1920x1080", OUT / "deploy_failure.png", "AKIA", "PoolTimeout"),
)


def main() -> int:
    for label, path, must_go, must_stay in CASES:
        if not path.exists():
            print(f"  skip {label}: {path.name} missing")
            continue

        image = Image.open(path).convert("RGB")
        print(f"\n  {label}   {image.width}x{image.height}  "
              f"(text ~{estimate_text_height(image):.0f}px)")

        # What the LOCAL deterministic lane manages on the same image, for contrast.
        local = scan(image)
        print(f"    local OCR + regex : {len(local)} secret(s) "
              f"{[s.kind for s in local] or ''}")

        try:
            reading = read(image)
        except AwsLaneUnavailable as exc:
            print(f"    AWS LANE UNAVAILABLE: {exc}")
            return 1

        print(f"    textract          : {len(reading.words)} words, upscale "
              f"{reading.upscale}x, {reading.textract_ms:.0f}ms")
        print(f"    findings          : {len(reading.secrets)}")
        for secret in reading.secrets:
            w = secret.box[2] - secret.box[0]
            h = secret.box[3] - secret.box[1]
            print(f"        {secret.kind:<34} conf {secret.confidence:.2f}  "
                  f"{w}x{h}px  {secret.redacted_sample}")

        boxes = [s.box for s in reading.secrets]
        out = redact(image, boxes, mode="block", label=None)
        text = " ".join(t for t, _ in read_text_boxes(out.image))
        flat = text.lower().replace(" ", "")

        leaked = must_go.lower() in flat or bool(scan_text(text))
        kept = must_stay.lower() in flat

        print(f"    covered           : {out.fraction_of(image):.2%} of the frame")
        print(f"    {must_go!r} recoverable  : {leaked}")
        print(f"    {must_stay!r} still readable : {kept}")
        print(f"    cost              : ${reading.cost_usd:.4f}, "
              f"{reading.total_ms:.0f}ms total")

        out.image.save(OUT / f"_aws_{label.split()[0]}_{image.width}.png")

        if leaked:
            print("    FAIL: the secret survived")
            return 1

    print("\n  Recognition is Textract + Comprehend. The DECISION is still Decider's:")
    print("  none of the above knows whether a screen may be sent anywhere.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
