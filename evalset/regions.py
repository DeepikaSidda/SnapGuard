"""Region-level ground truth, derived from the coordinates the screens were drawn at.

Calibration needs to know whether each individual answer was right, not just whether the
screen got the right verdict. That means knowing which *region* of each screen actually
holds a credential, a private conversation, and so on.

Rather than eyeball it, the boxes below are taken from the drawing code in `screens.py`
and `hard_screens.py` -- the same coordinates the content was painted at. A region counts
as containing something when its tile overlaps that box by more than
`MIN_OVERLAP_FRACTION` of the box's area, which stops a two-pixel clip at a seam from
being called a hit.

Every screen here is drawn by us, so this is exact rather than an annotator's opinion.
"""

from __future__ import annotations

from dataclasses import dataclass

Box = tuple[int, int, int, int]

# A tile must cover at least this much of a content box to count as containing it.
MIN_OVERLAP_FRACTION = 0.15


@dataclass(frozen=True)
class Truth:
    """Where sensitive content actually sits on one screen."""

    credential: tuple[Box, ...] = ()
    personal_contact: tuple[Box, ...] = ()
    # Whole-frame properties. Recorded per screen rather than per region.
    private_conversation: bool = False
    financial_records: bool = False
    identity_document: bool = False


# Coordinates below are the ones the content is drawn at in evalset/screens.py and
# evalset/hard_screens.py. All screens are 1920x1080.
SCREENS: dict[str, Truth] = {
    # Editor with production.env open. Credential lines are drawn from x=380, y=110,
    # nine lines at 34px spacing, running to roughly x=1150.
    "ide_env": Truth(credential=((380, 104, 1150, 420),)),

    # Split screen: terminal left, production.env right of the midline at x=960.
    # Env text starts at x=1032 (midline+72), y=110, eleven lines at 34px.
    "deploy_failure": Truth(credential=((1032, 104, 1900, 500),)),

    # Banking: three account cards from y=230 with balances at x~1620, plus a
    # transaction list from y=630. Money is spread across the whole frame.
    "bank": Truth(financial_records=True),

    # A one-to-one conversation, bubbles from y=130 down to y=562, x=400 onward.
    "dm": Truth(private_conversation=True),

    # Licence record: portrait at (110,270)-(340,570), fields at x=380 from y=272.
    "id_card": Truth(identity_document=True),

    # CRM list: emails at x=430, phones at x=920, addresses at x=1230, rows from
    # y=200 at 78px spacing for seven rows.
    "contacts": Truth(personal_contact=((430, 190, 1872, 740),)),

    # A real token in the browser address bar: drawn at (140,65), one line of 15px mono.
    # Also a reconciliation table of volumes, which is why financial_records fires.
    "token_in_url": Truth(
        credential=((140, 56, 1250, 92),),
        financial_records=True,
    ),

    # --- and the screens where there is nothing to find ---
    "terminal_error": Truth(),
    "docs": Truth(),
    "sha_soup": Truth(),
    "public_metrics": Truth(),
    "dark_editor": Truth(),
}


def _overlap_fraction(tile: Box, content: Box) -> float:
    """How much of `content` falls inside `tile`, as a fraction of the content's area."""
    left = max(tile[0], content[0])
    top = max(tile[1], content[1])
    right = min(tile[2], content[2])
    bottom = min(tile[3], content[3])
    if right <= left or bottom <= top:
        return 0.0
    overlapping = (right - left) * (bottom - top)
    area = (content[2] - content[0]) * (content[3] - content[1])
    return overlapping / area if area else 0.0


def region_holds(screen: str, rule: str, tile_box: Box) -> bool:
    """Does this region of this screen actually contain what `rule` asks about?"""
    truth = SCREENS[screen]
    boxes: tuple[Box, ...] = getattr(truth, rule, ())
    if not isinstance(boxes, tuple):
        # A whole-frame property: true of every region or none of them.
        return bool(boxes)
    return any(_overlap_fraction(tile_box, box) >= MIN_OVERLAP_FRACTION for box in boxes)


def frame_holds(screen: str, rule: str) -> bool:
    """Is this whole-frame property true of this screen?"""
    value = getattr(SCREENS[screen], rule, False)
    if isinstance(value, tuple):
        return bool(value)
    return bool(value)
