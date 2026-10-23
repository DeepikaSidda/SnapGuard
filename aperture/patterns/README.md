# RegionGate — a reusable Decider 2B pattern for images

**Pattern: `#context-memory`** — score which pieces of visual context are relevant, and
permitted, before injecting them into a model's context.

Two files, no dependency on the rest of this project:

| File | What it is |
|---|---|
| `region_gate.py` | The decision. Scores the regions of an image against a written rubric and composes a verdict. |
| `intervention.py` | The wiring. Puts that decision in a Strands agent's path as an `InterventionHandler`. |

Copy them both, or `pip install aperture-gate` and import them.

---

## The problem it solves

Your agent is about to send an image to a model. A screenshot, a scan, a camera frame, a
rendered page. Two questions are worth asking first, and an LLM is the wrong thing to ask:

- **Is all of it safe to send?** Part of a screenshot might be a credentials file.
- **Is all of it worth sending?** Most of a 1920×1080 frame is irrelevant to the question.

Both are cheap, repeated, visual judgements with a closed answer set. That is exactly what
a decision model is for, and it runs on `127.0.0.1`, which matters when the thing you are
deciding is whether something may leave the machine.

---

## Thirty-second version

```python
from aperture.decider import Decider, noul
from aperture.patterns import RegionGate, Rule, Verdict

RULES = [
    Rule(
        name="credential",
        question=noul(
            "Is an access key, password or token visible as text?",
            true="a credential can be read in the image",
            false="no credential is visible",
        ),
        any_question=noul(                      # same question, for a batch of regions
            "Across all the regions shown, does ANY of them contain an access key, "
            "password or token visible as text?",
            true="at least one region shows a credential",
            false="no region shows a credential",
        ),
        threshold=0.50,                          # yours, not the model's
        on_match=Verdict.REDACT,
    ),
]

gate = RegionGate(Decider(), rules=RULES)
result = gate.inspect(screenshot)

if result.verdict is Verdict.WITHHOLD:
    ...                                          # send nothing
elif result.verdict is Verdict.REDACT:
    send(redact(screenshot, result.boxes()))     # send it with those boxes painted out
else:
    send(screenshot)
```

You need a vision-enabled server:

```bash
pip install "strands-decider[vision]"
strands-decider serve StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 \
    --vision --device cpu --port 8099
```

---

## Two axes, not one

Pass `asking_about` and every region is scored for **relevance** as well as safety:

```python
result = gate.inspect(screenshot, asking_about="why is my deploy failing?")

result.cleared      # safe to send
result.relevant     # bears on the question
result.injectable   # the intersection -- what you actually send
```

`injectable` is the `#context-memory` decision in one property: safe **and** relevant
**and** readable. On the demo screen, safety alone withheld 1 of 4 regions; adding
relevance withheld 2 of 4, because an empty editor pane is perfectly safe and perfectly
useless.

It costs more: relevance must be known for every region, so the coarse screen is skipped
and each region gets its own request. Worth it when the alternative is sending four
regions where two would do. Omit `asking_about` and you get safety-only behaviour at the
cheaper call count.

Relevance is supplied as a *factory* rather than a `Rule`, because the question has to
quote what the user actually said and that isn't known until inspection time:

```python
gate = RegionGate(
    decider,
    rules=SENSITIVITY_RULES,
    relevance=relevance_rule,   # callable: str -> Rule
    legibility=LEGIBLE,         # asked per region, see point 1 below
)
```

If you report this to a user, keep the two reasons apart. "We withheld part of your
screen because it had a credential on it" is a warning they must act on. "We skipped a
region because it wasn't relevant" is housekeeping. Conflating them trains people to
ignore the warning.

## Four things this gets right that are easy to get wrong

Each one is a measurement, not an opinion. Numbers and scripts in
[`../../research/`](../../research/README.md).

### 1. It checks the image is actually there

Straight from the upstream docs:

> Its confidence does not tell you that an image is missing: check that the image is
> there before you ask.

Hand the model a blank or missing image and it does **not** become uncertain. It answers
confidently, at chance. A gate that trusts those numbers is reading noise wearing a
confident face.

Two layers, because they catch different things:

- **`looks_blank()`** measures pixel variance before anything is asked. Cheap, certain,
  and it costs **zero** decider calls. A blank frame returns `UNREADABLE` immediately.
