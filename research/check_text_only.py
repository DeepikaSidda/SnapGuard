"""Does restricting the mesh to Textract's word boxes remove the furniture?

The artefacts a user reported six times, after the key itself was finally covered
correctly: a vertical stripe down a card border, small bars under an input field, a block
over a two-letter word. All of them are the gradient test calling something ink that is
not writing.

Three rounds of threshold tuning did not separate them, and the measured reason is that
they do not separate cleanly on shape alone -- on the reported capture a border scores
elongation 3.2-3.8 and flatness 6.6-7.4 where a line of credentials scores 21.7 and 24.8.
Fitting thresholds between those two sets is fitting to one screenshot.

Textract returns a box per word, so when the cloud lane has run, "where is the text" is
known. This measures what happens when the mesh is restricted to those boxes: ink outside
them is furniture and is left alone.

What has to stay true: the key must still be unrecoverable. That is the only reason this
is not a free win -- a credential Textract failed to read would be in no word box and
would not be covered.

Sends the image to AWS. See the warning in `snapguard/aws_lane.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from snapguard.aws_lane import read, reveal_boxes  # noqa: E402
from snapguard.mesh import tighten_all  # noqa: E402
from snapguard.redact import redact, resolve_boxes  # noqa: E402
from snapguard.scanner import read_text_boxes, scan_text  # noqa: E402
from snapguard.tiling import tile_grid  # noqa: E402

QUARANTINE = Path(r"C:\Users\sidda\Downloads\snapguard-quarantine")
OUT = Path(__file__).resolve().parents[1] / "evalset" / "out"

# The regions the model flagged on the most recent real capture, from its guard log.
FLAGGED = [6, 7, 10]
MARKER = "dcd_live"
CONTROL = "allowance"


def main() -> int:
    captures = sorted(QUARANTINE.glob("*_redact.png"))
    if not captures:
        print("no quarantined capture to test against")
        return 1
    path = captures[-1]

    image = Image.open(path).convert("RGB")
    tiles = {t.index: t for t in tile_grid(image)}
    regions = [tiles[i].box for i in FLAGGED if i in tiles]

    reading = read(image)
    hints = [s.box for s in reading.secrets]
    reveal = reveal_boxes(reading)
    words = [w.box for w in reading.words]
    boxes, _ = resolve_boxes(regions, hints)

    print(f"  {path.name}  {image.width}x{image.height}")
    print(f"  textract: {len(reading.words)} words, {len(reading.secrets)} secrets, "
          f"{len(reveal)} revealed\n")
    print(f"    {'mode':<22}{'covered':>9}{'boxes':>7}   leak   control")
    print("    " + "-" * 52)

    for label, text_boxes in (
        ("ink (all of it)", None),
        ("ink inside word boxes", words),
    ):
        meshed, _ = tighten_all(
            image, boxes, reveal_boxes=reveal, text_boxes=text_boxes
        )
        out = redact(image, meshed, mode="block", label=None)
        text = " ".join(t for t, _ in read_text_boxes(out.image))
        flat = text.lower().replace(" ", "")
        leak = MARKER.lower() in flat or bool(scan_text(text))
        control = CONTROL.lower() in flat

        print(f"    {label:<22}{out.fraction_of(image):>8.2%}{len(meshed):>7}"
              f"{'   LEAK' if leak else '   none'}   {control}")
        suffix = "words" if text_boxes else "allink"
        out.image.save(OUT / f"_textonly_{suffix}.png")
        if leak:
            print("      FAIL: the key survived")
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
