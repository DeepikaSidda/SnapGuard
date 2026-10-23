"""Is a finer grid better than a coarse grid plus refinement?

Reported twice: the redaction covers far too much of the screen. The first answer was to
subdivide a flagged region and re-ask (refinement). It helped -- 56% to 24% -- and the
user looked at the result and said, correctly, that the obvious fix is to use small
regions in the first place.

This measures that. A finer grid means:

  + each region is a smaller fraction of the screen, so a hit covers less
  + regions stay under the server's 448px long side, so no detail is thrown away at all
    (a 2x2 region of a 1080p screen is downscaled 2.1x; a 4x4 region is not downscaled)
  - more regions to screen

The last point is the one that needs measuring, because the coarse "any of these?" pass
screens four regions per request. A 4x4 grid is 16 regions but only 4 coarse requests,
and the fine pass still only runs on batches that come back hot.

Run with the vision server on :8099.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aperture.decider import IMAGE_LONG_SIDE, Decider  # noqa: E402
from aperture.patterns import Policy, RegionGate  # noqa: E402
from aperture.questions import CONTEXT_RULES, REGION_RULES  # noqa: E402
from aperture.redact import redact  # noqa: E402
from aperture.scanner import read_text_boxes, scan_text  # noqa: E402
from aperture.tiling import tile_grid  # noqa: E402

W, H = 1024, 576
OUT = Path(__file__).resolve().parents[1] / "evalset" / "out"


def font(size: int, mono: bool = False):
    for n in (("consola.ttf",) if mono else ("segoeui.ttf",)):
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            pass
    return ImageFont.load_default()


def draw() -> Image.Image:
    """An editor with a keys CSV open: one secret line, lots of content worth keeping."""
    img = Image.new("RGB", (W, H), "#1e1e24")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 26], fill="#2b2b31")
    d.text((10, 6), "Deepu_accessKeys .csv - Strands Decider - Visual Studio Code",
           font=font(11), fill="#d0d0d8")
    d.rectangle([0, 26, 46, H], fill="#191920")
    d.rectangle([46, 26, 210, H], fill="#1b1b22")
    for i, name in enumerate(
        ["STRANDS DECIDER", "  .venv", "  aperture", "  evalset", "  research",
         "  tests", "  ARTICLE.md", "  Deepu_accessKeys .csv", "  DEMO_SCRIPT.md",
         "  LICENSE", "  pyproject.toml", "  README.md"]
    ):
        colour = "#ffffff" if "accessKeys" in name else "#8a8a99"
        d.text((54, 36 + i * 19), name, font=font(11), fill=colour)
    d.rectangle([210, 26, W, 50], fill="#23232b")
    d.text((222, 32), "Deepu_accessKeys .csv", font=font(11), fill="#dcdce4")
    d.text((222, 62), "1", font=font(12, True), fill="#5a5a66")
    d.text((248, 62), "Access key ID,Secret access key", font=font(12, True),
           fill="#c8c8d0")
    d.text((222, 82), "2", font=font(12, True), fill="#5a5a66")
    d.text((248, 82),
           "AKIAEXAMPLEKEYID1234,EXAMPLEKEYa1B2c3D4e5F6g7H8i9J0kL1mN2oP3q",
           font=font(12, True), fill="#ce9178")
    d.rectangle([210, 330, W, H], fill="#0f1014")
    d.text((222, 338), "TERMINAL", font=font(10), fill="#8a8a99")
    for i, line in enumerate([
        "PS C:\\Users\\sidda\\Downloads\\Strands Decider> aperture doctor",
        "decider server", "  ok    strands-decider-2B on cpu, vision on",
        "OCR fast lane", "  ok    installed", "Amazon Bedrock",
        "  ok    us.amazon.nova-2-lite-v1:0 reachable",
    ]):
        d.text((222, 356 + i * 17), line, font=font(11, True), fill="#c5c8c9")
    return img


def main() -> int:
    screen = draw()
    screen.save(OUT / "_grid_case.png")
    decider = Decider()
    print(decider.banner())
    print(f"\nscreen {W}x{H}\n")

    print(f"  {'grid':<9}{'region size':>14}{'downscale':>11}"
          f"{'covered':>10}{'calls':>7}{'time':>8}  leak")
    print("  " + "-" * 72)

    for columns, rows, refine in (
        (2, 2, True),    # what ships today
        (3, 3, False),
        (4, 4, False),
        (6, 4, False),
        (4, 4, True),
    ):
        tiles = tile_grid(screen, columns=columns, rows=rows)
        sample = tiles[0]
        downscale = max(sample.width, sample.height) / IMAGE_LONG_SIDE

        gate = RegionGate(
            decider,
            rules=REGION_RULES,
            context_rules=CONTEXT_RULES,
            policy=Policy(fail_closed=True),
            columns=columns,
            rows=rows,
            refine_flagged=refine,
        )
        started = time.perf_counter()
        result = gate.inspect(screen)
        wall = time.perf_counter() - started

        out = redact(screen, result.boxes(), mode="block")
        text = " ".join(t for t, _ in read_text_boxes(out.image))
        leaked = scan_text(text) or ("AKIAWBDM" in text)
        tree_kept = "pyproject" in text or "LICENSE" in text

        label = f"{columns}x{rows}" + ("+ref" if refine else "")
        out.image.save(OUT / f"_grid_{columns}x{rows}{'r' if refine else ''}.png")
        print(f"  {label:<9}{sample.width}x{sample.height:<9}"
              f"{downscale:>10.1f}x{out.fraction_of(screen):>10.0%}"
              f"{result.decider_calls:>7}{wall:>7.0f}s  "
              f"{leaked or 'none':<6} tree_kept={tree_kept}")

    print("\n  downscale 1.0x or less means the server throws away no detail at all.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
