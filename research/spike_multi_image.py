"""Does a question about several images answer per-image, or about all of them at once?

This decides the architecture. If four tiles in one request yielded four verdicts, a
whole 1920x1080 screen could be scored in three requests. If instead the question is
answered once over the combined state, per-tile verdicts cost one request each -- nine
requests and about 100 seconds for a 3x3 grid, which is too slow to sit in a clipboard
path.

The prompt layout in the upstream tests says images are concatenated into one state:

    <state>
    <|vision_start|><|image_pad|><|vision_end|>
    <|vision_start|><|image_pad|><|vision_end|>
    two pages
    </state>

...so joint is the expectation. Measured here rather than assumed, because if an "is it
in ANY of these" screening question works, a coarse-to-fine pass clears a clean screen
in far fewer requests than one-per-tile.

Run with the vision server on :8099.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from aperture.decider import Decider, noul  # noqa: E402

EVALSET = Path(__file__).resolve().parents[1] / "evalset" / "out"

CREDENTIAL = noul(
    "Is an access key, password, token or other secret value visible as text?",
    true="a credential or secret value can be read in the image",
    false="no credential or secret value is visible",
)

ANY_CREDENTIAL = noul(
    "Across all the images shown, does ANY of them contain an access key, password, "
    "token or other secret value visible as text?",
    true="at least one of the images shows a credential",
    false="none of the images shows a credential",
)


def crop(name: str, box: tuple[int, int, int, int]) -> Image.Image:
    return Image.open(EVALSET / f"{name}.png").crop(box)


def main() -> int:
    decider = Decider()
    print(decider.banner(), "\n")

    # A region that does contain a fake credential, and three that plainly do not.
    secret = crop("ide_env", (320, 100, 1320, 460))
    clean_code = crop("docs", (400, 420, 1400, 660))
    clean_text = crop("docs", (380, 140, 1380, 400))
    clean_term = crop("terminal_error", (20, 300, 1020, 660))

    print("=" * 74)
    print("A. one image at a time -- the baseline")
    print("=" * 74)
    for label, img in (
        ("secret region ", secret),
        ("clean code    ", clean_code),
        ("clean prose   ", clean_text),
        ("clean terminal", clean_term),
    ):
        r = decider.ask("A region of a screenshot.", {"cred": CREDENTIAL}, [img])
        print(f"  {label}  cred={r.value('cred'):.3f}   ({r.latency_ms:.0f} ms)")

    print("\n" + "=" * 74)
    print("B. four images in one request, asked the per-image question")
    print("   If this tracks 'any', the answer should be high (one of them has a key).")
    print("=" * 74)
    r = decider.ask(
        "Four regions of the same screenshot.",
        {"cred": CREDENTIAL},
        [clean_code, clean_text, secret, clean_term],
    )
    print(f"  secret in position 3   cred={r.value('cred'):.3f}   "
          f"({r.latency_ms:.0f} ms, {r.input_tokens} tokens)")

    r_clean = decider.ask(
        "Four regions of the same screenshot.",
        {"cred": CREDENTIAL},
        [clean_code, clean_text, clean_term, clean_code],
    )
    print(f"  all four clean         cred={r_clean.value('cred'):.3f}   "
          f"({r_clean.latency_ms:.0f} ms, {r_clean.input_tokens} tokens)")

    print("\n" + "=" * 74)
    print("C. the same batches, asked explicitly as an 'ANY of these' question")
    print("=" * 74)
    r_any_dirty = decider.ask(
        "Four regions of the same screenshot are shown.",
        {"any_cred": ANY_CREDENTIAL},
        [clean_code, clean_text, secret, clean_term],
    )
    r_any_clean = decider.ask(
        "Four regions of the same screenshot are shown.",
        {"any_cred": ANY_CREDENTIAL},
        [clean_code, clean_text, clean_term, clean_code],
    )
    print(f"  batch containing a key  any={r_any_dirty.value('any_cred'):.3f}")
    print(f"  batch of clean regions  any={r_any_clean.value('any_cred'):.3f}")
    separation = r_any_dirty.value("any_cred") - r_any_clean.value("any_cred")
    print(f"\n  separation: {separation:+.3f}")

    print("\n" + "=" * 74)
    print("VERDICT")
    print("=" * 74)
    if separation > 0.25:
        print("  A coarse 'any of these' screening pass works. A clean 3x3 screen can")
        print("  be cleared in 3 requests instead of 9, and only suspicious batches")
        print("  need to be split for per-tile localisation.")
    else:
        print("  Batched screening does not separate. Per-tile verdicts need one")
        print("  request per tile, so the gate must keep the tile count small and")
        print("  lean on the deterministic fast lane for localisation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
