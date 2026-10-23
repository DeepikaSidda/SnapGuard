"""At a 6x4 grid, does the contacts screen still keep every contact detail local?

At the old 2x2 grid all four regions of this screen fired `personal_contact`, so nothing
was sent and the README said so: "0 of 4 regions sent, 100% kept local". At 6x4 only
4 regions of 24 fire, and 20 would be sent to Bedrock.

That is either correct -- the details sit in one column and the other regions hold
labels, chrome and empty space -- or it is a protection regression caused by the finer
grid, because a small region holding one email has less context than a whole pane holding
seven rows.

Settled by reading the pixels rather than by argument: black out the regions the model
flagged, OCR everything that is left, and look for contact details in it. No model calls,
so this is seconds.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from aperture.mesh import tighten_all  # noqa: E402
from aperture.redact import redact  # noqa: E402
from aperture.scanner import read_text_boxes  # noqa: E402
from aperture.tiling import tile_grid  # noqa: E402

SCREEN = Path(__file__).resolve().parents[1] / "evalset" / "out" / "contacts.png"

# The regions the model flagged at 6x4, from the real run.
FLAGGED = [1, 3, 7, 13]

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Deliberately loose: any run of 7+ digits, with separators, is phone-shaped enough.
PHONE = re.compile(r"(?:\+?\d[\d\s().-]{6,}\d)")


def main() -> int:
    image = Image.open(SCREEN).convert("RGB")
    tiles = {t.index: t for t in tile_grid(image)}
    regions = [tiles[i].box for i in FLAGGED]

    for label, boxes in (
        ("regions only", regions),
        ("regions + mesh", tighten_all(image, regions)[0]),
    ):
        out = redact(image, boxes, mode="block", label=None)
        text = " ".join(t for t, _ in read_text_boxes(out.image))

        emails = EMAIL.findall(text)
        phones = [p.strip() for p in PHONE.findall(text) if len(re.sub(r"\D", "", p)) >= 7]

        print(f"\n  {label}: {len(boxes)} boxes, "
              f"{out.fraction_of(image):.1%} of the frame covered")
        print(f"    emails still readable : {len(emails)} {emails[:4]}")
        print(f"    phone-shaped runs     : {len(phones)} {phones[:4]}")
        if emails or phones:
            print("    -> contact details SURVIVE outside the flagged regions")
        else:
            print("    -> nothing contact-shaped recoverable")

    # What is actually in the 20 regions that would be sent.
    sent = [t for i, t in tiles.items() if i not in FLAGGED]
    print(f"\n  what the {len(sent)} sendable regions contain:")
    for tile in sent[:24]:
        text = " ".join(t for t, _ in read_text_boxes(tile.image)).strip()
        if text:
            print(f"    region {tile.index:>2}: {text[:72]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
