"""The same page captured small. Does any of this still work at 1024x472?

A user re-tested the hackathon page and reported the redaction covering the whole image.
Their paste was 1024x472 where the capture I had measured was 1805x832 -- exactly 1.76x
smaller, so the same screenshot scaled down, which is what you get from a smaller browser
window or a scaled snip.

That matters more than it looks, and in three separate places:

  the grid       1024x472 fits a 6x3 grid, not 6x4, so regions are ~170x157
  the model      the key text is ~8px tall instead of ~14px
  the mesh       a glyph stroke 1px wide at this scale may not clear the gradient floor

Every number in research/README.md sections 13 and 14 was measured at 1024x576 or
1920x1080. This checks whether they survive a smaller capture, by scaling the quarantined
original and running the real gate over both sizes.
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
from aperture.scanner import read_text_boxes  # noqa: E402
from aperture.tiling import tile_grid  # noqa: E402

SOURCE = Path(
    r"C:\Users\sidda\Downloads\aperture-quarantine\2026-10-23T165053.218_withhold.png"
)
OUT = Path(__file__).resolve().parents[1] / "evalset" / "out"
SECRET = "dcd_live"


def run(image: Image.Image, label: str, decider: Decider) -> None:
    tiles = tile_grid(image)
    gate = RegionGate(
        decider,
        rules=REGION_RULES,
        context_rules=CONTEXT_RULES,
        policy=Policy(fail_closed=True),
    )

    started = time.perf_counter()
    result = gate.inspect(image)
    wall = time.perf_counter() - started

    boxes = result.boxes()
    meshed, shrunk = tighten_all(image, boxes)
    if result.verdict in (Verdict.WITHHOLD, Verdict.UNREADABLE):
        meshed = [(0, 0, image.width, image.height)]
    out = redact(image, meshed, mode="block", label=None)

    text = " ".join(t for t, _ in read_text_boxes(out.image)).lower().replace(" ", "")
    leaked = SECRET in text

    print(f"\n  {label}  {image.width}x{image.height}")
    print(f"    grid         : {len(tiles)} regions of "
          f"{tiles[0].width}x{tiles[0].height} (downscale {tiles[0].downscale:.1f}x)")
    for name, value in result.context.items():
        rule = next(r for r in CONTEXT_RULES if r.name == name)
        fired = " <-- FIRED" if value >= rule.threshold else ""
        print(f"    {name:20s}: {value:.3f}  (threshold {rule.threshold}){fired}")
    print(f"    credential   : "
          + (", ".join(f"r{f.tile.index}={f.value:.3f}" for f in result.findings)
             or "nothing fired"))
    print(f"    verdict      : {result.verdict.name}")
    print(f"    covered      : {out.fraction_of(image):.1%} of the frame "
          f"({len(meshed)} boxes, mesh kept {shrunk:.0%})")
    print(f"    key readable : {leaked}")
    print(f"    cost         : {result.decider_calls} calls, {wall:.0f}s")

    out.image.save(OUT / f"_small_{label.replace(' ', '_')}.png")


def main() -> int:
    if not SOURCE.exists():
        print(f"missing {SOURCE}")
        return 1

    full = Image.open(SOURCE).convert("RGB")
    small = full.resize((1024, 472), Image.LANCZOS)
    small.save(OUT / "_small_source.png")

    decider = Decider()
    print(decider.banner())
    run(small, "as the user captured it", decider)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