- **the `legibility` rule** is asked per region, alongside the sensitivity questions, and
  catches the subtler case: a region that has pixels but nothing readable in them. Regions
  that fail it land in `result.illegible` and are excluded from `injectable`, so the gate
  never records a confident verdict about a smear.

### 2. Batched questions must say "any of these"

A request takes up to 4 images, and they are concatenated into **one** state — so a
question is answered once over all of them, not once per image. The upstream test pins
the layout:

```python
render_image_state("two pages", 2) == (
    "<state>\n<|vision_start|><|image_pad|><|vision_end|>\n"
    "<|vision_start|><|image_pad|><|vision_end|>\ntwo pages\n</state>\n")
```

Word the question accordingly:

| Wording | Batch with a key | Batch all clean | Separation |
|---|---|---|---|
| "Is a credential visible?" | 0.451 | 0.110 | 0.341 |
| "Does **ANY of these** contain a credential?" | **0.548** | **0.061** | **0.488** |

The rewording nearly doubles separation — and the naive version leaves the positive case
at 0.451, *below* a 0.5 threshold, where you would miss it entirely.

That is what `Rule.any_question` is for, and it is what makes the coarse pass safe.

### 3. Narrow questions only. Never ask for the verdict.

Measured on five labelled screens:

| Question style | Result |
|---|---|
| Narrow: "is a private conversation visible?" | 0.862 on the chat, ≤0.042 elsewhere |
| Narrow: "are account balances visible?" | 0.817 on the bank page, ≤0.073 elsewhere |
| Broad: "how sensitive is this screen?" (score) | everything 0.74–1.27, confidence 0.29–0.36 |

The broad question caused the run's only misclassification. Recomposing the same answers
in code: **5/5**.

Note what the calibration did: confidence collapsed to ~0.3 on the badly posed question.
**The model told us the question was bad.** That is the signal no LLM API gives you.

So `RegionGate` never asks for an overall judgement. Rules return facts; `Policy` turns
facts into a verdict in ordinary Python you can read and test.

### 4. Crop before you ask about small things

Images are scaled so the longest side is ≤448px. A 1920×1080 frame arrives at 448×252,
and small text is gone:

| Input | `credential_visible` |
|---|---|
| Full 1920×1080 frame | 0.643 — barely above chance |
| Cropped region containing the key | **0.884** |
| Control crop: code, no secret | **0.186** |

Hence the grid. A 2×2 grid of a 1080p screen gives regions that downscale 2.1× instead of
4.3×. The default is **6×4**, which gives 320×270 regions — already inside 448px, so
**nothing is downscaled at all.** Past roughly 3×3 the grid stops being a cost/accuracy
trade and becomes free accuracy.

6 columns by 4 rows rather than a square grid because screens are 16:9, and the 448px cap
applies to the long side: square-ish regions spend the budget on detail, letterbox strips
spend it on width.

The other reason to go fine is the redaction. A region is the unit that gets blacked out,
so one hit at 2×2 covers a quarter of the display and at 6×4 covers a twenty-fourth.
A user reported the former as a bug three times before it was fixed properly —
[`research/README.md` §13](../../research/README.md).

---

## How `inspect()` spends its calls

```
1. blank check            0 calls      pixel variance, instant
2. context pass           1 call       whole-frame rules, all batched together
3. coarse pass            ceil(n/4)    4 regions at a time, "any of these?"
4. fine pass              1 per hot    only regions a coarse batch flagged
```

Step 3 is what makes a fine grid affordable. At the 6×4 default a clean screen costs
**7 calls** for 24 regions — 1 context plus 6 coarse batches — not 25, because the coarse
pass asks about four regions at once and only a batch that comes back hot is split. A
screen with one problem in it costs 11. Blank regions are dropped for free by step 1.

There is a fifth, optional step: `refine_flagged` subdivides a region that fired and asks
again, for a tighter box. It is **off by default**. At a coarse grid it was worth a lot
(56% of a frame covered down to 24%); at a fine grid it buys three points of coverage for
2.3× the requests, and its cost scales with how much fired, so a screen that is sensitive
everywhere pays four extra requests per flagged region. Make the regions small first.

