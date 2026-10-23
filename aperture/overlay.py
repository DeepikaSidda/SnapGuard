"""Draw the gate's reasoning back onto the screenshot it was reasoning about.

Everything else in this project is invisible. The gate reads a screen, asks a dozen typed
questions, composes a verdict, and reports it as text scrolling past in a terminal. The
most interesting thing here -- a 2B model on the local machine looking at a screen and
judging it region by region -- has no visual form at all, which makes it hard to trust
and impossible to film.

This renders a `GateResult` as an annotated image: the region grid over the original
screen, each cell carrying the scores it actually got, cleared cells green and withheld
cells red. The numbers are the point. A reader who sees `credential 0.88` next to
`credential 0.07` learns what the thresholds mean far faster than any prose does.

Nothing here can affect a verdict. It takes a finished result and draws it.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .patterns.region_gate import GateResult, Verdict
from .tiling import Tile

# Green cleared, red withheld, amber skipped-as-irrelevant, grey unreadable. Chosen to
# survive video compression, which eats thin lines and subtle hues.
CLEARED = (46, 190, 110)
WITHHELD = (230, 70, 62)
SKIPPED = (232, 163, 61)
UNREADABLE = (140, 140, 155)

PANEL_BG = (16, 16, 20)
PANEL_FG = (240, 240, 247)
PANEL_MUTED = (150, 150, 165)

# The overlay dims the original so the annotation reads on top of a busy screenshot.
DIM = 0.45


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for name in (("segoeuib.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


@dataclass
class _Cell:
    tile: Tile
    colour: tuple[int, int, int]
    label: str
    scores: list[str]


def _classify(result: GateResult, tile: Tile) -> _Cell:
    """What happened to this region, and the numbers behind it."""
    scores = result.scores.get(tile.index, {})
    # Order matters: show the rule that drove the decision first.
    interesting = sorted(scores.items(), key=lambda kv: -kv[1])[:3]
    rendered = [f"{name} {value:.2f}" for name, value in interesting]

    if tile in result.illegible:
        return _Cell(tile, UNREADABLE, "UNREADABLE", rendered)
    if tile in result.flagged:
        fired = [f for f in result.findings if f.tile.index == tile.index]
        name = fired[0].rule.name if fired else "withheld"
        return _Cell(tile, WITHHELD, f"WITHHELD - {name}", rendered)
    if result.relevant and tile not in result.relevant:
        return _Cell(tile, SKIPPED, "SKIPPED - not relevant", rendered)
    return _Cell(tile, CLEARED, "SENT", rendered)


def _check_tiles_match(result: GateResult, tiles: list[Tile]) -> None:
    """Refuse to draw if `tiles` are not the tiles `result` is about.

    This function used to trust the caller, and that was a latent leak with a very
    unhelpful failure mode. A caller that passed a 2x2 grid alongside a result computed
    on a 6x4 grid got an image where *nothing* was destroyed: no supplied tile matched
    any flagged tile, so the "withhold" branch never ran, while the "paste the regions
    that were sent" branch matched on index alone and happily restored the original
    pixels. The output looked like a finished explain figure and had the credentials in
    it, which is precisely the bug this module's tests exist to catch. It took a grid
    change to surface it.

    So: a mismatch is a programming error, and it raises rather than rendering.
    """
    supplied = {t.index: t.box for t in tiles}
    referenced = list(result.flagged) + list(result.cleared) + list(result.illegible)
    for tile in referenced:
        if supplied.get(tile.index) != tile.box:
            raise ValueError(
                f"annotate() was given tiles that do not match the result: region "
                f"{tile.index} is {tile.box} in the result and "
                f"{supplied.get(tile.index)} in the tiles passed in. Build the grid "
                f"with the same columns and rows the gate used."
            )


def annotate(
    image: Image.Image,
    result: GateResult,
    tiles: list[Tile],
    *,
    title: str = "",
    show_numbers: bool = True,
) -> Image.Image:
    """Return a copy of `image` with the gate's reasoning drawn over it."""
    _check_tiles_match(result, tiles)
    base = image.convert("RGB")

    # Dim everything, then restore the regions that were actually sent, so the picture
    # literally shows what the cloud model got to see.
    canvas = Image.blend(base, Image.new("RGB", base.size, (10, 10, 14)), 1 - DIM)
    sent = {t.index: t.box for t in result.injectable}
    for tile in tiles:
        if sent.get(tile.index) == tile.box:
            canvas.paste(base.crop(tile.box), tile.box[:2])

    # And *destroy* the regions that were withheld, rather than merely dimming them.
    #
    # The first version of this function only dimmed, and the result was an "explain"
    # image with the AWS keys it was claiming to protect still perfectly legible. Run
    # that on a real screenshot, paste it into an article, and the tool has become the
    # leak. A diagram of a redaction has to be redacted.
    #
    # Blur first so the shape of the content survives -- a reader should be able to see
    # that something text-like was there -- then overlay a dark wash heavy enough that
    # no glyph can be recovered.
    # The boxes come from `result`, not from the `tiles` argument, so the destructive
    # step cannot be skipped by passing the wrong grid. Belt as well as braces: the
    # check above would have raised already.
    withheld_boxes = [t.box for t in result.flagged]
    if result.verdict in (Verdict.WITHHOLD, Verdict.UNREADABLE):
        withheld_boxes = [(0, 0, base.width, base.height)]
    for box in withheld_boxes:
        region = canvas.crop(box)
        if not region.width or not region.height:
            continue
        radius = max(14, min(region.width, region.height) // 10)
        region = region.filter(ImageFilter.GaussianBlur(radius))
        region = Image.blend(region, Image.new("RGB", region.size, (14, 10, 12)), 0.72)
        canvas.paste(region, box[:2])

    d = ImageDraw.Draw(canvas, "RGBA")
    scale = max(1.0, base.width / 1600)
    label_size = int(19 * scale)
    score_size = int(15 * scale)

    for tile in tiles:
        cell = _classify(result, tile)
        left, top, right, bottom = tile.box

        # A translucent wash plus a thick border: the wash survives scaling down for a
        # thumbnail, the border survives compression. Kept light on sent regions so the
        # screen content stays readable -- the point of a green cell is that you can see
        # what went.
        wash = 16 if cell.colour == CLEARED else 34
        d.rectangle([left, top, right, bottom], fill=(*cell.colour, wash))
        d.rectangle([left + 2, top + 2, right - 3, bottom - 3],
                    outline=cell.colour, width=max(3, int(4 * scale)))

        # Caption block, anchored inside the cell.
        pad = int(10 * scale)
        lines = [f"REGION {tile.index}  {cell.label}"]
        if show_numbers:
            lines += cell.scores
        box_h = pad * 2 + label_size + (score_size + 4) * (len(lines) - 1)
        box_w = max(
            d.textlength(line, font=_font(label_size if i == 0 else score_size, i == 0))
            for i, line in enumerate(lines)
        ) + pad * 2

        bx, by = left + int(14 * scale), top + int(14 * scale)
        d.rectangle([bx, by, bx + box_w, by + box_h], fill=(*PANEL_BG, 225))
        d.rectangle([bx, by, bx + int(5 * scale), by + box_h], fill=cell.colour)

        y = by + pad
        d.text((bx + pad + int(8 * scale), y), lines[0],
               font=_font(label_size, True), fill=cell.colour)
        y += label_size + 4
        for line in lines[1:]:
            d.text((bx + pad + int(8 * scale), y), line,
                   font=_font(score_size), fill=PANEL_MUTED)
            y += score_size + 4

    _footer(canvas, result, tiles, title, scale)
    return canvas


def _footer(
    canvas: Image.Image,
    result: GateResult,
    tiles: list[Tile],
    title: str,
    scale: float,
) -> None:
    """A summary bar: the verdict, the whole-frame scores, and the headline number."""
    d = ImageDraw.Draw(canvas, "RGBA")
    height = int(118 * scale)
    top = canvas.height - height
    d.rectangle([0, top, canvas.width, canvas.height], fill=(*PANEL_BG, 242))

    accent = {
        Verdict.PASS: CLEARED,
        Verdict.REDACT: WITHHELD,
        Verdict.WITHHOLD: WITHHELD,
        Verdict.UNREADABLE: UNREADABLE,
    }[result.verdict]
    d.rectangle([0, top, canvas.width, top + int(6 * scale)], fill=accent)

    pad = int(28 * scale)
    y = top + int(20 * scale)
    d.text((pad, y), "STRANDS DECIDER 2B", font=_font(int(13 * scale), True), fill=accent)

    sending = len(result.injectable)
    total = len(tiles) or 1
    headline = (
        f"{result.verdict.value.upper()}   -   {sending} of {total} regions sent to "
        f"Amazon Bedrock   -   {1 - sending / total:.0%} never left this machine"
    )
    d.text((pad, y + int(17 * scale)), headline,
           font=_font(int(21 * scale), True), fill=PANEL_FG)

    context = "   ".join(f"{k} {v:.2f}" for k, v in result.context.items())
    cost = (f"{result.decider_calls} local decisions   "
            f"{result.decider_ms / 1000:.0f}s   $0.00")
    d.text((pad, y + int(48 * scale)), f"{context}", font=_font(int(14 * scale)),
           fill=PANEL_MUTED)
    d.text((pad, y + int(68 * scale)), cost, font=_font(int(14 * scale)), fill=PANEL_MUTED)

    if title:
        w = d.textlength(title, font=_font(int(15 * scale)))
        d.text((canvas.width - pad - w, y + int(48 * scale)), title,
               font=_font(int(15 * scale)), fill=PANEL_MUTED)

    # Legend, right-aligned.
    legend = [("sent", CLEARED), ("withheld", WITHHELD),
              ("not relevant", SKIPPED), ("unreadable", UNREADABLE)]
    x = canvas.width - pad
    for label, colour in reversed(legend):
        w = d.textlength(label, font=_font(int(14 * scale)))
        x -= w
        d.text((x, y + int(17 * scale)), label, font=_font(int(14 * scale)), fill=colour)
        x -= int(10 * scale)
        d.rectangle([x - int(14 * scale), y + int(21 * scale),
                     x - int(2 * scale), y + int(33 * scale)], fill=colour)
        x -= int(28 * scale)


def thumbnail(annotated: Image.Image, width: int = 380) -> Image.Image:
    """A small version, for putting inside a notification panel."""
    ratio = width / annotated.width
    return annotated.resize((width, max(1, round(annotated.height * ratio))),
                            Image.LANCZOS)
