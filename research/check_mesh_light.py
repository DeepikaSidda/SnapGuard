"""Does the mesh work on a light-themed web page, not just a dark editor?

The mesh was built and measured against dark VS Code screenshots. The first light page a
user tested -- a hackathon dashboard, cream panels on white, soft card shadows -- got no
reduction at all: every cell read as inked, the dense-region ceiling tripped, and five
flagged regions were painted as one solid bar across 24% of the frame.

This replays that exact capture with no model calls. The regions the gate flagged are
known from the run (tiles 6-10 of a 6x4 grid), so the model's judgement is held fixed
and only the pixel arithmetic is under test. That makes it a 4ms experiment instead of a
3-minute one.

Checks both themes, because a fix for one that breaks the other is not a fix.
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

CASES = (
    # path, the tile indices the gate flagged, a string that must NOT survive,
    # a string that must survive
    (
        Path(r"C:\Users\sidda\Downloads\snapguard-quarantine"
             r"\2026-10-23T165053.218_withhold.png"),
        [6, 7, 8, 9, 10],
        "dcd_live",
        "allowance",
        "light web page (hackathon dashboard)",
    ),
    (
        OUT / "deploy_failure.png",
        [3, 4, 10],
        "AKIA",
        None,
        "dark editor (1920x1080)",
    ),
)


def main() -> int:
    worst = 0.0
    for path, flagged, must_go, must_stay, label in CASES:
        if not path.exists():
            print(f"  skip  {label}: {path.name} not found")
            continue

        image = Image.open(path).convert("RGB")
        tiles = {t.index: t for t in tile_grid(image)}
        boxes = [tiles[i].box for i in flagged if i in tiles]

        meshed, shrunk = tighten_all(image, boxes)
        rect = redact(image, boxes, mode="block", label=None)
        out = redact(image, meshed, mode="block", label=None)

        text = " ".join(t for t, _ in read_text_boxes(out.image))
        leaked = must_go.lower() in text.lower().replace(" ", "")
        kept = must_stay is None or must_stay.lower() in text.lower()

        print(f"\n  {label}")
        print(f"    {image.width}x{image.height}, {len(boxes)} flagged region(s)")
        print(f"    regions only : {rect.fraction_of(image):>5.1%} of the frame")
        print(f"    after mesh   : {out.fraction_of(image):>5.1%} of the frame "
              f"({len(meshed)} boxes, kept {shrunk:.0%} of the flagged area)")
        print(f"    {must_go!r} recoverable : {leaked}")
        if must_stay:
            print(f"    {must_stay!r} still readable : {kept}")

        out.image.save(OUT / f"_mesh_{path.stem[:20]}.png")
        if leaked or not kept:
            print("    FAIL")
            return 1
        worst = max(worst, shrunk)

    print(f"\n  worst reduction across both themes: kept {worst:.0%} of the flagged area")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
