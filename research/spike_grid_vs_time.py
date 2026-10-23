"""Now that the mesh exists, how coarse can the grid go?

The grid went 2x2 -> 6x4 for one reason: a flagged region was the redaction, so small
regions meant small redactions. That reason no longer holds. `mesh.tighten_all` reduces a
flagged region to the cells that actually hold ink, at pixel resolution, so redaction
tightness is set by the mesh and not by the grid.

What the grid still controls is the local request count, and that is the whole latency
bill. A clean screen costs one context pass plus ceil(regions/4) coarse batches:

    6x4  24 regions   7 calls clean    ~170s measured on this CPU
    4x3  12 regions   4 calls clean
    3x2   6 regions   3 calls clean
    2x2   4 regions   2 calls clean

A user has asked for this to be faster, and 2-4 minutes a screenshot is not a product.

What the grid does still cost, and the reason this needs measuring rather than assuming:
a region is the unit of the verdict, so all the ink inside a flagged region is covered
(minus whatever the label whitelist reveals). At 2x2 one hit covers the inked parts of a
whole quarter-screen -- the file tree, the terminal, everything -- where at 6x4 it covers
a twenty-fourth. So this is a straight latency-for-coverage trade, and the question is
where it sits.

Reports calls, wall time, covered fraction and a leak check for each grid.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from aperture.decider import Decider  # noqa: E402
from aperture.mesh import tighten_all  # noqa: E402
from aperture.patterns import Policy, RegionGate, Verdict  # noqa: E402
from aperture.questions import CONTEXT_RULES, REGION_RULES  # noqa: E402
from aperture.redact import redact  # noqa: E402
from aperture.scanner import read_text_boxes, scan_text  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evalset" / "out"
QUARANTINE = Path(r"C:\Users\sidda\Downloads\aperture-quarantine")

GRIDS = ((2, 2), (3, 2), (4, 3), (6, 4))

CASES = (
    ("light (the user's case)",
     QUARANTINE / "2026-10-23T165053.218_withhold.png", "dcd_live", "allowance"),
    ("dark editor", OUT / "deploy_failure.png", "AKIA", "PoolTimeout"),
)


def main() -> int:
    decider = Decider()
    decider.require_vision()
    print(decider.banner())

    for label, path, must_go, must_stay in CASES:
        if not path.exists():
            print(f"  skip {label}: {path.name} missing")
            continue

        image = Image.open(path).convert("RGB")
        print(f"\n  {label}   {image.width}x{image.height}")
        print(f"    {'grid':<6}{'regions':>8}{'calls':>7}{'time':>8}"
              f"{'covered':>10}{'boxes':>7}   leak   kept")
        print("    " + "-" * 62)

        for columns, rows in GRIDS:
            gate = RegionGate(
                decider,
                rules=REGION_RULES,
                context_rules=CONTEXT_RULES,
                policy=Policy(fail_closed=True),
                columns=columns,
                rows=rows,
            )
            started = time.perf_counter()
            result = gate.inspect(image)
            wall = time.perf_counter() - started

            boxes = result.boxes()
            if result.verdict is Verdict.WITHHOLD:
                meshed = [(0, 0, image.width, image.height)]
            else:
                meshed, _ = tighten_all(image, boxes)
            out = redact(image, meshed, mode="block", label=None)

            text = " ".join(t for t, _ in read_text_boxes(out.image))
            flat = text.lower().replace(" ", "")
            leak = must_go.lower() in flat or bool(scan_text(text))
            kept = must_stay.lower() in flat

            print(f"    {columns}x{rows:<4}{columns * rows:>8}"
                  f"{result.decider_calls:>7}{wall:>7.0f}s"
                  f"{out.fraction_of(image):>10.2%}{len(meshed):>7}"
                  f"{'   LEAK' if leak else '   none'}   {kept}")
            out.image.save(OUT / f"_gt_{columns}x{rows}_{image.width}.png")

    print("\n  'kept' is a control string that must stay readable. A grid that leaks, or")
    print("  loses the control, is not a candidate however fast it is.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
