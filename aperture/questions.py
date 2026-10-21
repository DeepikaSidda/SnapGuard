"""Aperture's rubric: the questions put to Decider, and the thresholds over them.

Kept apart from the gate so the gate stays generic and this file stays readable. If you
are adapting Aperture to your own definition of sensitive, this is the only file you
need to touch.

Three conventions, each one earned from measurement (../research/README.md):

* Every rule is a `noul` about **one concrete, visible thing**. No rule asks for an
  overall judgement, because the one broad question we tried returned 0.29-0.36
  confidence and caused the run's only mistake.
* Every rule carries an `any_question`: the same question reworded for a batch of
  regions. Batched requests answer over the combined state, and the rewording moved
  separation from 0.341 to 0.488.
* `true`/`false` descriptions are written as carefully as the question. A noul renders
  as a two-option list, so those strings carry much of the signal.

Thresholds are policy, not truth. They are ours, set from measured separations, and they
deliberately sit low: the cheap mistake here is withholding something harmless, and the
expensive one is leaking a key.
"""

from __future__ import annotations

from .decider import choice, noul
from .patterns.region_gate import Rule, Verdict

# --------------------------------------------------------------------------- context
#
# Asked once about the whole frame. Measured on full 1920x1080 screens: these separate
# cleanly without any cropping, because they are about layout and colour rather than
# glyphs (private_conversation 0.862 vs <=0.042; shows_money 0.817 vs <=0.073).

SCREEN_KIND = {
    "screen_kind": choice(
        "What kind of screen is shown in this image?",
        {
            "code_editor": "a source code editor or IDE showing files and code",
            "terminal": "a command line terminal showing console output",
            "banking": "a bank, payroll or financial account page showing money",
            "private_chat": "a personal one-to-one messaging conversation",
            "documentation": "public documentation, a reference manual or an article",
            "browser": "a web browser showing an ordinary public web page",
            "spreadsheet": "a spreadsheet or table of records",
        },
    )
}
"""Informational, not a rule. Recorded so the generative half can say what it saw.

Measured 5/5 on the eval screens, at 0.96-0.99 on the three that matter. It is not used
to fire anything because a screen's *kind* is not the same as its sensitivity -- a code
editor is usually fine, and this one happened to have a secrets file open.
"""


CONTEXT_RULES: tuple[Rule, ...] = (
    Rule(
        name="private_conversation",
        question=noul(
            "Does this screen show a private conversation between named people?",
            true="a personal message thread between individuals is visible",
            false="no private conversation is shown",
        ),
        # Measured 0.862 on the DM screen, <=0.042 on all four others. A wide gap, so
        # the threshold can sit high enough to avoid firing on, say, a code review.
        threshold=0.55,
        # There is no useful way to redact a conversation: the whole frame is the
        # conversation. Withhold it and ask for a narrower screenshot instead.
        on_match=Verdict.WITHHOLD,
        describes="a private conversation between named people is on screen",
    ),
    Rule(
        name="financial_records",
        question=noul(
            "Does this screen show someone's account balances or financial records?",
            true="monetary amounts belonging to a person or company are visible",
            false="no personal financial information is shown",
        ),
        # Measured 0.817 on the banking screen, <=0.073 elsewhere.
        threshold=0.55,
        on_match=Verdict.WITHHOLD,
        describes="account balances or financial records are on screen",
    ),
    Rule(
        name="identity_document",
        question=noul(
            "Does this screen show an identity document, passport, licence or a "
            "photograph of a person's face?",
            true="an identity document or a person's face is visible",
            false="no identity document or face is visible",
        ),
        threshold=0.55,
        on_match=Verdict.WITHHOLD,
        describes="an identity document or a person's face is on screen",
    ),
)
"""Whole-frame rules. All WITHHOLD, because none of them can be fixed by blurring a
corner -- if the screen *is* a bank statement, there is no clean part to send."""


# --------------------------------------------------------------------------- regions
#
# Asked per region, and in batches during the coarse pass. These are the ones that need
# a crop: the credential question scored 0.643 on a full frame and 0.884 on the region
# containing the key.

REGION_RULES: tuple[Rule, ...] = (
    Rule(
        name="credential",
        question=noul(
            "Is an access key, password, token or other secret value visible as text?",
            true="a credential or secret value can be read in the image",
            false="no credential or secret value is visible",
        ),
        any_question=noul(
            "Across all the regions shown, does ANY of them contain an access key, "
            "password, token or other secret value visible as text?",
            true="at least one of the regions shows a credential",
            false="none of the regions shows a credential",
        ),
        # Measured per-region: 0.860 on the region with a fake AWS key; 0.196, 0.075 and
        # 0.071 on clean code, terminal output and prose. 0.50 sits in the gap.
        threshold=0.50,
        on_match=Verdict.REDACT,
        describes="a credential or secret value is visible in part of the screen",
    ),
    Rule(
        name="personal_contact",
        question=noul(
            "Are a real person's contact details visible, such as an email address, "
            "phone number or home address?",
            true="personal contact details for an individual are visible",
            false="no personal contact details are visible",
        ),
        any_question=noul(
            "Across all the regions shown, does ANY of them show a real person's "
            "email address, phone number or home address?",
            true="at least one region shows personal contact details",
            false="no region shows personal contact details",
        ),
        threshold=0.60,
        on_match=Verdict.REDACT,
        describes="a person's contact details are visible in part of the screen",
    ),
)
"""Region rules. All REDACT: these sit in one corner of a screen, and blacking that
corner out usually leaves the part you actually wanted to send."""


# --------------------------------------------------------------------------- relevance
#
# The second half of the context-and-memory decision. Sensitivity decides what *may* be
# sent; relevance decides what is *worth* sending. Scoring both and intersecting them is
# what keeps most of the frame on the machine even when nothing is sensitive.


def relevance_rule(asking_about: str) -> Rule:
    """A region rule that scores whether a region relates to the user's question.

    The question is built per request because it has to quote what the user asked. Kept
    narrow for the same reason as everything else: "does this region relate to X" is a
    concrete visual judgement, where "how useful is this region" is not.
    """
    return Rule(
        name="relevant",
        question=noul(
            f'Does this part of the screen relate to the following question from the '
            f'user: "{asking_about}"?',
            true="this region shows something that helps answer the question",
            false="this region is unrelated to the question",
        ),
        any_question=noul(
            f'Across all the regions shown, does ANY of them relate to the following '
            f'question from the user: "{asking_about}"?',
            true="at least one region helps answer the question",
            false="none of the regions relates to the question",
        ),
        threshold=0.50,
        describes="relates to what the user asked",
    )


LEGIBLE = Rule(
    name="legible",
    question=noul(
        "Is there readable text or a recognisable interface in this image?",
        true="text or interface elements can be made out",
        false="the image is blank, blurred or featureless",
    ),
    threshold=0.40,
    describes="readable",
)
"""A second line of defence behind `looks_blank()`.

`looks_blank` catches a solid colour by pixel variance, which is cheap and certain. This
catches the subtler case -- a region that has pixels but nothing readable -- so the gate
does not record a confident verdict about a smear. Note the docs' warning: the model's
confidence will not tell you the image was unreadable, so something has to.
"""
