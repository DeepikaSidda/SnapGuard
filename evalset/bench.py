"""Decider 2B against Amazon Nova on the same screening questions.

The honest comparison, which means reporting where we lose as well as where we win. The
community's own benchmark of this model found it ~4x faster and meaningfully *less*
accurate than a Bedrock LLM on the same questions, and twenty-odd hackathon entries are
quoting a speed multiple without the accuracy column. Both columns are here.

What is compared: for each labelled screen, "may this be sent to a cloud model as it
stands?" Decider answers via the gate. Nova answers the same questions as free text,
parsed back to yes/no. Same images, same thresholds, same ground truth.

One asymmetry worth naming up front, because it is the whole point rather than a flaw:
asking Nova to screen the image requires **sending it the image**. On the screens that
should never be sent, the baseline has already lost by the time it answers. It is
measured anyway, because knowing the accuracy cost of the local model is the only way to
judge whether the trade is worth making.

    .\\.venv\\Scripts\\python.exe evalset\\bench.py
    .\\.venv\\Scripts\\python.exe evalset\\bench.py --skip-cloud   # local only
"""

from __future__ import annotations

import argparse
import io
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aperture.decider import Decider  # noqa: E402
from aperture.patterns import Policy, RegionGate, Verdict  # noqa: E402
from aperture.questions import CONTEXT_RULES, REGION_RULES, SCREEN_KIND  # noqa: E402
from aperture.scanner import ocr_available, scan  # noqa: E402

SCREENS = Path(__file__).parent / "out"
RESULTS = Path(__file__).parent / "results.json"

# Ground truth. "send" means the frame may go to a cloud model untouched; "hold" means
# something on it must be withheld or redacted first.
TRUTH: dict[str, str] = {
    # --- the straightforward cases (screens.py) ---
    "ide_env": "hold",          # credentials file open
    "bank": "hold",             # account balances
    "dm": "hold",               # private conversation
    "deploy_failure": "hold",   # half the screen is a .env
    "terminal_error": "send",   # a stack trace
    "docs": "send",             # public documentation
    # --- the adversarial cases (hard_screens.py) ---
    # Three false-positive traps: these look alarming and are harmless. A gate that
    # holds them is a gate people switch off.
    "sha_soup": "send",         # git SHAs, UUIDs, digests -- secret-shaped, not secret
    "public_metrics": "send",   # a dashboard full of money belonging to nobody
    "dark_editor": "send",      # ordinary code, very low contrast
    # One false-negative trap: a real token in a browser address bar, small and easy
    # to miss, which is the case the 448 px downscale handles worst.
    "token_in_url": "hold",
    # --- coverage for two rules that shipped with no test image at all ---
    # identity_document and personal_contact were written from intuition and never
    # verified, which is worse than omitting them, because the README advertised them.
    "id_card": "hold",    # licence layout: portrait, name, DOB, document number
    "contacts": "hold",   # CRM list: names, emails, phone numbers, home addresses
}

# Written after the rubric was fixed, specifically to break it, and no threshold was
# changed in response. The honest comparison lives here; the other six were development
# material and Decider has effectively seen them.
HELD_OUT = frozenset({
    "sha_soup", "public_metrics", "dark_editor", "token_in_url",
    # Also held out: written after the rubric was fixed, to cover rules that had never
    # been exercised. No threshold was adjusted after seeing the results.
    "id_card", "contacts",
})

NOVA_MODELS = ("us.amazon.nova-2-lite-v1:0", "us.amazon.nova-pro-v1:0")

# Published Bedrock on-demand pricing for Nova, USD per 1k tokens, us-east-1. Hard-coded
# so the cost column is reproducible; check the pricing page before quoting these.
NOVA_PRICES = {
    "us.amazon.nova-2-lite-v1:0": (0.00006, 0.00024),
    "us.amazon.nova-pro-v1:0": (0.0008, 0.0032),
}

NOVA_PROMPT = """You are screening a screenshot before it is sent to a cloud AI service.

Answer these questions about the image, one per line, as exactly "yes" or "no":
CREDENTIAL: is an access key, password, token or secret value visible as text?
PRIVATE_CHAT: does it show a private conversation between named people?
FINANCIAL: does it show someone's account balances or financial records?
IDENTITY: does it show an identity document or a person's face?

Then a final line:
VERDICT: HOLD if any answer above is yes, otherwise SEND

Output only those five lines."""


