"""Confirm the shipped defaults behave as the grid measurement predicted.

spike_grid.py compared grids by constructing a RegionGate for each one. This runs the
gate the way the CLI and the clipboard guard construct it -- no arguments, nothing
pinned -- on the same screen a user reported as over-redacted, and checks the two things
that have to hold together:

    the covered area is small          (the complaint)
    no credential survives in it       (the point)

Run with the vision server on :8099.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aperture.decider import Decider  # noqa: E402
from aperture.mesh import tighten_all  # noqa: E402
from aperture.patterns import Policy, RegionGate, Verdict  # noqa: E402
from aperture.questions import CONTEXT_RULES, REGION_RULES  # noqa: E402
from aperture.redact import redact  # noqa: E402
from aperture.scanner import read_text_boxes, scan_text  # noqa: E402
from aperture.tiling import DEFAULT_COLUMNS, DEFAULT_ROWS, tile_grid  # noqa: E402
from spike_grid import draw  # noqa: E402


def main() -> int:
    screen = draw()
    decider = Decider()
    print(decider.banner())

    gate = RegionGate(
        decider,
        rules=REGION_RULES,
        context_rules=CONTEXT_RULES,
        policy=Policy(fail_closed=True),
    )
    assert (gate.columns, gate.rows) == (DEFAULT_COLUMNS, DEFAULT_ROWS)

    tiles = tile_grid(screen)
    print(f"\n  screen        : {screen.width}x{screen.height}")
    print(f"  grid          : {gate.columns}x{gate.rows} "
          f"({len(tiles)} regions of {tiles[0].width}x{tiles[0].height}, "
          f"downscale {tiles[0].downscale:.1f}x)")

    started = time.perf_counter()
    result = gate.inspect(screen)
    wall = time.perf_counter() - started

    regions = result.boxes()
    meshed, shrunk = tighten_all(screen, regions)

    rect = redact(screen, regions, mode="block")
    out = redact(screen, meshed, mode="block")

    text = " ".join(t for t, _ in read_text_boxes(out.image))
    leak = scan_text(text) or ("AKIAWBDM" in text)
    tree_kept = "pyproject" in text or "LICENSE" in text
    terminal_kept = "doctor" in text or "aperture" in text

    print(f"  verdict       : {result.verdict.name}")
    print(f"  regions fired : {len(result.flagged)} of {len(tiles)}")
    print(f"  local calls   : {result.decider_calls} in {wall:.0f}s")
    print(f"  regions only  : {rect.fraction_of(screen):.0%} of the screen "
          f"({len(regions)} boxes)")
    print(f"  after mesh    : {out.fraction_of(screen):.0%} of the screen "
          f"({len(meshed)} boxes, kept {shrunk:.0%} of the flagged area)")
    print(f"  leak          : {leak or 'none'}")
    print(f"  file tree kept: {tree_kept}")
    print(f"  terminal kept : {terminal_kept}")
    for note in result.notes:
        print(f"    - {note}")

    out_dir = Path(__file__).resolve().parents[1] / "evalset" / "out"
    out.image.save(out_dir / "_grid_default.png")
    rect.image.save(out_dir / "_grid_default_rects.png")

    ok = (
        result.verdict is not Verdict.PASS
        and not leak
        and out.fraction_of(screen) <= 0.08
        and tree_kept
        and terminal_kept
    )
    print(f"\n  {'PASS' if ok else 'FAIL'}: secret covered, "
          f"{'<=' if ok else '>'}8% of the screen, file tree and terminal readable")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
