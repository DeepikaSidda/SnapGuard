import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from aperture.mesh import (
    DEFAULT_CELL,
    MAX_FRACTION,
    extend_along_ink,
    ink_mask,
    join_adjacent,
    tighten,
    _union_area,
)
from aperture.scanner import ordinary_language_boxes
from aperture.tiling import tile_grid

path = Path(r"C:\Users\sidda\Downloads\aperture-quarantine"
            r"\2026-10-23T165053.218_withhold.png")
image = Image.open(path).convert("RGB")
tiles = {t.index: t for t in tile_grid(image)}
flagged = [6, 7, 8, 9, 10]
boxes = [tiles[i].box for i in flagged]
total = image.width * image.height

reveal = ordinary_language_boxes(image)
print(f"reveal boxes: {len(reveal)}\n")

bars = []
for i in flagged:
    box = tiles[i].box
    raw, _, _ = ink_mask(image, box, cell=DEFAULT_CELL)
    masked, _, _ = ink_mask(image, box, cell=DEFAULT_CELL, reveal=reveal)
    got = tighten(image, box, reveal=reveal)
    whole = got == [box]
    print(f"  region {i} {box}  raw={raw.mean():.0%} masked={masked.mean():.0%} "
          f"ceiling={MAX_FRACTION:.0%}  -> {len(got)} box(es)"
          f"{'  WHOLE RECTANGLE' if whole else ''}")
    bars.extend(got)

a = _union_area(bars, image.size) / total
ext = extend_along_ink(image, bars, reveal=reveal)
b = _union_area(ext, image.size) / total
joined = join_adjacent(ext, bridge=0)
c = _union_area(joined, image.size) / total
print(f"\n  tighten {len(bars)} bars {a:.1%} -> extend {len(ext)} {b:.1%} "
      f"-> join {len(joined)} {c:.1%}")
