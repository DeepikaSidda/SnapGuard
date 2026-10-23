"""Local recognition against AWS recognition, with Decider making the decision in both.

The gate is unchanged between the two columns: the same 2B model flags the same regions
for the same reasons. What changes is who reads the pixels inside them.

    local   rapidocr line boxes  + 13 regexes
    aws     Textract word boxes  + 13 regexes + Comprehend entity types

Four numbers per capture, and the interesting one is not the coverage:

    found       secrets located precisely. The original reason precise boxes were not
                trusted is that the local scanner matched ONE credential of four.
    covered     how much of the screen is painted to hide them.
    leak        OCR of the output, scanned for patterns. Must stay clean.
    kept        a control string that must remain readable.

Sends the images to AWS. See the warning in `aperture/aws_lane.py`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from aperture.aws_lane import AwsLaneUnavailable, read, reveal_boxes  # noqa: E402
from aperture.mesh import tighten_all  # noqa: E402
from aperture.redact import redact, resolve_boxes  # noqa: E402
from aperture.scanner import read_text_boxes, scan, scan_text  # noqa: E402
from aperture.tiling import tile_grid  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evalset" / "out"
QUARANTINE = Path(r"C:\Users\sidda\Downloads\aperture-quarantine")

# The regions the 2B model flagged on each capture, from real runs. Held fixed so this
# measures recognition and not the model's afternoon.
CASES = (
    ("light 1805x832", QUARANTINE / "2026-10-23T165053.218_withhold.png",
     [6, 7, 8, 9, 10], "dcd_live", "allowance"),
    ("small 1024x472", OUT / "_small_source.png", [6, 7, 8, 10], "dcd_live",
     "allowance"),
    ("dark editor 1920x1080", OUT / "deploy_failure.png", [3, 4, 10], "AKIA",
     "PoolTimeout"),
)


def report(label, image, boxes, found, elapsed, must_go, must_stay, cost):
    out = redact(image, boxes, mode="block", label=None)
    text = " ".join(t for t, _ in read_text_boxes(out.image))
    flat = text.lower().replace(" ", "")
    leak = must_go.lower() in flat or bool(scan_text(text))
    kept = must_stay.lower() in flat
    print(f"    {label:<9}{found:>7}{out.fraction_of(image):>10.2%}"
          f"{len(boxes):>7}{elapsed:>8.1f}s{'  LEAK' if leak else '  none'}"
          f"   kept={kept}  ${cost:.4f}")
    out.image.save(OUT / f"_vs_{label}_{image.width}.png")
    return leak, kept


def main() -> int:
    for name, path, flagged, must_go, must_stay in CASES:
        if not path.exists():
            print(f"  skip {name}: {path.name} missing")
            continue

        image = Image.open(path).convert("RGB")
        tiles = {t.index: t for t in tile_grid(image)}
        regions = [tiles[i].box for i in flagged if i in tiles]

        print(f"\n  {name}   {image.width}x{image.height}, "
              f"{len(regions)} regions flagged by Decider")
        print(f"    {'lane':<9}{'found':>7}{'covered':>10}{'boxes':>7}"
              f"{'time':>9}  leak")
        print("    " + "-" * 66)

        # Regions alone, for scale.
        report("regions", image, regions, 0, 0.0, must_go, must_stay, 0.0)

        # Local: rapidocr + regex for the hints, rapidocr lines for the reveal.
        started = time.perf_counter()
        local_hits = scan(image)
        boxes, _ = resolve_boxes(regions, [s.box for s in local_hits])
        meshed, _ = tighten_all(image, boxes)
        report("local", image, meshed, len(local_hits),
               time.perf_counter() - started, must_go, must_stay, 0.0)

        # AWS: Textract words + Comprehend entities for the hints and the reveal.
        try:
            started = time.perf_counter()
            reading = read(image)
            boxes, _ = resolve_boxes(regions, [s.box for s in reading.secrets])
            meshed, _ = tighten_all(
                image, boxes, reveal_boxes=reveal_boxes(reading)
            )
            report("aws", image, meshed, len(reading.secrets),
                   time.perf_counter() - started, must_go, must_stay,
                   reading.cost_usd)
        except AwsLaneUnavailable as exc:
            print(f"    aws lane unavailable: {exc}")

    print("\n  'found' is precisely located secrets. The local scanner matching one of")
    print("  four on the dark editor is why precise boxes were not trusted before.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
