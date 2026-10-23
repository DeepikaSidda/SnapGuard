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
    # tile index -> tighter boxes, when refinement localised the signal inside it.
    refined: dict[int, list[tuple[int, int, int, int]]] = field(default_factory=dict)
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
        """Pixel boxes to paint over.

        Returns the refined sub-region boxes where refinement localised the signal, and
        the whole region where it could not. Refinement asks the model again on a
        quarter of the region, so a hit is still the model's own claim about an area --
        which is what makes it safe to narrow to, unlike a pattern match that only sees
        the secrets it was taught.
        """
        out: list[tuple[int, int, int, int]] = []
        for tile in self.flagged:
            out.extend(self.refined.get(tile.index) or [tile.box])
        return out

    @property
    def injectable(self) -> list[Tile]:
        """The regions actually worth sending: safe AND relevant AND readable.

        This is the `#context-memory` decision in one line. When no question was given,
        relevance was never scored and every cleared region qualifies. When one was,
        the intersection is usually much smaller -- which is the point, since the
        regions that never leave are both the cheapest and the safest.
        """
        # A whole-frame verdict overrides any per-region conclusion. A banking page is
        # condemned by what the frame *is*, not by any one quadrant of it, so every
        # region can look individually clean while nothing may be sent. Without this
        # the summary cheerfully reported "4 of 4 regions selected" on a screen that
        # was refused outright.
        if self.verdict in (Verdict.WITHHOLD, Verdict.UNREADABLE):
            return []

        safe = [t for t in self.cleared if t not in self.illegible]
        if not self.relevant:
            return safe
        keep = {t.index for t in self.relevant}
        return [t for t in safe if t.index in keep]

    def accounting(self, total: int) -> dict[str, int]:
        """Where every region went, in one place so every report agrees.

        Written as a single method because two separate reporters disagreed about this
        and both were wrong: a whole-frame refusal was being filed under "unrelated",
        which reads as housekeeping rather than the safety decision it actually was.
        """
        sending = len(self.injectable)
        if self.verdict is Verdict.WITHHOLD:
            # The frame itself is the problem, so every region is a safety withholding
            # regardless of how any individual one scored.
            return {"sent": 0, "unsafe": total, "skipped": 0, "unreadable": 0}
        if self.verdict is Verdict.UNREADABLE:
            return {"sent": 0, "unsafe": 0, "skipped": 0, "unreadable": total}
        unsafe = len(self.flagged)
        unreadable = len(self.illegible)
        return {
            "sent": sending,
            "unsafe": unsafe,
            "unreadable": unreadable,
            "skipped": max(0, total - sending - unsafe - unreadable),
        }

    def selection_summary(self, total: int) -> str:
        if not total:
            return "no regions"
        a = self.accounting(total)
        bits = [f"{a['sent']} of {total} regions sent"]
        if a["unsafe"]:
            bits.append(f"{a['unsafe']} withheld for safety")
        if a["skipped"]:
            bits.append(f"{a['skipped']} skipped as unrelated")
        if a["unreadable"]:
            bits.append(f"{a['unreadable']} unreadable")
        return ", ".join(bits) + f" ({1 - a['sent'] / total:.0%} kept local)"

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
        # A batch is only skipped when the batched question answers BELOW this. It is
        # much lower than any rule threshold on purpose: see `_coarse_pass`. Measured,
        # clean batches answer around 0.061 and a batch containing a credential answers
        # 0.505 to 0.548, so 0.25 separates them with wide margin on both sides, where
        # the rule's own 0.50 threshold left 0.005.
        coarse_clear_below: float = 0.25,
        # Off by default, and it used to be on. Subdividing a flagged region and
        # re-asking was the first answer to "the redaction covers too much of my
        # screen", and it worked -- 56% of the reported frame down to 24%. Then the grid
        # itself went from 2x2 to 6x4, which gets to 15% on its own, and refinement
        # stopped paying for itself:
        #
        #   6x4            15% covered   11 calls
        #   4x4            22% covered    9 calls
        #   4x4 + refine   19% covered   21 calls
        #
        # Three points of coverage for 2.3x the local requests, on a good case. On a bad
        # case it is far worse than 2.3x, because cost scales with how much fired: a
        # screen that is sensitive everywhere flags most of 24 regions and then pays four
        # more requests for each one. One such screen in the eval set ran for thirteen
        # minutes before this was turned off.
        #
        # Kept, because it is the right tool on a large display where even a 4x3 region
        # is big, and exposed as `aperture --precise`. The lesson is the ordering: make
        # the regions small first, and only then narrow the ones that fire.
        refine_flagged: bool = False,
        # How many times to quarter a flagged region. Each level cuts the covered area
        # by up to four and costs up to four more requests, but only on regions that
        # actually fired. 2 takes a quarter-screen region down to a sixty-fourth of the
        # display, which is about the size of a line of text in an editor.
        refine_depth: int = 1,
        # The shape to subdivide a flagged region into. Horizontal strips were tried on
        # the theory that a credential is a wide, short thing and would fire in one strip
        # rather than two quadrants. Measured, strips and quadrants came out the same
        # (23% vs 24% covered on the reported case), because the limit is the model
        # firing on three of four pieces, not the geometry. Quadrants kept: squarer
        # sub-regions survive the 448px downscale better.
        refine_columns: int = 2,
        refine_rows: int = 2,
        # Do not produce sub-regions smaller than this. Checked against the size of the
        # pieces a subdivision *would* make, not the region being subdivided -- asking
        # the model about a 90x76 crop and then recording a verdict on the answer is
        # exactly the mistake this is meant to prevent. It is what makes the refinement
        # pass self-limiting across display sizes.
        refine_min_side: int = 150,
        # Scored alongside the sensitivity rules when `inspect` is given a question.
        # A factory rather than a Rule, because the question has to quote what the user
        # actually asked, which is not known until inspection time.
        relevance: Callable[[str], Rule] | None = None,
        # Asked per region so a verdict is never recorded about an unreadable smear.
        # `looks_blank` catches a solid colour cheaply; this catches the subtler case.
        legibility: Rule | None = None,
        # 4x3, wider than tall because screens are. This has moved twice and the two
        # moves are worth reading together, because the second one undoes the premise of
        # the first.
        #
        # It started at 2x2, and a user reported three times that the redaction covered
        # too much. Correct: a flagged region *was* the redaction, so a quarter-screen
        # region meant a quarter-screen black box. The fix was finer regions, and 6x4
        # took the reported case from 24% of the screen to 15%.
        #
        # Then `mesh.tighten_all` arrived and reduced a flagged region to the cells
        # actually holding ink. That silently removed the entire reason the grid was
        # fine, and nobody noticed for a day: tightness is now set by the mesh at pixel
        # resolution, and the grid only governs the request count. Measured on the dark
        # editor, coverage is flat at 3.25%-3.38% across every grid from 2x2 to 6x4
        # while the time triples.
        #
        # So 4x3, which is 40% faster than 6x4 on the capture a user actually reported.
        # The full table is in tiling.py.
        columns: int = 6,
        rows: int = 4,
        state: str = "A region of a screen captured on a computer.",
        context_state: str = "A screenshot of an application window on a computer.",
        # Full resolution, and it stays that way. Two attempts to shrink images for
        # speed were measured and both were reverted:
        #
        #   Region tiles. Shrinking kept accuracy at 4/4 on a four-case probe while
        #   collapsing the margins -- a clean region reached 0.48 against a 0.50
        #   threshold at 336px, and a region holding a credential fell to exactly 0.50
        #   at 224px. Accuracy hid the risk.
        #
        #   This whole-frame pass. At 336px the three context rules answered the same
        #   on the fixture screen (0.06/0.07/0.05 against 0.04/0.07/0.05) and saved
        #   2.5s, so it looked free. Measured across the whole eval set it dropped
        #   12/12 to 11/12 and LET token_in_url THROUGH: that frame is caught by
        #   financial_records firing on its reconciliation figures, and at 336px that
        #   detail is gone.
        #
        # Both errors were the same error -- generalising from one screen. See
        # research/README.md section 11.
        context_long_side: int = 448,
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
        self.coarse_clear_below = coarse_clear_below
        self.refine_flagged = refine_flagged
        self.refine_depth = refine_depth
        self.refine_columns = refine_columns
        self.refine_rows = refine_rows
        self.refine_min_side = refine_min_side
        self.relevance = relevance
        self.legibility = legibility
        self.policy = policy or Policy()
        self.columns = columns
        self.rows = rows
        self.state = state
        self.context_state = context_state
        self.context_long_side = context_long_side
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
        reply = self.decider.ask(
            self.context_state, questions, [_shrink(image, self.context_long_side)]
        )
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

        **The coarse pass may only skip work, never clear a frame.** It is judged
        against `coarse_clear_below`, not against each rule's own threshold, and the two
        are deliberately far apart.

        Why: measured on the fixture screen, the batched "does ANY of these contain a
        credential" question answers 0.505 where the rule threshold is 0.500 -- a margin
        of 0.005. A screenshot with a live AWS key in it passed cleanly through a real
        run because that number landed a hair under the line, all four tiles were
        cleared, and no fine pass ever ran. The fine pass on the same tile scores 0.824.

        So the batched question is a cheap way to recognise an *obviously* clean batch
        (clean batches measured 0.061) and nothing more. Anything above
        `coarse_clear_below` goes to the fine pass, which is the only place a region is
        allowed to be cleared.

        Only rules that supplied `any_question` can be screened this way. If none did,
        every batch is treated as hot.
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

            highest = max(reply.value(r.name) for r in screenable)
            if highest >= self.coarse_clear_below:
                hot.append(batch)
                result.notes.append(
                    f"coarse screen returned {highest:.3f} for a batch of "
                    f"{len(batch)}; examined each region individually"
                )
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
                self._refine(tile, fired, result)
            else:
                result.cleared.append(tile)

            if relevance and relevance.fires(reply.value(relevance.name)):
                result.relevant.append(tile)

    def _refine(
        self, tile: Tile, fired: Sequence[Rule], result: GateResult
    ) -> None:
        """Subdivide a flagged region and find out which part of it fired.

        The gate used a 2x2 grid, which makes each region a quarter of the display.
        Black one out to hide a line of text and you have destroyed a file tree, an
        editor pane and a lot of empty space -- which is exactly what a user reported.

        The wrong fix is to trust the deterministic scanner's precise box and cover only
        that: tried, and it left three credentials legible, because the scanner matched
        one secret of four. See `redact.resolve_boxes`.

        The real fix was the grid itself, and the user said so: small regions beat large
        regions that are narrowed afterwards. The default is 4x3. This pass remains
        for the case the grid cannot cover, a region that is large because the display
        is -- ask the model again, smaller. Subdivide the flagged region
        and re-test only the rules that actually fired. Sub-regions that fire give a box
        four times tighter; if none fire, the signal was diffuse and the whole region is
        kept. Cost is bounded because only flagged regions are ever refined, and most
        screens flag nothing.
        """
        if not self.refine_flagged or tile.index < 0:
            return
        boxes = self._narrow(tile.image, tile.box, fired, result, self.refine_depth)
        if boxes and boxes != [tile.box]:
            result.refined[tile.index] = boxes
            area = sum((r - l) * (b - t) for l, t, r, b in boxes)
            whole = tile.width * tile.height
            result.notes.append(
                f"region {tile.index} narrowed to {len(boxes)} box(es), "
                f"{area / whole:.0%} of the region"
            )

    def _narrow(
        self,
        image: Image.Image,
        offset_box: tuple[int, int, int, int],
        fired: Sequence[Rule],
        result: GateResult,
        depth: int,
    ) -> list[tuple[int, int, int, int]]:
        """Recursively quarter `image`, keeping only the parts that still fire.

        Each level cuts the covered area by up to four. One level took a reported case
        from 56% of the frame to 24% and saved the user's file tree; the recursion is
        what takes it further, because a line of text in an editor is a very small
        fraction of a quarter-screen region.

        Recursion stops when the model stops localising -- if every sub-region fires, or
        none does, the signal is diffuse and the parent box is the honest answer. It also
        stops before making sub-regions too small to survive the 448px downscale, since
        below that the model is being asked about a smear. Note that the size test is on
        the pieces this call would create, not on the region it was handed: testing the
        parent lets through exactly one round of questions about smears, which at the
        default grid is every round.
        """
        left, top = offset_box[0], offset_box[1]
        width, height = image.size
        piece_side = min(width / self.refine_columns, height / self.refine_rows)

        if depth <= 0 or piece_side < self.refine_min_side:
            return [offset_box]

        questions = {r.name: r.question for r in fired}
        hits: list[tuple[Image.Image, tuple[int, int, int, int]]] = []
        pieces = tile_grid(image, columns=self.refine_columns, rows=self.refine_rows)
        for sub in pieces:
            if looks_blank(sub.image):
                continue
            reply = self.decider.ask(self.state, questions, [sub.image])
            result.decider_calls += 1
            result.decider_ms += reply.latency_ms
            if any(r.fires(reply.value(r.name)) for r in fired):
                absolute = (
                    left + sub.box[0],
                    top + sub.box[1],
                    left + sub.box[2],
                    top + sub.box[3],
                )
                hits.append((sub.image, absolute))

        # Nothing fired: the parent's signal did not survive subdivision, so trust the
        # parent rather than concluding the content vanished.
        if not hits:
            return [offset_box]
        # Everything fired: the content is spread across the whole region, and four
        # boxes covering it are no better than one.
        if len(hits) == len(pieces):
            return [offset_box]

        narrowed: list[tuple[int, int, int, int]] = []
        for sub_image, absolute in hits:
            narrowed.extend(
                self._narrow(sub_image, absolute, fired, result, depth - 1)
            )
        return narrowed

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


def _shrink(image: Image.Image, long_side: int) -> Image.Image:
    """Downscale for the whole-frame pass. Never upscales."""
    if max(image.size) <= long_side:
        return image
    scale = long_side / max(image.size)
    return image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.LANCZOS,
    )


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