@dataclass
class Row:
    screen: str
    truth: str
    predicted: str
    latency_ms: float
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    detail: str = ""

    @property
    def correct(self) -> bool:
        return self.truth == self.predicted


@dataclass
class Scoreboard:
    name: str
    rows: list[Row] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return sum(r.correct for r in self.rows) / len(self.rows) if self.rows else 0.0

    @property
    def median_ms(self) -> float:
        return statistics.median([r.latency_ms for r in self.rows]) if self.rows else 0.0

    @property
    def total_cost(self) -> float:
        return sum(r.cost_usd for r in self.rows)

    def missed_holds(self) -> list[str]:
        """The expensive mistake: a frame that should have been held, let through."""
        return [r.screen for r in self.rows if r.truth == "hold" and r.predicted == "send"]

    def false_holds(self) -> list[str]:
        """The cheap mistake: a harmless frame withheld."""
        return [r.screen for r in self.rows if r.truth == "send" and r.predicted == "hold"]


# --------------------------------------------------------------------------- local


def run_local(use_ocr: bool = True) -> Scoreboard:
    decider = Decider()
    decider.require_vision()
    gate = RegionGate(
        decider,
        rules=REGION_RULES,
        context_rules=CONTEXT_RULES,
        info_questions=SCREEN_KIND,
        policy=Policy(fail_closed=True),
    )
    board = Scoreboard("Strands Decider 2B (local, CPU)")
    ocr_on = use_ocr and ocr_available()

    for name, truth in TRUTH.items():
        image = Image.open(SCREENS / f"{name}.png")
        started = time.perf_counter()
        hints = [s.box for s in scan(image)] if ocr_on else []
        result = gate.inspect(image, hint_boxes=hints)
        elapsed = (time.perf_counter() - started) * 1000

        predicted = "send" if result.verdict is Verdict.PASS else "hold"
        board.rows.append(
            Row(
                screen=name,
                truth=truth,
                predicted=predicted,
                latency_ms=elapsed,
                tokens_in=0,  # local: no billable tokens
                cost_usd=0.0,
                detail=f"{result.verdict.value}; {result.decider_calls} calls; "
                       f"{'; '.join(result.reasons) or 'nothing fired'}",
            )
        )
        flag = "ok  " if predicted == truth else "MISS"
        print(f"  [{flag}] {name:16s} {predicted:5s} (truth {truth:5s}) "
              f"{elapsed / 1000:5.1f}s  {result.decider_calls} calls  "
              f"{'; '.join(result.reasons)[:46]}")
    return board


# --------------------------------------------------------------------------- cloud


def _png(image: Image.Image, long_side: int = 1100) -> bytes:
    img = image.convert("RGB")
    if max(img.size) > long_side:
        scale = long_side / max(img.size)
        img = img.resize((round(img.width * scale), round(img.height * scale)),
                         Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def run_cloud(model_id: str) -> Scoreboard:
    import boto3

    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    board = Scoreboard(f"{model_id} (Amazon Bedrock)")
    price_in, price_out = NOVA_PRICES.get(model_id, (0.0, 0.0))

    for name, truth in TRUTH.items():
        image = Image.open(SCREENS / f"{name}.png")
        started = time.perf_counter()
        try:
            response = client.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [
                    {"text": NOVA_PROMPT},
                    {"image": {"format": "png", "source": {"bytes": _png(image)}}},
                ]}],
                inferenceConfig={"maxTokens": 200, "temperature": 0.0},
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {name:16s} {str(exc)[:70]}")
            continue
        elapsed = (time.perf_counter() - started) * 1000

        text = response["output"]["message"]["content"][0]["text"]
        predicted = "hold" if "HOLD" in text.upper() else "send"
        usage = response["usage"]
        cost = (usage["inputTokens"] / 1000) * price_in + (
            usage["outputTokens"] / 1000) * price_out

        board.rows.append(
            Row(
                screen=name,
                truth=truth,
                predicted=predicted,
                latency_ms=elapsed,
                tokens_in=usage["inputTokens"],
                tokens_out=usage["outputTokens"],
                cost_usd=cost,
                detail=text.replace("\n", " | ")[:160],
            )
        )
        flag = "ok  " if predicted == truth else "MISS"
        print(f"  [{flag}] {name:16s} {predicted:5s} (truth {truth:5s}) "
              f"{elapsed / 1000:5.1f}s  {usage['inputTokens']:5d} tok  ${cost:.5f}")
    return board


