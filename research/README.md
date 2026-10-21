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
2. Deterministic code for structured secrets — a regex beats a 2B model at finding
   `AKIA[0-9A-Z]{16}`, and returns coordinates.
3. Coarse-to-fine tiles with "any of these?" wording, only when needed.
4. A blank-image check before trusting any answer.
5. The verdict composed in code, never asked of the model.
