"""Draw the architecture diagram, with the AWS services shown as icons.

Generated rather than drawn by hand, for the same reason every evaluation screen in this
repository is generated: a diagram that is a script can be corrected when the
architecture changes, and a diagram that is a PNG cannot.

    python docs/make_architecture.py

Writes docs/architecture.png.

The one thing the layout is built to make unmistakable is the boundary. Everything inside
the dashed panel happens on the user's machine; everything outside it is a network call.
The local reader and Decider sit inside. Textract, Comprehend and Bedrock sit outside,
and the arrow that reaches Textract is dashed because it is opt-in and because it carries
the whole screenshot.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1860, 1000
OUT = Path(__file__).parent / "architecture.png"

# AWS brand and 2023 icon-category colours.
NAVY = (35, 47, 62)          # AWS squid ink
ORANGE = (255, 153, 0)       # AWS orange
ML_GREEN = (1, 168, 141)     # Machine Learning category
INK = (26, 32, 44)
MUTED = (113, 128, 150)
LINE = (176, 186, 200)
PANEL = (247, 249, 252)
LOCAL = (237, 242, 247)
WHITE = (255, 255, 255)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = ("segoeuib.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def centre(d: ImageDraw.ImageDraw, box, text, f, fill):
    """Write `text` centred in `box`."""
    left, top, right, bottom = box
    w = d.textlength(text, font=f)
    ascent, descent = f.getmetrics()
    d.text(
        (left + (right - left - w) / 2, top + (bottom - top - ascent - descent) / 2),
        text, font=f, fill=fill,
    )


def icon_tile(d: ImageDraw.ImageDraw, x: int, y: int, size: int, colour) -> None:
    """The rounded square an AWS service icon sits in."""
    d.rounded_rectangle([x, y, x + size, y + size], size // 5, fill=colour)


def icon_textract(d: ImageDraw.ImageDraw, x: int, y: int, s: int = 84) -> None:
    """A page with lines of text and one line picked out."""
    icon_tile(d, x, y, s, ML_GREEN)
    px, py, pw, ph = x + s * 0.26, y + s * 0.18, s * 0.48, s * 0.64
    d.rounded_rectangle([px, py, px + pw, py + ph], 4, fill=WHITE)
    for i in range(5):
        ly = py + ph * 0.18 + i * ph * 0.15
        wide = pw * (0.66 if i % 2 else 0.52)
        colour = ORANGE if i == 2 else ML_GREEN
        d.rectangle([px + pw * 0.14, ly, px + pw * 0.14 + wide, ly + 3], fill=colour)


def icon_comprehend(d: ImageDraw.ImageDraw, x: int, y: int, s: int = 84) -> None:
    """Tokens being labelled: three chips, one tagged."""
    icon_tile(d, x, y, s, ML_GREEN)
    for i, w in enumerate((0.40, 0.30, 0.46)):
        cy = y + s * (0.24 + i * 0.21)
        d.rounded_rectangle(
            [x + s * 0.20, cy, x + s * 0.20 + s * w, cy + s * 0.13], 5,
            fill=WHITE if i != 1 else ORANGE,
        )
    d.ellipse([x + s * 0.70, y + s * 0.42, x + s * 0.84, y + s * 0.56], fill=ORANGE)


def icon_bedrock(d: ImageDraw.ImageDraw, x: int, y: int, s: int = 84) -> None:
    """Stacked strata, the Bedrock motif."""
    icon_tile(d, x, y, s, ML_GREEN)
    for i, inset in enumerate((0.16, 0.22, 0.28)):
        ly = y + s * (0.26 + i * 0.20)
        d.rounded_rectangle(
            [x + s * inset, ly, x + s * (1 - inset), ly + s * 0.12], 4, fill=WHITE,
        )


def icon_decider(d: ImageDraw.ImageDraw, x: int, y: int, s: int = 84) -> None:
    """A decision: a diamond with a tick."""
    icon_tile(d, x, y, s, ORANGE)
    m = s / 2
    d.polygon(
        [(x + m, y + s * 0.16), (x + s * 0.84, y + m),
         (x + m, y + s * 0.84), (x + s * 0.16, y + m)],
        fill=WHITE,
    )
    d.line([(x + s * 0.38, y + m), (x + s * 0.47, y + s * 0.60),
            (x + s * 0.64, y + s * 0.40)], fill=ORANGE, width=6)


def icon_screen(d: ImageDraw.ImageDraw, x: int, y: int, s: int = 84) -> None:
    """A window with a redaction bar across it."""
    icon_tile(d, x, y, s, NAVY)
    d.rounded_rectangle([x + s * 0.16, y + s * 0.22, x + s * 0.84, y + s * 0.74], 5,
                        fill=WHITE)
    d.rectangle([x + s * 0.16, y + s * 0.22, x + s * 0.84, y + s * 0.33], fill=LINE)
    d.rectangle([x + s * 0.24, y + s * 0.44, x + s * 0.62, y + s * 0.52], fill=INK)
    for i in range(2):
        ly = y + s * (0.58 + i * 0.07)
        d.rectangle([x + s * 0.24, ly, x + s * 0.70, ly + 3], fill=LINE)


def card(d, box, title, lines, accent=None, fill=WHITE, dashed=False):
    """A labelled box. `accent` draws a bar down the left edge."""
    left, top, right, bottom = box
    d.rounded_rectangle(box, 14, fill=fill, outline=LINE, width=2)
    if accent:
        d.rounded_rectangle([left, top, left + 8, bottom], 4, fill=accent)
    d.text((left + 26, top + 18), title, font=font(23, True), fill=INK)
    for i, line in enumerate(lines):
        d.text((left + 26, top + 54 + i * 25), line, font=font(17), fill=MUTED)


def arrow(d, start, end, colour=NAVY, width=3, dashed=False, label=None,
          label_above=True):
    x1, y1 = start
    x2, y2 = end
    if dashed:
        total = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        steps = max(1, int(total // 18))
        for i in range(steps):
            if i % 2:
                continue
            a = i / steps
            b = min(1.0, (i + 1) / steps)
            d.line([(x1 + (x2 - x1) * a, y1 + (y2 - y1) * a),
                    (x1 + (x2 - x1) * b, y1 + (y2 - y1) * b)], fill=colour, width=width)
    else:
        d.line([start, end], fill=colour, width=width)

    # Head, pointing along the segment.
    import math
    angle = math.atan2(y2 - y1, x2 - x1)
    size = 13
    d.polygon([
        (x2, y2),
        (x2 - size * math.cos(angle - 0.42), y2 - size * math.sin(angle - 0.42)),
        (x2 - size * math.cos(angle + 0.42), y2 - size * math.sin(angle + 0.42)),
    ], fill=colour)

    if label:
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        w = d.textlength(label, font=font(15))
        ly = my - 28 if label_above else my + 12
        d.rectangle([mx - w / 2 - 6, ly - 2, mx + w / 2 + 6, ly + 20], fill=WHITE)
        d.text((mx - w / 2, ly), label, font=font(15), fill=MUTED)


def main() -> int:
    img = Image.new("RGB", (W, H), WHITE)
    d = ImageDraw.Draw(img)

    d.text((60, 44), "SnapGuard", font=font(44, True), fill=INK)
    d.text((60, 100), "A local model decides how much of your screen may reach the cloud.",
           font=font(21), fill=MUTED)

    # ---- the machine boundary ------------------------------------------------
    #
    # One vertical column. An earlier version put "cover it" in a second column to the
    # right, which read fine until the opt-in cloud arrows were added and crossed
    # straight through it. A diagram whose arrows pass behind a box is a diagram nobody
    # trusts, so the flow is a single line and the right-hand margin is kept empty for
    # the two arrows that leave the machine.
    machine = (60, 170, 900, 905)
    d.rounded_rectangle(machine, 20, fill=LOCAL, outline=(160, 174, 192), width=2)
    d.text((88, 190), "YOUR MACHINE", font=font(18, True), fill=(90, 103, 120))
    d.text((88, 215), "nothing here crosses the network", font=font(15),
           fill=(130, 145, 165))

    # ---- the cloud boundary --------------------------------------------------
    cloud = (1150, 170, 1800, 905)
    d.rounded_rectangle(cloud, 20, fill=PANEL, outline=(160, 174, 192), width=2)
    d.rounded_rectangle([1150, 170, 1800, 228], 20, fill=NAVY)
    d.text((1180, 186), "AMAZON WEB SERVICES", font=font(18, True), fill=WHITE)

    # ---- the four local stages, stacked -------------------------------------
    icon_screen(d, 96, 258)
    card(d, (196, 252, 862, 342), "1   Capture",
         ["a screenshot, or an image on your clipboard"], accent=NAVY)

    card(d, (96, 382, 862, 492), "2   Read it     where is the text?",
         ["RapidOCR + 13 regex patterns   -   free, local, a box per line",
          "the default. nothing is uploaded."], accent=(130, 145, 165))

    icon_decider(d, 96, 540)
    card(d, (196, 532, 862, 662), "3   Judge it     may it leave?",
         ["Strands Decider 2B   -   127.0.0.1:8099",
          "whole frame: 4 questions.   each of 24 regions: 3",
          "every answer carries a calibrated confidence"], accent=ORANGE)

    card(d, (96, 702, 862, 792), "4   Cover it",
         ["paint only the cells that hold ink, leave the labels readable"],
         accent=NAVY)

    # ---- the AWS services ----------------------------------------------------
    icon_textract(d, 1182, 268)
    card(d, (1284, 262, 1772, 352), "Amazon Textract",
         ["pixels to words, a box per WORD"], accent=ML_GREEN)

    icon_comprehend(d, 1182, 402)
    card(d, (1284, 396, 1772, 486), "Amazon Comprehend",
         ["names each span it finds"], accent=ML_GREEN)

    d.text((1182, 512), "an alternative reader, reached only with  --aws-lane",
           font=font(16, True), fill=(150, 120, 60))
    d.text((1182, 536), "reads small text the local engine cannot, and returns a box",
           font=font(15), fill=MUTED)
    d.text((1182, 558), "per word. it sends the whole capture, so it is off by default.",
           font=font(15), fill=MUTED)

    icon_bedrock(d, 1182, 700)
    card(d, (1284, 694, 1772, 800), "Amazon Bedrock",
         ["Nova 2 Lite reads the cleared regions", "and answers the question"],
         accent=ML_GREEN)

    # ---- the wiring ----------------------------------------------------------
    arrow(d, (480, 346), (480, 378))      # capture -> read
    arrow(d, (480, 496), (480, 528))      # read -> judge
    arrow(d, (480, 666), (480, 698))      # judge -> cover

    # The opt-in lane, out of the machine and back. Both arrows live in the empty
    # corridor between the two panels.
    arrow(d, (866, 420), (1176, 310), colour=(200, 150, 40), dashed=True,
          label="the capture")
    arrow(d, (1176, 440), (866, 462), colour=(200, 150, 40), dashed=True,
          label="words + labels", label_above=False)

    # The only thing that ever reaches Bedrock.
    arrow(d, (866, 750), (1278, 748), colour=NAVY, width=4,
          label="only cleared regions")

    # ---- the three outcomes --------------------------------------------------
    d.text((96, 810), "The verdict", font=font(19, True), fill=INK)
    outcomes = (
        ("PASS", "sent unchanged", (46, 139, 87)),
        ("REDACT", "sensitive parts covered, the rest sent", ORANGE),
        ("WITHHOLD", "nothing leaves, and you are told why", (197, 48, 48)),
    )
    for i, (name, text, colour) in enumerate(outcomes):
        x = 96 + i * 268
        d.rounded_rectangle([x, 842, x + 112, 870], 8, fill=colour)
        centre(d, (x, 842, x + 112, 870), name, font(14, True), WHITE)
        d.text((x, 876), text, font=font(13), fill=MUTED)

    # ---- the headline --------------------------------------------------------
    d.rounded_rectangle([940, 824, 1110, 905], 14, fill=ORANGE)
    centre(d, (940, 824, 1110, 866), "12/12", font(34, True), WHITE)
    centre(d, (940, 862, 1110, 898), "benchmark", font(15), (90, 55, 0))

    d.rounded_rectangle([1150, 930, 1800, 972], 12, fill=NAVY)
    d.text((1178, 942), "$0.00 per image   -   1.2% of a screen covered to hide a key",
           font=font(17, True), fill=WHITE)

    img.save(OUT)
    print(f"wrote {OUT}  ({img.width}x{img.height})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