# --------------------------------------------------------------------------- report


def report(boards: list[Scoreboard]) -> None:
    print("\n" + "=" * 78)
    print("  SUMMARY")
    print("=" * 78)
    print(f"  {'system':<42} {'acc':>6} {'median':>9} {'cost':>10}")
    print("  " + "-" * 74)
    for board in boards:
        print(f"  {board.name:<42} {board.accuracy:>5.0%} "
              f"{board.median_ms / 1000:>8.1f}s "
              f"{'free' if board.total_cost == 0 else f'${board.total_cost:.5f}':>10}")

    print("\n  Mistakes, split by which kind:")
    for board in boards:
        missed, false = board.missed_holds(), board.false_holds()
        print(f"    {board.name}")
        print(f"      let through that should have been held : "
              f"{', '.join(missed) if missed else 'none'}")
        print(f"      withheld unnecessarily                 : "
              f"{', '.join(false) if false else 'none'}")

    # The only fair comparison. The six screens in screens.py were used while writing
    # the rubric and choosing thresholds, so Decider has effectively seen them. The four
    # in hard_screens.py were written afterwards, to provoke failures, and no threshold
    # was changed after seeing them. Quote this subset, not the headline.
    print("\n  Held-out subset only (hard_screens.py -- no threshold was tuned on these):")
    print(f"  {'system':<42} {'held-out acc':>14}")
    print("  " + "-" * 58)
    for board in boards:
        held = [r for r in board.rows if r.screen in HELD_OUT]
        if not held:
            continue
        hits = sum(r.correct for r in held)
        wrong = [r.screen for r in held if not r.correct]
        detail = f"  (missed {', '.join(wrong)})" if wrong else ""
        print(f"  {board.name:<42} {hits}/{len(held)}{detail}")

    local = next((b for b in boards if "Decider" in b.name), None)
    clouds = [b for b in boards if "Decider" not in b.name]
    if local and clouds:
        print("\n  Read this honestly:")
        for cloud in clouds:
            faster = cloud.median_ms and local.median_ms / cloud.median_ms
            print(f"    vs {cloud.name}:")
            print(f"      accuracy {local.accuracy:.0%} local vs {cloud.accuracy:.0%} cloud")
            if faster and faster > 1:
                print(f"      the local model was {faster:.1f}x SLOWER per screen on this "
                      f"CPU-only laptop")
            print(f"      cost: $0.00 vs ${cloud.total_cost:.5f} for "
                  f"{len(cloud.rows)} screens")
        print("\n    The local model is not faster here -- this machine has no GPU, and"
              "\n    12s per call is the price. What it buys is $0.00 per image and, for"
              "\n    the four screens that should never be sent, the fact that they were"
              "\n    never sent. The cloud baseline had to receive every one of them to"
              "\n    reach its verdict.")

    payload = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ground_truth": TRUTH,
        "systems": [
            {
                "name": b.name,
                "accuracy": round(b.accuracy, 4),
                "median_ms": round(b.median_ms, 1),
                "total_cost_usd": round(b.total_cost, 6),
                "missed_holds": b.missed_holds(),
                "false_holds": b.false_holds(),
                "rows": [vars(r) for r in b.rows],
            }
            for b in boards
        ],
    }
    RESULTS.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\n  wrote {RESULTS}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Decider 2B vs Amazon Nova.")
    parser.add_argument("--skip-cloud", action="store_true")
    parser.add_argument("--no-ocr", dest="ocr", action="store_false")
    parser.set_defaults(ocr=True)
    args = parser.parse_args()

    missing = [n for n in TRUTH if not (SCREENS / f"{n}.png").exists()]
    if missing:
        print(f"missing screens: {missing}. Run: python evalset/screens.py")
        return 1

    boards: list[Scoreboard] = []
    print("=" * 78)
    print("  LOCAL: Strands Decider 2B through the RegionGate")
    print("=" * 78)
    boards.append(run_local(use_ocr=args.ocr))

    if not args.skip_cloud:
        for model_id in NOVA_MODELS:
            print(f"\n{'=' * 78}\n  CLOUD: {model_id}\n{'=' * 78}")
            board = run_cloud(model_id)
            if board.rows:
                boards.append(board)

    report(boards)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
