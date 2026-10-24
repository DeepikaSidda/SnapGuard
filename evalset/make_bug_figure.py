"""Rebuild the --explain bug beside its fix, as one figure.

The first version of `overlay.annotate` only dimmed the regions the gate had withheld.
Dim is still readable, so the image produced to *explain* a redaction rendered the
credentials it was claiming to protect. This regenerates that broken output next to the
current one so the difference can be seen rather than described.

The broken half is reproduced here deliberately and is safe to publish: every credential
on the fixture screen is either AWS's own documented example value
(AKIAIOSFODNN7EXAMPLE) or an invented string containing the word EXAMPLE. Never run this
against a real capture.

    python evalset/make_bug_figure.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from snapguard.decider import Decider  # noqa: E402
from snapguard.overlay import DIM, annotate  # noqa: E402
from snapguard.patterns import Policy, RegionGate  # noqa: E402
from snapguard.questions import CONTEXT_RULES, REGION_RULES, SCREEN_KIND  # noqa: E402
from snapguard.tiling import tile_grid  # noqa: E402

OUT = Path(__file__).parent / "out"
SCREEN = OUT / "deploy_failure.png"
FIGURE = OUT / "_bug_before_after.png"


def font(size: int, bold: bool = False):
    for name in (("segoeuib.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def annotate_the_old_broken_way(image, result, tiles):
    """The original implementation: dim everything, restore what was sent, stop there.

    Reproduced rather than imported, because the shipped code no longer does this and
    should not grow a flag that makes it do it again.
    """
    base = image.convert("RGB")
    canvas = Image.blend(base, Image.new("RGB", base.size, (10, 10, 14)), 1 - DIM)
    sent = {t.index for t in result.injectable}
    for tile in tiles:
        if tile.index in sent:
            canvas.paste(base.crop(tile.box), tile.box[:2])
    # No destruction of the withheld regions. That was the bug.
    d = ImageDraw.Draw(canvas, "RGBA")
    for tile in tiles:
        withheld = any(f.tile.index == tile.index for f in result.findings)
        colour = (230, 70, 62) if withheld else (46, 190, 110)
        left, top, right, bottom = tile.box
        d.rectangle([left, top, right, bottom], fill=(*colour, 28))
        d.rectangle([left + 2, top + 2, right - 3, bottom - 3], outline=colour, width=4)
        label = f"REGION {tile.index}  {'WITHHELD' if withheld else 'SENT'}"
        d.rectangle([left + 14, top + 14, left + 14 + 330, top + 14 + 44],
                    fill=(16, 16, 20, 225))
        d.text((left + 28, top + 26), label, font=font(21, True), fill=colour)
    return canvas


def caption(img, title, subtitle, accent):
    """Add a header strip above an image."""
    bar = 112
    out = Image.new("RGB", (img.width, img.height + bar), (12, 12, 16))
    out.paste(img, (0, bar))
    d = ImageDraw.Draw(out)
    d.rectangle([0, 0, img.width, 7], fill=accent)
    d.text((34, 26), title, font=font(38, True), fill=accent)
    d.text((34, 72), subtitle, font=font(21), fill=(206, 206, 216))
    return out


def main() -> int:
    decider = Decider()
    decider.require_vision()
    image = Image.open(SCREEN)
    gate = RegionGate(
        decider,
        rules=REGION_RULES,
        context_rules=CONTEXT_RULES,
        info_questions=SCREEN_KIND,
        policy=Policy(fail_closed=True),
    )
    print("running the gate...")
    result = gate.inspect(image)
    tiles = tile_grid(image, columns=2, rows=2)
    print(f"  verdict {result.verdict.value}, "
          f"{len(result.flagged)} region(s) withheld")

    broken = annotate_the_old_broken_way(image, result, tiles)
    fixed = annotate(image, result, tiles, title="deploy_failure.png")

    left = caption(
        broken,
        "BEFORE  -  the bug",
        "Withheld regions were only dimmed. The credentials are still legible.",
        (230, 70, 62),
    )
    right = caption(
        fixed,
        "AFTER  -  the fix",
        "Withheld regions are blurred then washed. OCR recovers nothing.",
        (46, 190, 110),
    )

    gap = 26
    figure = Image.new("RGB", (left.width + right.width + gap,
                               max(left.height, right.height)), (12, 12, 16))
    figure.paste(left, (0, 0))
    figure.paste(right, (left.width + gap, 0))

    # Halve it: two 1080p frames side by side is 3866px wide and nobody can see that.
    figure = figure.resize((figure.width // 2, figure.height // 2), Image.LANCZOS)
    figure.save(FIGURE)
    print(f"wrote {FIGURE}  ({figure.width}x{figure.height})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
