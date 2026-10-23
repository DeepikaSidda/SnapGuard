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
        # 0.70, and this is the second threshold here to be derived rather than guessed.
        #
        # It started at 0.55, set from one positive: 0.817 on the banking screen against
        # <=0.073 on the four others. A gap that wide makes any line through the middle
        # look safe. Then the eval set grew and `public_metrics` -- a public revenue
        # dashboard, correctly a 'send' -- answered 0.530. Still under the line, but by
        # two hundredths, and research/README.md section 12 recorded it as "the next
        # threshold worth deriving rather than guessing".
        #
        # It then fired for real. A user screenshotted a hackathon page to test that the
        # API key on it would be covered. The page carries a "Free plan" badge, a
        # "Monthly allowance" progress bar, "0 of 2,000 answers used" and a row of
        # large-number cards -- the visual grammar of a billing dashboard, with no money
        # anywhere on it. It answered 0.599, fired, and WITHHOLD outranks REDACT, so the
        # whole frame was refused and the credential rule's correct 0.795 was discarded.
        #
        #     bank statement           0.817   yes
        #     hackathon quota page     0.599   no     <- fired at 0.55
        #     public metrics dashboard 0.530   no
        #
        # 0.70 separates all three with ~0.10 either side. Stated plainly: one positive
        # and two negatives is thin evidence for a threshold, and the honest fix is more
        # labelled positives, which has not been done. But 0.55 is now known to be wrong
        # in the direction that destroys a screenshot, and 0.70 is wrong in no direction
        # yet measured.
        #
        # The failure mode this leaves is a bank page answering between 0.55 and 0.70,
        # which would now pass this rule. It is not unguarded: a statement also carries
        # account numbers, and `private_conversation` and the region rules still run.
        threshold=0.70,
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
        # WITHHOLD, and correctly so when the screen *is* the document. A user tested a
        # PAN card and asked, reasonably, why it did not just cover the card number.
        # Because covering the number protects nothing: the same card carries the name,
        # the father's name, the date of birth, a face photograph, a signature and a QR
        # code that encodes the number. A redaction that leaves the QR code has
        # published the number. There is no clean part of an ID card.
        #
        # The case where that reasoning does NOT hold is an ID occupying one corner of a
        # large screen, and this whole-frame rule used to condemn those too. See
        # `identity_region` in REGION_RULES.
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
        # 0.35, and it is the one threshold here that was derived rather than guessed.
        #
        # It started at 0.50, chosen by eye from a handful of scores: 0.860 on a region
        # holding a fake AWS key against 0.196, 0.075 and 0.071 on clean code, terminal
        # output and prose. A wide gap, so 0.50 looked obviously safe.
        #
        # Then `evalset/calibrate.py` scored all 48 regions of the eval set against
        # region-level ground truth and swept the threshold:
        #
        #     threshold   missed   false alarms      (5 of 48 regions hold a credential)
        #          0.25        1              1
        #          0.35        1              0
        #          0.45        2              0
        #          0.50        3              0      <- the hand-picked value
        #
        # 0.50 missed three of the five. Two of those answered 0.445 and 0.459 -- just
        # under the line I had drawn by eye. 0.35 catches both and costs nothing, and
        # 0.25 buys nothing further while introducing a false alarm.
        #
        # The screen-level verdicts were 12/12 either way, because other rules covered
        # those frames. That is what made the gap invisible: a correct verdict can hide
        # a rule that is barely working. See research/README.md section 12.
        threshold=0.35,
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
