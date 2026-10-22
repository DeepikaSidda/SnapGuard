# Demo video script — 2:45

Hard limit three minutes. This runs about 2:45, which leaves slack for talking slower
than you expect to.

**The single most important decision in this video:** open with your own real leak, not
with my drawn fixture. You have footage nobody else in this hackathon has — your actual
access keys, on your actual machine, being blacked out by a model running on your own
CPU. That is worth more than every benchmark number combined.

---

## Before you record

```bash
# 1. Start the server FIRST. It needs ~2 minutes to load 6.8 GB on CPU.
strands-decider serve StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 \
    --vision --device cpu --port 8099

# 2. Confirm everything is green, on the terminal you will film
aperture doctor

# 3. Pre-generate the figures so you are not waiting on screen
aperture ask "my deploy is failing, what's wrong?" \
    --screen evalset/out/deploy_failure.png \
    --explain evalset/out/_explained_agent.png

# 4. ONE throwaway run. The first call after boot is roughly twice as slow as the
#    rest, and you do not want that on tape.
aperture check evalset/out/docs.png
```

**Zoom your terminal font.** The confidence numbers are the point of this video and
they must be readable after compression. 16pt minimum, ideally 20.

**The pacing problem, and how to be honest about it.** The gate takes 40–90 seconds on a
GPU-less laptop. Do not sit in silence and do not fake it. Either:

- **Cut, with a caption on the cut:** *"gate running — 50s on a laptop with no GPU, ~115ms on a GPU"*
- **Talk over it.** The log prints line by line, so narrate the numbers as they appear.

Pretending it is instant is the one thing that will cost you. One of the judges co-wrote
this model.

---

## 0:00–0:25 · Your own leak

**Show:** your real screenshot with the access keys. **Blur the keys for the recording.**

> "Four messages into designing this project I pasted a screenshot into a chat window to
> show a quota number. My API key was three lines above it. The page in the same
> screenshot said, in plain text, don't post this in chats.
>
> I wasn't being careless. I was looking at the quota. The key came along for the ride."

## 0:25–0:50 · It catches it, on your machine

**Show:** `aperture guard` armed, then take the same screenshot again with `Win+Shift+S`.

**Narrate the log as it appears:**

> "The clipboard is taken away immediately — I can't paste while it's thinking. The regex
> pass finds nothing. Then Strands Decider 2B, running on my own CPU: credential, 0.88."

**Then paste into any chat box.** The keys are already black.

> "Fifty seconds. Four local decisions. Zero cost. And the keys never left this laptop."

## 0:50–1:15 · Why it has to be local

> "The obvious question is why use a 2B local model instead of just asking an LLM.
>
> Because to ask a cloud model *is this screenshot too sensitive to send to the cloud*,
> you have to send it the screenshot. You've already done the thing you were preventing.
>
> It's like phoning someone to ask whether you should have phoned them. The checker has
> to run on my machine — not because it's cheaper, but because anything else is
> incoherent."

## 1:15–1:55 · The agent, and the picture

**Show:** `evalset/out/deploy_failure.png` for three seconds. Point at both halves.

> "Terminal error on the left. Production credentials on the right. I want help with the
> error. I do not want to send the keys."

**Run it** (or cut to the pre-generated figure):

```bash
aperture ask "my deploy is failing, what's wrong?" \
    --screen evalset/out/deploy_failure.png --explain fig.png
```

**Then show `_explained_agent.png` full screen.** Hold it. This is the money shot.

> "Here's what the local model decided, drawn back onto the screen it was looking at.
>
> Every region scored twice — is it safe, and is it relevant to what I asked. Region zero:
> relevant 0.94, credential 0.09. Sent. Region one: credential 0.82. Withheld, and its
> contents are gone. Region three: legible 0.09 — empty space, not worth sending.
>
> Two of four regions reached Amazon Bedrock. Half my screen stayed here. And it still
> diagnosed the bug — connection pool exhausted — and told me to rotate the key."

## 1:55–2:20 · The numbers

**Show:** the benchmark table.

> "Twelve labelled screens, against two Amazon Nova models on Bedrock.
>
> Decider got twelve out of twelve. Nova 2 Lite, ten. Nova Pro — the biggest — nine.
>
> And the failure that matters: both Nova models looked at a screen with seven people's
> email addresses, phone numbers and home addresses, and said it was safe to send.
> Decider flagged every region of it.
>
> I should be straight with you though. On this laptop, with no GPU, my gate is forty
> times *slower* than asking Bedrock. It is not faster. What it buys is zero cost per
> image, and the six screens that should never have been sent were never sent."

## 2:20–2:45 · What I got wrong

**Show:** `_bug_before_after.png`.

> "The most useful thing I learned came from breaking it.
>
> I built that annotated view to explain the redaction. The first version only *dimmed*
> the regions it withheld — and dim is still readable. So the picture explaining my
> secret-protection tool displayed the secrets. Left side. Had I run that on a real
> screenshot and published it, the explain mode would have been the leak.
>
> I measured the fix with OCR rather than trusting my eyes: 277 characters recoverable
> before, zero after.
>
> The reusable pattern is in the repo — RegionGate, two files, drop it into any agent
> that's about to send an image somewhere. Thanks for watching."

---

## Shot list

| # | Shot | Source |
|---|---|---|
| 1 | Your real leaked screenshot, keys blurred | your own recording |
| 2 | `aperture doctor`, all four green | terminal |
| 3 | `aperture guard` armed + `Win+Shift+S` + the log | terminal |
| 4 | Pasting into a chat box, keys already black | your screen |
| 5 | `deploy_failure.png` full screen | `evalset/out/` |
| 6 | **`_explained_agent.png`** — hold 8 seconds | `evalset/out/` |
| 7 | Benchmark table | README or `bench.py` output |
| 8 | **`_bug_before_after.png`** | `evalset/out/` |

Shots 6 and 8 are the ones people will remember. Give them room.

## Checklist

- [ ] Under 3:00
- [ ] **Publicly viewable** — not unlisted and forgotten
- [ ] Your real key blurred in shot 1
- [ ] No real credentials anywhere else in frame
- [ ] Confidence numbers legible after compression
- [ ] The slowness stated out loud, not hidden
- [ ] Both tags shown or said: `#strands-decider-2b`, `#context-memory`
- [ ] Says "Strands Decider 2B" and "Amazon Bedrock" by name at least once each
