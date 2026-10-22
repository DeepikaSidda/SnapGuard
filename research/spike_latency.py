"""Where does the 47-second median go, and can any of it be bought back?

Three levers, measured rather than guessed:

  1. IMAGE SIZE. Tiles are sent at 448px on the long side, which is 196 tokens for a
     square. Smaller images mean fewer visual tokens and a shorter forward pass. The
     question is whether the accuracy survives -- the whole reason for tiling was that
     detail matters (full frame 0.643 vs cropped 0.884).

  2. QUESTION COUNT. The context pass asks three rules plus `screen_kind`, a 7-option
     choice that is informational only and never fires anything. Each extra question
     measured ~2.8s.

  3. GRID SIZE. 2x2 means the coarse pass covers the frame in one request. 3x3 is more
     detail but more requests.

Run with the vision server on :8099. Expect this to take a while.
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aperture.decider import Decider, noul  # noqa: E402
from aperture.questions import CONTEXT_RULES, REGION_RULES, SCREEN_KIND  # noqa: E402
from aperture.tiling import tile_grid  # noqa: E402

SCREENS = Path(__file__).resolve().parents[1] / "evalset" / "out"

CREDENTIAL = next(r for r in REGION_RULES if r.name == "credential")

# The region that is known to hold a credential, and three that do not.
CASES = [
    ("dirty: .env pane", "deploy_failure", 1, True),
    ("clean: terminal", "deploy_failure", 0, False),
    ("clean: docs body", "docs", 2, False),
    ("dirty: ide_env", "ide_env", 0, True),
]


def tile_of(screen: str, index: int, long_side: int) -> Image.Image:
    """Crop a region and scale it to `long_side`, as the client would."""
    image = Image.open(SCREENS / f"{screen}.png")
    tile = next(t for t in tile_grid(image, columns=2, rows=2) if t.index == index)
    crop = tile.image.convert("RGB")
    scale = long_side / max(crop.size)
    if scale < 1:
        crop = crop.resize(
            (max(1, round(crop.width * scale)), max(1, round(crop.height * scale))),
            Image.LANCZOS,
        )
    return crop


def main() -> int:
    decider = Decider()
    print(decider.banner(), "\n")

    print("=" * 78)
    print("1. IMAGE SIZE: latency and accuracy against the long side")
    print("=" * 78)
    print(f"  {'long side':>10}  {'tokens':>7}  {'latency':>9}   verdicts")
    print("  " + "-" * 70)

    best_small = None
    for long_side in (448, 384, 336, 288, 224):
        latencies, tokens, correct = [], [], 0
        detail = []
        for label, screen, index, dirty in CASES:
            crop = tile_of(screen, index, long_side)
            reply = decider.ask(
                "A region of a screen captured on a computer.",
                {"credential": CREDENTIAL.question},
                [crop],
            )
            value = reply.value("credential")
            fired = value >= CREDENTIAL.threshold
            correct += fired == dirty
            latencies.append(reply.latency_ms)
            tokens.append(reply.input_tokens)
            detail.append(f"{value:.2f}{'!' if fired != dirty else ''}")

        median = statistics.median(latencies)
        print(f"  {long_side:>10}  {statistics.mean(tokens):>7.0f}  "
              f"{median / 1000:>8.1f}s   {correct}/{len(CASES)}  [{' '.join(detail)}]")
        if correct == len(CASES) and (best_small is None or long_side < best_small[0]):
            best_small = (long_side, median)

    if best_small:
        baseline = None
        print(f"\n  smallest size with no mistakes: {best_small[0]}px "
              f"at {best_small[1] / 1000:.1f}s")

    print("\n" + "=" * 78)
    print("2. QUESTION COUNT: what does screen_kind cost?")
    print("=" * 78)
    frame = Image.open(SCREENS / "deploy_failure.png")
    rules_only = {r.name: r.question for r in CONTEXT_RULES}
    with_kind = {**rules_only, **SCREEN_KIND}

    for label, questions in (
        ("3 rules only          ", rules_only),
        ("3 rules + screen_kind ", with_kind),
    ):
        reply = decider.ask("A screenshot of an application window.", questions, [frame])
        print(f"  {label} {len(questions)} questions  "
              f"{reply.latency_ms / 1000:>5.1f}s  {reply.input_tokens} tokens")

    print("\n" + "=" * 78)
    print("3. WHOLE-FRAME CONTEXT PASS: is the full frame worth its tokens?")
    print("=" * 78)
    for long_side in (448, 336):
        shrunk = frame.convert("RGB")
        scale = long_side / max(shrunk.size)
        shrunk = shrunk.resize(
            (round(shrunk.width * scale), round(shrunk.height * scale)), Image.LANCZOS
        )
        reply = decider.ask(
            "A screenshot of an application window.", rules_only, [shrunk]
        )
        values = "  ".join(f"{n}={reply.value(n):.2f}" for n in rules_only)
        print(f"  {long_side}px  {reply.latency_ms / 1000:>5.1f}s  "
              f"{reply.input_tokens} tokens   {values}")

    print("\nAll three rules should stay near zero on this screen; if a smaller frame")
    print("changes that, the context pass needs its tokens.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
