"""RegionGate: score the regions of an image against a written rubric, coarse to fine.

THE REUSABLE PATTERN. Nothing in this module knows about screenshots, secrets, or
Aperture. It takes an image, a set of narrow typed questions, and a policy, and returns a
per-region verdict. Drop it into any agent that is about to send an image somewhere.

Three things it does that a naive implementation gets wrong, each one measured rather
than guessed (numbers in ../../research/README.md):

1. **It checks the image is really there first.** The upstream docs warn that Decider's
   confidence does not drop when an image is missing or unreadable -- it answers
   confidently at chance. A gate that trusts those numbers is reading noise.

2. **It words batched questions as "any of these".** Images in one request are
   concatenated into a single state, so a question is answered once over all of them.
   Asking "is a credential visible" of four tiles separated dirty from clean by 0.341;
   asking "does ANY of these contain a credential" separated them by 0.488, and lifted
   the positive case from 0.451 to 0.548 -- across a 0.5 threshold that would otherwise
   have missed it.

3. **It never asks the model for the verdict.** Narrow factual questions answered at
   0.96-0.99. One broad "how sensitive is this?" question collapsed to 0.29-0.36
   confidence and caused the only misclassification in the run. The model supplies
   facts; this module's `Policy` composes them.

Coarse to fine, because a request takes up to four images and batching them was 2.4x
faster than asking separately: screen four regions at once, and only split a batch that
comes back hot. A clean screen costs a few requests instead of one per region.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Sequence

from PIL import Image

from ..decider import Decider, looks_blank
from ..tiling import Tile, batched, tile_grid, whole_frame


class Verdict(str, Enum):
    """What the calling code should do. Deliberately small."""

    PASS = "pass"          # nothing found; send it
    REDACT = "redact"      # send it, with the flagged regions obscured
    WITHHOLD = "withhold"  # do not send this at all
    UNREADABLE = "unreadable"  # we could not judge; fail closed

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Rule:
    """One narrow question, and what a 'yes' means for the verdict.

    `question` is a typed question dict from `aperture.decider` -- usually `noul()`.

    `any_phrasing` is the same question reworded for a batch of images. If you leave it
    out the gate will only ever ask this rule one image at a time, which is correct but
    slower. Providing it is what unlocks the coarse pass, and the measured separation
    improvement is large enough to be worth the extra sentence.

    `threshold` is yours, not the model's. It is the policy knob. Bias it toward the
    cheaper mistake for your situation: a gate protecting secrets should fire early.

    `on_match` says what a firing rule implies -- REDACT if the offending region can be
    obscured and the rest is still useful, WITHHOLD if its presence condemns the frame.
    """

    name: str
    question: dict[str, Any]
    threshold: float = 0.5
    on_match: Verdict = Verdict.REDACT
    any_question: dict[str, Any] | None = None
    # Shown to a human, and handed to the generative model to explain itself.
    describes: str = ""

    def fires(self, value: float) -> bool:
        return value >= self.threshold


@dataclass
class RegionFinding:
    """One rule firing on one region."""

    rule: Rule
    tile: Tile
    value: float
    verdict: Verdict

    def __str__(self) -> str:
        return (
            f"{self.rule.name}={self.value:.3f} on {self.tile} -> {self.verdict}"
        )


@dataclass
class GateResult:
    """Everything the gate learned, and what it concluded.

    Keeping the numbers alongside the verdict matters: the generative half of an agent
    needs them to explain itself, and a human overriding the gate deserves to see why.
    """

    verdict: Verdict
    findings: list[RegionFinding] = field(default_factory=list)
    context: dict[str, float] = field(default_factory=dict)
    # Regions that may be sent, and regions that may not.
    cleared: list[Tile] = field(default_factory=list)
    flagged: list[Tile] = field(default_factory=list)
    # The second axis. Populated only when `inspect(asking_about=...)` is given: regions
    # judged to bear on the question actually asked. Safety and usefulness are separate
    # questions, and a region has to pass both to be worth sending.
    relevant: list[Tile] = field(default_factory=list)
    # Per-region scores, keyed by tile index then question name, for the record.
    scores: dict[int, dict[str, float]] = field(default_factory=dict)
    illegible: list[Tile] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Answers recorded for the record rather than to fire a rule -- a `choice` naming
    # what kind of screen this is, for instance. The generative half uses these to
    # explain itself, and they cost nothing extra because they ride along in the same
    # request as the context rules.
    info: dict[str, Any] = field(default_factory=dict)
    decider_calls: int = 0
    decider_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.PASS

    @property
    def reasons(self) -> list[str]:
        out = [f.rule.describes or f.rule.name for f in self.findings]
        return list(dict.fromkeys(out)) + self.notes

    def boxes(self) -> list[tuple[int, int, int, int]]:
        """Pixel boxes of the flagged regions, for a redactor to paint over."""
        return [t.box for t in self.flagged]

    @property
    def injectable(self) -> list[Tile]:
        """The regions actually worth sending: safe AND relevant AND readable.

        This is the `#context-memory` decision in one line. When no question was given,
        relevance was never scored and every cleared region qualifies. When one was,
        the intersection is usually much smaller -- which is the point, since the
        regions that never leave are both the cheapest and the safest.
        """
        safe = [t for t in self.cleared if t not in self.illegible]
        if not self.relevant:
            return safe
        keep = {t.index for t in self.relevant}
        return [t for t in safe if t.index in keep]

    def selection_summary(self, total: int) -> str:
        chosen = len(self.injectable)
        return (
            f"{chosen} of {total} regions selected "
            f"({1 - chosen / total:.0%} of the frame withheld)"
            if total
            else "no regions"
        )

    def summary(self) -> str:
        if self.verdict is Verdict.PASS:
            return f"pass ({self.decider_calls} decisions, {self.decider_ms:.0f} ms)"
        return (
            f"{self.verdict} - {', '.join(self.reasons) or 'no reason recorded'} "
            f"({self.decider_calls} decisions, {self.decider_ms:.0f} ms)"
        )


@dataclass
class Policy:
    """How findings become a verdict. Plain code, by design.

    Separated from the rules so you can keep one rubric and swap the policy -- strict in
    CI, forgiving on a developer's laptop.
    """

    # Below this, a rule's answer is treated as "the model does not know" rather than
    # "no". With a hard-fail gate, not knowing has to mean not sending.
    uncertain_below: float = 0.0
    fail_closed: bool = True

    def decide(self, findings: Sequence[RegionFinding]) -> Verdict:
        if any(f.verdict is Verdict.WITHHOLD for f in findings):
            return Verdict.WITHHOLD
        if any(f.verdict is Verdict.REDACT for f in findings):
            return Verdict.REDACT
        return Verdict.PASS


class RegionGate:
    """Score an image's regions against `rules`, and compose a verdict with `policy`.

    Typical use::

        gate = RegionGate(Decider(), rules=MY_RULES)
        result = gate.inspect(screenshot)
        if result.verdict is Verdict.WITHHOLD:
            ...          # do not send it
        elif result.verdict is Verdict.REDACT:
            send(redact(screenshot, result.boxes()))
        else:
            send(screenshot)
    """

    def __init__(
        self,
        decider: Decider,
        *,
        rules: Sequence[Rule],
        context_rules: Sequence[Rule] = (),
        policy: Policy | None = None,
        info_questions: Mapping[str, dict[str, Any]] | None = None,
        # Scored alongside the sensitivity rules when `inspect` is given a question.
        # A factory rather than a Rule, because the question has to quote what the user
        # actually asked, which is not known until inspection time.
        relevance: Callable[[str], Rule] | None = None,
        # Asked per region so a verdict is never recorded about an unreadable smear.
        # `looks_blank` catches a solid colour cheaply; this catches the subtler case.
        legibility: Rule | None = None,
        # 2x2 by default. The grid is a straight trade: a 3x3 grid of a 1920x1080 screen
        # preserves more detail (tiles downscale 1.4x rather than 2.1x) but costs more
        # requests, and on CPU each request is ~12 s. 2x2 lets the coarse pass screen
        # the entire frame in a single request, so a clean screen costs two calls.
        columns: int = 2,
        rows: int = 2,
        state: str = "A region of a screen captured on a computer.",
        context_state: str = "A screenshot of an application window on a computer.",
        batch_state: str = (
            "Several regions cut from the same screenshot are shown, one after another."
        ),
    ) -> None:
        if not rules:
            raise ValueError("RegionGate needs at least one rule")
        self.decider = decider
        self.rules = list(rules)
        self.context_rules = list(context_rules)
        self.info_questions = dict(info_questions or {})
        self.relevance = relevance
        self.legibility = legibility
        self.policy = policy or Policy()
        self.columns = columns
        self.rows = rows
        self.state = state
        self.context_state = context_state
        self.batch_state = batch_state

    # ---- the three passes --------------------------------------------------

    def _context_pass(self, image: Image.Image, result: GateResult) -> None:
        """Whole-frame questions, asked once.

        Some judgements are about the screen as a whole and do not need the grid: "is
        this a banking page", "is this a private conversation". Measured at 0.98 and
        0.99 on the full frame, so one request is enough and nine would be waste.
        """
        if not self.context_rules and not self.info_questions:
            return
        questions = {r.name: r.question for r in self.context_rules}
        questions.update(self.info_questions)
        reply = self.decider.ask(self.context_state, questions, [image])
        result.decider_calls += 1
        result.decider_ms += reply.latency_ms

        frame = whole_frame(image)
        for rule in self.context_rules:
            value = reply.value(rule.name)
            result.context[rule.name] = value
            if rule.fires(value):
                result.findings.append(
                    RegionFinding(rule, frame, value, rule.on_match)
                )
        for name in self.info_questions:
            answer = reply[name]
            result.info[name] = answer.label if answer.label else answer.value
            result.info[f"{name}_confidence"] = round(answer.confidence, 3)

    def _coarse_pass(
        self, tiles: Sequence[Tile], result: GateResult
    ) -> list[list[Tile]]:
        """Screen batches of tiles, and return the batches that need splitting.

        Only rules that supplied `any_question` can be screened this way. Anything else
        falls through to the fine pass.
        """
        screenable = [r for r in self.rules if r.any_question]
        if not screenable:
            return [list(b) for b in batched(tiles)]

        questions = {r.name: r.any_question for r in screenable}
        hot: list[list[Tile]] = []
        for batch in batched(tiles):
            if len(batch) == 1:
                hot.append(batch)
                continue
            reply = self.decider.ask(
                self.batch_state, questions, [t.image for t in batch]
            )
            result.decider_calls += 1
            result.decider_ms += reply.latency_ms
            if any(r.fires(reply.value(r.name)) for r in screenable):
                hot.append(batch)
            else:
                result.cleared.extend(batch)
        return hot

    def _fine_pass(
        self,
        tiles: Sequence[Tile],
        result: GateResult,
        relevance: Rule | None = None,
    ) -> None:
        """One request per tile, every rule batched into it.

        Batching is the whole reason this is affordable: measured on an i5-1235U, the
        first question about an image costs 9.5 s and each additional one 2.8 s. Asking
        sensitivity, relevance and legibility together costs barely more than asking
        about safety alone.
        """
        base = {r.name: r.question for r in self.rules}
        if relevance:
            base[relevance.name] = relevance.question
        if self.legibility:
            base[self.legibility.name] = self.legibility.question

        for tile in tiles:
            if looks_blank(tile.image):
                # Cheap and certain, and it costs no decider call. A blank region is
                # neither a risk nor any use, so it is cleared and marked unreadable.
                result.cleared.append(tile)
                result.illegible.append(tile)
                continue

            reply = self.decider.ask(self.state, base, [tile.image])
            result.decider_calls += 1
            result.decider_ms += reply.latency_ms
            result.scores[tile.index] = {
                name: answer.value for name, answer in reply.answers.items()
            }

            if self.legibility and not self.legibility.fires(
                reply.value(self.legibility.name)
            ):
                # Nothing readable here, so do not record a verdict about it. The
                # model's confidence would not have told us this.
                result.illegible.append(tile)

            fired = [r for r in self.rules if r.fires(reply.value(r.name))]
            for rule in fired:
                result.findings.append(
                    RegionFinding(rule, tile, reply.value(rule.name), rule.on_match)
                )
            if fired:
                result.flagged.append(tile)
            else:
                result.cleared.append(tile)

            if relevance and relevance.fires(reply.value(relevance.name)):
                result.relevant.append(tile)

    # ---- the entry point ---------------------------------------------------

    def inspect(
        self,
        image: Image.Image,
        *,
        hint_boxes: Sequence[tuple[int, int, int, int]] = (),
        skip_coarse: bool = False,
        asking_about: str | None = None,
    ) -> GateResult:
        """Inspect `image` and return a verdict.

        `hint_boxes` lets a cheaper, exact detector run first and point the gate at
        regions worth a close look -- an OCR-plus-regex pass, say. Tiles overlapping a
        hint skip the coarse screen and go straight to the fine pass, because we already
        have reason to believe something is there.

        `asking_about` turns on the second axis. Give it the user's actual question and
        every region is also scored for whether it bears on that question, so
        `result.injectable` becomes the intersection of safe and useful rather than just
        safe. This necessarily costs more: relevance is per-region, so the coarse screen
        is skipped and every region gets a request. Worth it when the alternative is
        sending four regions where one would do.
        """
        result = GateResult(verdict=Verdict.PASS)
        relevance = (
            self.relevance(asking_about)
            if (self.relevance and asking_about)
            else None
        )

        # 1. Is there anything to look at? The model will not tell us, so we check.
        if looks_blank(image):
            result.verdict = (
                Verdict.UNREADABLE if self.policy.fail_closed else Verdict.PASS
            )
            result.notes.append(
                "the image is blank or featureless, so no judgement is possible; "
                "the model would have answered confidently at chance"
            )
            return result

        # 2. Whole-frame context.
        self._context_pass(image, result)

        # 3. Regions, coarse then fine.
        tiles = tile_grid(image, columns=self.columns, rows=self.rows)
        hinted = [t for t in tiles if _overlaps_any(t.box, hint_boxes)]
        rest = [t for t in tiles if t not in hinted]

        to_examine = list(hinted)
        if skip_coarse or relevance is not None:
            # Relevance has to be known for every region, not just the suspicious ones,
            # so the coarse screen cannot help us here.
            to_examine.extend(rest)
        else:
            for batch in self._coarse_pass(rest, result):
                to_examine.extend(batch)

        self._fine_pass(to_examine, result, relevance=relevance)

        result.verdict = self.policy.decide(result.findings)
        return result


def _overlaps_any(
    box: tuple[int, int, int, int], others: Sequence[tuple[int, int, int, int]]
) -> bool:
    left, top, right, bottom = box
    for other_left, other_top, other_right, other_bottom in others:
        if (
            left < other_right
            and right > other_left
            and top < other_bottom
            and bottom > other_top
        ):
            return True
    return False
