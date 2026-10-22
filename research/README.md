# What we measured

Every number here came off one machine: an Intel i5-1235U, 16 GB RAM, **no GPU**, running
`strands-decider-2B-qwen3.5-v1-2610` with `--vision --device cpu`. Treat the latencies as
a floor-of-the-floor: AWS report ~115 ms on an RTX 3090 and the community's hosted CPU
deployment reports ~681 ms on a 4-vCPU `c7i.xlarge`. The *ratios* are what travel.

The scripts are here and re-runnable. Start a server first:

```
strands-decider serve StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 \
    --vision --device cpu --port 8099
```

| Script | Question it answers |
|---|---|
| `spike_vision.py` | Can it read a screenshot at all, and what survives downscaling? |
| `spike_multi_image.py` | Do batched images answer per-image or jointly? |
| `model_probe.py` | Which Bedrock models are usable on this account? |

---

## 1. It reads screenshots well, at the whole-frame level

Five drawn 1920x1080 screens, asked `screen_kind` as a 5-option choice:

| Screen | Answer | Confidence |
|---|---|---|
| IDE with `.env` open | `code_editor` | 0.96 |
| Online banking | `banking` | 0.98 |
| Private chat | `private_chat` | 0.99 |
| Terminal with a stack trace | `terminal` | 0.40 |
| API documentation | `documentation` | 0.64 |

**5/5 correct.** Narrow yes/no questions separated just as cleanly:

| Question | Fires on | Everywhere else |
|---|---|---|
| `shows_money` | bank 0.817 | ≤ 0.073 |
| `private_conversation` | dm 0.862 | ≤ 0.042 |

Note the confidence pattern: it is most certain about exactly the screens that matter
most (banking 0.98, private chat 0.99) and least certain on the two benign ones.

## 2. Do not ask it for the verdict

The same run asked one broad, subjective question — *"how sensitive is this screen?"* as a
3-level score. It returned mush:

| Screen | Score | Confidence |
|---|---|---|
| IDE with credentials | 1.27 | 0.36 |
| Banking | 1.22 | 0.34 |
| Private chat | 0.90 | 0.36 |
| Terminal | 0.74 | 0.33 |
| Docs | 0.75 | 0.29 |

Everything within 0.5 of everything else, confidence never above 0.36. That one question
caused the run's only misclassification.

Recomposing the verdict in code from the narrow answers instead: **5/5**.

**The calibration was honest about it.** Confidence collapsed to ~0.3 on the question that
was badly posed. The model told us the question was bad; we just had to read the number.

> Ask for facts, not verdicts. Keep each question about one concrete, visible thing, and
> compose the decision in your own code.

## 3. Fine text needs a crop

Images are scaled so the longest side is at most 448 px, so a 1920x1080 frame arrives at
448x252. Asked whether a credential was visible:

| Input | `credential_visible` |
|---|---|
| Full 1920x1080 frame | 0.643 — weak |
| Cropped 1000x360 region containing the key | **0.884** — correct |
| Control crop: code, no secret | **0.186** — correct |

Cropping is not an optimisation, it is the difference between a usable signal and a
coin flip. Single-tile reads in a later run were cleaner still:

| Region | `credential_visible` |
|---|---|
| Region with a fake AWS key | 0.860 |
| Clean code sample | 0.196 |
| Clean prose | 0.071 |
| Clean terminal output | 0.075 |

## 4. Batched images answer *jointly* — so word the question that way

A request takes up to 4 images. They are concatenated into one state, so a question is
answered once over all of them, not once per image. That means the phrasing has to admit
there are several:

| Question wording | Batch with a key | Batch all clean | Separation |
|---|---|---|---|
| "Is a credential visible?" | 0.451 | 0.110 | 0.341 |
| "Does **ANY of these** contain a credential?" | **0.548** | **0.061** | **0.488** |

Rewording nearly doubles the separation — and the naive wording leaves the positive case
at 0.451, *below* a 0.5 threshold, where it would be missed entirely.

This is what makes coarse-to-fine screening viable: batch four regions, ask "any of
these?", and only split a batch that comes back hot.

## 5. Asking many things at once is much cheaper

| | Latency |
|---|---|
| 1 question about 1 image | 9.5 s |
| 4 questions about 1 image | 18.1 s |
| → marginal cost per extra question | **2.8 s** (vs 9.5 s for a fresh request) |
| 4 images in 1 request | 12.8 s |
| the same 4 images in 4 requests | 30.8 s |
| → batching images | **2.4× faster** |

The state and the images are encoded once and shared across questions. Ask everything you
need in one call.

## 6. The trap the docs warn about, and why the gate guards it

From `docs/vision.md` upstream:

> Its confidence does not tell you that an image is missing: check that the image is
> there before you ask.

With no image, or a blank one, the model does **not** become uncertain — it answers
confidently at chance. Any gate that trusts those numbers is reading noise with a
confident face on it. `aperture.decider.looks_blank()` exists for this, and the gate
calls it before it asks anything.

## 7. Which Bedrock models were actually usable

Measured on this account (414691912352, us-east-1) via `model_probe.py`:

