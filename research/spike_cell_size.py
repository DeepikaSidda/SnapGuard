"""How big should a mesh cell be?

14px was chosen by eye, on the reasoning that screenshot text is 11-16px tall so a cell
is about one character. That held for the two captures it was measured on and then failed
on a third: the same page captured at 1024x472 instead of 1805x832 has ~8px text, so a
14px cell spans a whole line plus its leading, and dilating by one cell merges adjacent
lines into a solid block. The mesh kept 82% of the flagged area -- barely better than
painting rectangles.

The cell clearly has to scale with the content. This measures what it should be, across
three real captures at three different scales, holding the model's judgement fixed (the
flagged regions are known from real runs) so the sweep costs milliseconds rather than
minutes.

Two things to watch, because cell size trades them off:

  too large   lines merge, coverage stays high, the complaint comes back
  too small   more boxes, and eventually a stroke thinner than a cell is missed
              -- which would be a LEAK, so the leak column is the one that matters
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from snapguard.mesh import tighten_all  # noqa: E402
from snapguard.redact import redact  # noqa: E402
from snapguard.scanner import read_text_boxes  # noqa: E402
from snapguard.tiling import tile_grid  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evalset" / "out"
QUARANTINE = Path(r"C:\Users\sidda\Downloads\snapguard-quarantine")

CASES = (
    ("small light 1024x472", OUT / "_small_source.png", [6, 7, 8, 10], "dcd_live"),
    ("light 1805x832", QUARANTINE / "2026-10-23T165053.218_withhold.png",
     [6, 7, 8, 9, 10], "dcd_live"),
    ("dark editor 1920x1080", OUT / "deploy_failure.png", [3, 4, 10], "AKIA"),
)

CELLS = (4, 6, 8, 10, 14)


def main() -> int:
    for label, path, flagged, secret in CASES:
        if not path.exists():
            print(f"  skip {label}: {path.name} missing")
            continue

        image = Image.open(path).convert("RGB")
        tiles = {t.index: t for t in tile_grid(image)}
        boxes = [tiles[i].box for i in flagged if i in tiles]
        rect = redact(image, boxes, mode="block", label=None)

        print(f"\n  {label}   {len(boxes)} flagged regions, "
              f"rectangles cover {rect.fraction_of(image):.1%}")
        print(f"    {'cell':>5}{'covered':>10}{'kept':>8}{'boxes':>7}   leak")
        print("    " + "-" * 44)

        for cell in CELLS:
            meshed, shrunk = tighten_all(image, boxes, cell=cell)
            out = redact(image, meshed, mode="block", label=None)
            text = " ".join(t for t, _ in read_text_boxes(out.image))
            leaked = secret.lower() in text.lower().replace(" ", "")
            print(f"    {cell:>5}{out.fraction_of(image):>9.1%}{shrunk:>8.0%}"
                  f"{len(meshed):>7}   {'LEAK' if leaked else 'none'}")
            out.image.save(OUT / f"_cell_{cell}_{label.split()[0]}.png")

    print("\n  'kept' is the fraction of the flagged area still painted. Lower is")
    print("  better, but only while the leak column stays clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
