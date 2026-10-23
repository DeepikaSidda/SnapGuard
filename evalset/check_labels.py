"""Sanity-check the region ground truth before spending model time on it.

Bad labels make a calibration measurement worse than no measurement, because it looks
rigorous and is wrong. This prints which region of which screen is labelled as holding
what, so the labels can be eyeballed against the images.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from aperture.questions import CONTEXT_RULES, REGION_RULES  # noqa: E402
from regions import SCREENS, frame_holds, region_holds  # noqa: E402

FRAME_RULES = [r.name for r in CONTEXT_RULES]
TILE_RULES = [r.name for r in REGION_RULES]

print(f"{'screen':<17}{'credential in':>15}{'contact in':>13}   frame flags")
print("-" * 76)

positives = 0
judgements = 0

for name in SCREENS:
    path = HERE / "out" / f"{name}.png"
    if not path.exists():
        print(f"{name:<17}  MISSING IMAGE")
        continue

    from aperture.tiling import tile_grid

    tiles = tile_grid(Image.open(path), columns=2, rows=2)
    cred = [t.index for t in tiles if region_holds(name, "credential", t.box)]
    contact = [t.index for t in tiles if region_holds(name, "personal_contact", t.box)]
    flags = [r for r in FRAME_RULES if frame_holds(name, r)]

    positives += len(cred) + len(contact) + len(flags)
    judgements += len(FRAME_RULES) + len(tiles) * len(TILE_RULES)

    print(f"{name:<17}{str(cred):>15}{str(contact):>13}   "
          f"{', '.join(flags) if flags else '-'}")

print()
print(f"positive labels   : {positives}")
print(f"total judgements  : {judgements}")
print(f"negative labels   : {judgements - positives}")
print()
print("Expectations to eyeball:")
print("  ide_env        credential in the left/top regions (env text x=380-1150)")
print("  deploy_failure credential in the RIGHT regions only (env text x>1032)")
print("  token_in_url   credential in the TOP regions (address bar y=56-92)")
print("  contacts       contact details across all four (rows span the frame)")
print("  bank/dm/id_card  frame flags only, no region labels")
print("  terminal_error, docs, sha_soup, public_metrics, dark_editor  all empty")