| Model | Text | Tools | Vision | Said about the IDE screenshot |
|---|---|---|---|---|
| `us.amazon.nova-2-lite-v1:0` | ok | ok | ok, 1473 ms | **"a code editor screen, specifically showing a fil…"** |
| `us.amazon.nova-pro-v1:0` | ok | ok | ok, 1352 ms | "a terminal screen displaying the output of a command" |
| `us.amazon.nova-lite-v1:0` | ok | ok | ok, 1590 ms | "terminal" |
| `us.amazon.nova-micro-v1:0` | ok | ok | — text only | — |
| `us.amazon.nova-premier-v1:0` | not available | | | |
| `us.anthropic.claude-*` | `INVALID_PAYMENT_INSTRUMENT` | | | |

Anthropic models are provisioned through AWS Marketplace and were refused on this
account, so Amazon Nova 2 Lite drives the agent.

Worth noting on its own: the screen is an editor with `production.env` open. Nova 2 Lite
said "code editor". Nova Lite and Nova Pro both said "terminal" — wrong. Decider, running
locally and free, said `code_editor` at **0.96**, agreeing with the better cloud model and
beating two larger ones on the screening question.

---

## What all of this implies for the design

1. Whole-frame questions for context — proven at 0.96–0.99, one request.
2. Deterministic code for structured secrets, as an accelerator rather than a safety net.
   See section 8: the expectation that a regex would beat the model at finding
   `AKIA[0-9A-Z]{16}` did not survive contact with two real screenshots.
3. Coarse-to-fine tiles with "any of these?" wording, only when needed.
4. A blank-image check before trusting any answer.
5. The verdict composed in code, never asked of the model.

---

## 8. The prediction that failed: regex did not beat the model

Everything above was measured on screens we drew ourselves. This section was measured on
two **real** screenshots taken by the author on their own machine, with live cloud access
keys visible. The images are not in this repository and were never saved; only the
verdicts below were recorded.

The design assumed a clear division of labour:

> Code finds things with a known shape — `AKIA[0-9A-Z]{16}`, `sk_live_`, JWTs, DSNs.
> Decider answers the judgements that have no shape.

An AWS access key id is about as fixed a format as exists, so the deterministic lane
should have won it outright: exact, instant, and it returns pixel coordinates.

It didn't.

| | Capture 1, 1221x347 | Capture 2, 822x175 |
|---|---|---|
| OCR + 13 regex patterns | **nothing found** (4.8 s) | **nothing found** (4.9 s) |
| Strands Decider 2B | **credential 0.884** on region 0 | **credential 0.900** region 0, **0.568** region 1 |
| Result | redacted 28% of the frame | redacted 56% of the frame |
| Local decisions / wall | 4 / 49 s | 6 / 63 s |

Twice, on real data, the 2B decision model caught credentials the hand-written patterns
missed entirely.

**Cause: not yet established.** Two candidates, and we have not separated them:

1. `rapidocr-onnxruntime` is misreading characters at that font size, so the text the
   regexes see is not the text on screen.
2. The keys are in a format none of the 13 patterns cover.

Diagnosing it properly means inspecting what the OCR actually read, which can be done
without handling the secret itself by reporting character classes and token lengths rather
than content. That work is not done.

**What we changed as a result.** Nothing in the code -- the fast lane stays, because when
it *does* fire it localises to exact pixels in under a second and cut one run from 6 local
decisions to 3. What changed is the claim. The roles were stated backwards in the first
draft of every document here:

> code is the **optimisation**, Decider is the **safety net**

not the other way round. A deterministic lane you cannot measure is an assumption, and
this one was wrong on the first real data it met.

---

## 9. Two rules that shipped with no evidence behind them

Asked directly whether every rule had been tested, the answer was no. Of the five rules
the README advertised, two had **never been run against a single image**:

| Rule | Coverage before | Coverage now |
|---|---|---|
| `credential` | ide_env, deploy_failure, and two real screenshots | 0.824 - 0.926 |
| `private_conversation` | dm.png | 0.916 |
| `financial_records` | bank.png | 0.837 |
| `identity_document` | **none** | see below |
| `personal_contact` | **none** | see below |

Writing a rule and advertising it without testing it is worse than omitting it, because a
reader trusts the list. Two screens were drawn to close the gap.

### `identity_document` -- id_card.png

A driving-licence layout: portrait, surname, given names, date of birth, document number.

```
identity_document    0.767   -> WITHHOLD
private_conversation 0.048
financial_records    0.078
verdict: 0 of 4 regions sent, 100% kept local
```

**It fired, and that was not the expected result.** The rule asks about "a photograph of a
person's face", and the portrait here is PIL ellipses -- a cartoon, not a photograph. The
prediction was that this test would be inconclusive. Instead it answered 0.767, which
suggests the signal is coming from the *document layout* as much as the face. A real
photograph has not been tested, so "works on photographs" remains unproven.

### `personal_contact` -- contacts.png

A CRM list: seven people, with email addresses, international phone numbers and home
addresses.

```
personal_contact  tile 0  0.835
                  tile 1  0.697
                  tile 2  0.792
                  tile 3  0.726
verdict: REDACT, all four regions flagged, 100% kept local
```

Fired on every region, which is correct for this screen -- contact details are spread
across all of it. Note the threshold is 0.60 and the weakest region scored 0.697, so the
margin is thinner here than for `credential` (0.824-0.926). A screen with a single email
address in one corner would be a better test of the lower bound, and has not been run.

### What is still not covered

- A **real photograph** of a face, as opposed to a drawing.
- A screen with **one** contact detail rather than twenty-eight, to find where the rule
  stops firing.
- Credit card numbers, national ID numbers, and medical records: no rule exists for any of
  them. They are listed as gaps in the README rather than implied to work.