With `asking_about`, step 3 is skipped — relevance cannot be asked as a batched "any of
these?" — and every non-blank region gets a fine pass. That is the expensive mode, and it
is the agent path, which is why the agent uses a coarse 2×2 grid while the clipboard guard
uses 6×4. Each fine call carries sensitivity, relevance and legibility together: the
marginal cost of an extra question on an image already in the request is 2.8 s against
9.5 s for a fresh one, so bundling is most of why this is affordable at all.

Measured on an i5-1235U with no GPU: ~12s per call, so a clean 6×4 screen is ~80s. On a
4-vCPU `c7i.xlarge` the community reports ~681ms per call, and AWS report ~115ms on an
RTX 3090 — which would put the same screen at under ten seconds and under three
respectively. The *ratios* are what travel; the absolute numbers here are a worst case,
and were measured on a machine with ~1 GB of RAM to spare.

---

## Putting it in a Strands agent

`intervention.py` maps the four-value verdict onto Strands' typed actions. Most gates only
use `Proceed` and `Deny`, which makes them a bouncer. The interesting one is `Transform`:

| Verdict | Action | What the agent experiences |
|---|---|---|
| `PASS` | `Proceed` | the call runs unchanged |
| `REDACT` | **`Transform`** | the call runs, **narrowed** to the cleared regions |
| `WITHHOLD` | `Deny` | blocked, with the reason shown to the model |
| `UNREADABLE` | `Guide` or `Confirm` | hands the turn back, or asks a human |

`Transform` mutates the tool call in place before it executes. So an agent that asked to
read nine regions reads the two that were clean and relevant, and never learns the other
seven existed. Better than allowing everything, and better than blocking everything.

```python
from strands import Agent
from strands.models import BedrockModel
from aperture.patterns.intervention import ScreenBuffer, ScreenGate

# Coarse on purpose: these regions are the crops that get sent to the cloud model, so
# they have to stay big enough for it to read. The guard's grid is 6x4; this is not.
buffer = ScreenBuffer(columns=2, rows=2)
agent = Agent(
    model=BedrockModel(model_id="us.amazon.nova-2-lite-v1:0"),
    tools=[make_look_at_screen(buffer), make_read_screen_region(buffer)],
    interventions=[ScreenGate(gate, buffer, guarded_tools=["read_screen_region"])],
)
```

Two notes from building this:

**Capture and inspection share a `ScreenBuffer`.** The handler cannot pull the image out
of a tool call, so the capture tool writes the frame into a buffer and the handler reads
it back. Pass the same instance to both.

**A `Transform`ed tool cannot tell what was removed from its own arguments.** By the time
the tool runs, the unsafe regions are already gone from the list — ask the arguments and
the answer is always "nothing was withheld". Read it from `result.flagged` instead. This
cost me a debugging cycle; the agent cheerfully answered as though it had seen the whole
screen.

---

## Adapting it

Change the rubric, not the gate. Define your own `Rule` list:

- **`threshold`** is policy, and it is yours. Bias it toward the cheaper mistake. A gate
  protecting credentials should fire early; one filtering noise should not.
- **`on_match`** — use `REDACT` when painting out a corner leaves something useful, and
  `WITHHOLD` when the whole frame is the problem. A bank statement has no clean half.
- **`true`/`false` descriptions** are not decoration. A noul renders as a two-option list,
  so those strings carry much of the signal. Write them as carefully as the question.
- **`hint_boxes`** lets a cheaper exact detector go first. We run OCR plus regex for
  secrets with known formats (`AKIA[0-9A-Z]{16}` and friends), and regions overlapping a
  hint skip the coarse screen, which cut one real run from 6 local decisions to 3.

  Worth knowing before you rely on it: we expected the regex lane to beat the model at
  finding structured secrets, and on two real screenshots with live keys on screen it
  found **nothing** while Decider scored 0.884 and 0.900. Treat a deterministic lane as an
  accelerator that localises precisely *when it fires*, not as the thing you depend on.

Other uses the same shape fits: deciding which frames of a session recording are worth a
multimodal call; gating camera frames before they leave a device; choosing which pages of
a scanned document to pay a document model for.

---

## What this is not

A security control. A ~0.87-accuracy model with **no image training at all** — the vision
tower is inherited from Qwen3.5-2B-Base and frozen — is a speed bump and a second pair of
eyes. It will miss things.

Bias your thresholds toward withholding, keep the deterministic lane for anything with a
known shape, and do not tell your users they are safe.

Apache-2.0, same as this repository.
