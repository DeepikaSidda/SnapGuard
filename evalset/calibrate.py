"""Does Decider 2B's calibration survive on this task?

AWS publish an expected calibration error of 0.023 for this checkpoint on POPE. That is a
claim about their benchmark, not about screenshots of a developer's screen, and the whole
project leans on those confidences being honest -- the thresholds in `questions.py` are
the policy, and a threshold is only meaningful if the number it compares against means
something.

So: collect every individual judgement the gate makes across the eval set, check each one
against region-level ground truth, and ask whether the confidences line up with how often
the model is actually right.

Three outputs:

  1. A reliability table. Of the answers given at 0.8-0.9 confidence, how many were
     correct? If calibration holds, about 85%.
  2. Expected calibration error and Brier score, computed the same way AWS compute theirs,
     so the numbers are comparable to the published ones.
  3. A threshold sweep: for each rule, what does moving the threshold do to misses and
     false alarms? This is the part that replaces hand-picked numbers with measured ones.

Note on what "confidence" means for a yes/no question. A noul returns P(true). An answer
of 0.05 is not an unconfident yes, it is a confident no -- so the prediction is
`p >= 0.5` and the confidence in that prediction is `max(p, 1-p)`, which is the standard
treatment for binary classifiers and what makes these numbers comparable.

    python evalset/calibrate.py            # run it
    python evalset/calibrate.py --reuse    # re-analyse the saved answers, no model calls
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from snapguard.decider import Decider  # noqa: E402
from snapguard.questions import CONTEXT_RULES, REGION_RULES  # noqa: E402
from snapguard.tiling import tile_grid  # noqa: E402
from regions import SCREENS, frame_holds, region_holds  # noqa: E402

OUT = HERE / "out"
ANSWERS = HERE / "calibration_answers.json"
DIAGRAM = OUT / "_calibration.png"

# Ten bins would leave two or three answers in several of them, which says nothing.
# Five bins over the 0.5-1.0 confidence range is what this sample size supports.
BINS = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]


@dataclass
class Judgement:
    """One answer, and whether it was right."""

    screen: str
    rule: str
    scope: str          # "frame" or "region N"
    probability: float  # P(true), as returned
    truth: bool
    latency_ms: float

    @property
    def predicted(self) -> bool:
        return self.probability >= 0.5

    @property
    def confidence(self) -> float:
        """How sure the model was of the answer it gave, not of 'true'."""
        return self.probability if self.predicted else 1.0 - self.probability

    @property
    def correct(self) -> bool:
        return self.predicted == self.truth


# --------------------------------------------------------------------------- collecting


def collect() -> list[Judgement]:
    decider = Decider()
    decider.require_vision()
    print(decider.banner())

    judgements: list[Judgement] = []
    frame_questions = {r.name: r.question for r in CONTEXT_RULES}
    region_questions = {r.name: r.question for r in REGION_RULES}

    for i, screen in enumerate(SCREENS, 1):
        path = OUT / f"{screen}.png"
        if not path.exists():
            print(f"  skipping {screen}: not generated")
            continue
        image = Image.open(path)
        print(f"\n[{i}/{len(SCREENS)}] {screen}")

        # Whole-frame rules, one request.
        reply = decider.ask(
            "A screenshot of an application window on a computer.",
            frame_questions,
            [image],
        )
        for rule in CONTEXT_RULES:
            p = reply.value(rule.name)
            truth = frame_holds(screen, rule.name)
            judgements.append(
                Judgement(screen, rule.name, "frame", p, truth, reply.latency_ms)
            )
            mark = "ok " if (p >= 0.5) == truth else "MISS"
            print(f"    [{mark}] frame      {rule.name:22s} {p:.3f}  truth={truth}")

        # Region rules, one request per region.
        for tile in tile_grid(image, columns=2, rows=2):
            reply = decider.ask(
                "A region of a screen captured on a computer.",
                region_questions,
                [tile.image],
            )
            for rule in REGION_RULES:
                p = reply.value(rule.name)
                truth = region_holds(screen, rule.name, tile.box)
                judgements.append(
                    Judgement(
                        screen, rule.name, f"region {tile.index}", p, truth,
                        reply.latency_ms,
                    )
                )
                mark = "ok " if (p >= 0.5) == truth else "MISS"
                print(f"    [{mark}] region {tile.index}   {rule.name:22s} "
                      f"{p:.3f}  truth={truth}")

    ANSWERS.write_text(
        json.dumps([asdict(j) for j in judgements], indent=2), encoding="utf-8"
    )
    print(f"\nwrote {len(judgements)} judgements to {ANSWERS}")
    return judgements


def load() -> list[Judgement]:
    raw = json.loads(ANSWERS.read_text(encoding="utf-8"))
    return [Judgement(**row) for row in raw]


# --------------------------------------------------------------------------- analysis


def reliability(judgements: list[Judgement]):
    """Accuracy against stated confidence, per bin."""
    rows = []
    for low, high in BINS:
        in_bin = [j for j in judgements if low <= j.confidence < high]
        if not in_bin:
            rows.append((low, high, 0, None, None))
            continue
        accuracy = sum(j.correct for j in in_bin) / len(in_bin)
        mean_conf = statistics.mean(j.confidence for j in in_bin)
        rows.append((low, high, len(in_bin), mean_conf, accuracy))
    return rows


def expected_calibration_error(judgements: list[Judgement]) -> float:
    """Weighted mean gap between stated confidence and measured accuracy."""
    total = len(judgements)
    error = 0.0
    for _, _, count, mean_conf, accuracy in reliability(judgements):
        if count and mean_conf is not None and accuracy is not None:
            error += (count / total) * abs(accuracy - mean_conf)
    return error


def brier(judgements: list[Judgement]) -> float:
    """Mean squared error of P(true) against the 0/1 outcome. Lower is better."""
    return statistics.mean(
        (j.probability - (1.0 if j.truth else 0.0)) ** 2 for j in judgements
    )


def sweep(judgements: list[Judgement], rule: str):
    """What each threshold would cost, for one rule."""
    subset = [j for j in judgements if j.rule == rule]
    rows = []
    for threshold in [round(0.05 * i, 2) for i in range(2, 19)]:
        misses = sum(1 for j in subset if j.truth and j.probability < threshold)
        false_alarms = sum(1 for j in subset if not j.truth and j.probability >= threshold)
        rows.append((threshold, misses, false_alarms, len(subset)))
    return rows


# --------------------------------------------------------------------------- diagram


def draw_diagram(judgements: list[Judgement], path: Path) -> None:
    """A reliability diagram. Perfect calibration is the diagonal."""
    W = H = 620
    pad = 86
    plot = W - pad * 2

    img = Image.new("RGB", (W, H + 70), (18, 18, 22))
    d = ImageDraw.Draw(img)

    def font(size: int, bold: bool = False):
        for name in (("segoeuib.ttf",) if bold else ("segoeui.ttf",)):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                pass
        return ImageFont.load_default()

    def xy(conf: float, acc: float) -> tuple[float, float]:
        """x is confidence over 0.5-1.0; y is accuracy over the FULL 0-1 range.

        The y axis deliberately runs from zero rather than from 0.5. The first version
        assumed accuracy could not fall below 0.5 and crashed when a bin measured 0% --
        which is exactly the bin worth seeing, since the 0.5-0.6 band was wrong every
        time. An axis that cannot draw the failure is the wrong axis.
        """
        return (pad + (conf - 0.5) * 2 * plot, pad + plot - acc * plot)

    d.text((pad, 26), "Is Decider 2B's confidence honest on this task?",
           font=font(19, True), fill=(240, 240, 247))
    d.text((pad, 52), "the diagonal is perfect calibration; bars are measured accuracy",
           font=font(13), fill=(150, 150, 165))

    # Grid. x is confidence (0.5-1.0), y is accuracy (0-1).
    for t in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
        x, _ = xy(t, 0)
        d.line([(x, pad), (x, pad + plot)], fill=(44, 44, 52))
        d.text((x - 12, pad + plot + 10), f"{t:.1f}", font=font(12), fill=(130, 130, 145))
    for a in (0.0, 0.25, 0.5, 0.75, 1.0):
        _, y = xy(0.5, a)
        d.line([(pad, y), (pad + plot, y)], fill=(44, 44, 52))
        d.text((pad - 40, y - 7), f"{a:.0%}", font=font(12), fill=(130, 130, 145))
    # Perfect calibration: accuracy equals stated confidence.
    d.line([xy(0.5, 0.5), xy(1.0, 1.0)], fill=(110, 110, 130), width=2)

    for (low, high), (_, _, count, mean_conf, accuracy) in zip(
        BINS, reliability(judgements)
    ):
        if not count or mean_conf is None or accuracy is None:
            continue
        x0, _ = xy(low, 0.5)
        x1, _ = xy(min(high, 1.0), 0.5)
        _, y_acc = xy(0.5, accuracy)
        _, y_base = xy(0.5, 0.0)
        # Green when accuracy meets or beats the stated confidence, red when it falls
        # short -- overconfidence is the failure that matters for a safety gate.
        colour = (46, 190, 110) if accuracy >= mean_conf - 0.02 else (224, 69, 63)
        d.rectangle([x0 + 4, y_acc, x1 - 4, y_base], fill=(*colour, 90), outline=colour,
                    width=2)
        d.text((x0 + 8, y_acc - 20), f"{accuracy:.0%}  n={count}",
               font=font(12, True), fill=colour)

    d.text((pad, H + 10), "stated confidence (x)  /  measured accuracy (y)",
           font=font(13), fill=(150, 150, 165))
    ece = expected_calibration_error(judgements)
    d.text((pad, H + 32),
           f"ECE {ece:.3f}   -   AWS publish 0.023 for this checkpoint   -   "
           f"{len(judgements)} judgements",
           font=font(13, True), fill=(240, 240, 247))
    img.save(path)


# --------------------------------------------------------------------------- report


def report(judgements: list[Judgement]) -> None:
    correct = sum(j.correct for j in judgements)
    print("\n" + "=" * 78)
    print(f"  {len(judgements)} individual judgements, {correct} correct "
          f"({correct / len(judgements):.1%})")
    print("=" * 78)

    print("\nRELIABILITY -- does the number mean what it says?")
    print(f"  {'confidence':<14}{'n':>5}{'says':>9}{'actually':>11}{'gap':>9}")
    print("  " + "-" * 50)
    for low, high, count, mean_conf, accuracy in reliability(judgements):
        label = f"{low:.1f} - {min(high, 1.0):.1f}"
        if not count:
            print(f"  {label:<14}{0:>5}{'-':>9}{'-':>11}{'-':>9}")
            continue
        gap = accuracy - mean_conf
        flag = "" if gap >= -0.02 else "  <- overconfident"
        print(f"  {label:<14}{count:>5}{mean_conf:>9.0%}{accuracy:>11.0%}"
              f"{gap:>+9.1%}{flag}")

    ece = expected_calibration_error(judgements)
    print(f"\n  expected calibration error : {ece:.3f}")
    print(f"  Brier score                : {brier(judgements):.3f}")
    print(f"  AWS publish for this checkpoint: ECE 0.023, Brier 0.192 (POPE-adversarial)")
    print("  Their figure is on their benchmark; this is on screenshots of a screen.")

    print("\nTHRESHOLD SWEEP -- what the policy knob actually costs")
    for rule in (r.name for r in REGION_RULES):
        subset = [j for j in judgements if j.rule == rule]
        positives = sum(1 for j in subset if j.truth)
        print(f"\n  {rule}  ({positives} of {len(subset)} regions truly contain it)")
        print(f"    {'threshold':>10}{'missed':>9}{'false alarms':>15}")
        best = None
        for threshold, misses, false_alarms, _ in sweep(judgements, rule):
            marker = ""
            # A missed credential is the expensive mistake, so prefer the highest
            # threshold that still misses nothing.
            if misses == 0 and (best is None or threshold > best[0]):
                best = (threshold, false_alarms)
                marker = "  <- no misses"
            if threshold in (0.25, 0.35, 0.45, 0.5, 0.55, 0.6, 0.7, 0.8):
                print(f"    {threshold:>10.2f}{misses:>9}{false_alarms:>15}{marker}")
        if best:
            print(f"    highest threshold with zero misses: {best[0]:.2f} "
                  f"({best[1]} false alarms)")

    print("\nWORST ANSWERS -- confidently wrong")
    wrong = sorted((j for j in judgements if not j.correct),
                   key=lambda j: -j.confidence)[:6]
    if not wrong:
        print("  none")
    for j in wrong:
        print(f"  {j.screen:<16}{j.scope:<11}{j.rule:<22}"
              f"said {j.probability:.3f}, truth {j.truth}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure Decider's calibration here.")
    parser.add_argument("--reuse", action="store_true",
                        help="re-analyse saved answers without calling the model")
    args = parser.parse_args()

    if args.reuse:
        if not ANSWERS.exists():
            print(f"no saved answers at {ANSWERS}; run without --reuse first")
            return 1
        judgements = load()
        print(f"re-analysing {len(judgements)} saved judgements")
    else:
        started = time.time()
        judgements = collect()
        print(f"collection took {(time.time() - started) / 60:.1f} minutes")

    if not judgements:
        print("nothing to analyse")
        return 1

    report(judgements)
    draw_diagram(judgements, DIAGRAM)
    print(f"\nwrote {DIAGRAM}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
