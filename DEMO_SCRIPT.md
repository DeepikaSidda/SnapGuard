# Demo video script — 2:40

Hard limit is three minutes. This runs to about 2:40, which leaves room for you to talk
slower than you think you will.

## Before you record

```bash
# 1. Start the server and let it finish loading (~2 min on CPU). Do this first.
strands-decider serve StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 \
    --vision --device cpu --port 8099

# 2. Generate the screens
python evalset/screens.py
python evalset/hard_screens.py

# 3. Confirm everything is green ON CAMERA-READY TERMINAL
aperture doctor

# 4. Do one throwaway run first. The first decider call after boot is ~2x slower
#    than the rest, and you do not want that on tape.
aperture ask "test" --screen evalset/out/docs.png
```

**The pacing problem, and how to handle it.** The gate takes ~70 seconds on a CPU-only
laptop. Do not sit in silence and do not fake it. Two options:

- **Cut the wait**, and put a caption on the cut: *"gate running — 70 s on a laptop with
  no GPU, ~115 ms on a GPU."* Honest and keeps the pace.
- **Talk over it.** The log prints as it goes, so narrate the numbers appearing.

Either is fine. Pretending it's instant is not — one of the judges co-wrote the model.

---

## 0:00–0:20 · The hook (use your own screen, this really happened)

**Show:** the screenshot you pasted into chat, with the API key visible above the quota
number. Blur the key for the video.

> "Four messages into designing this project, I pasted this screenshot into a chat to show
> a quota number. My API key is three lines above it. The page in the same screenshot says
> 'don't post this in chats.'
>
> I wasn't being careless. I was looking at the quota. That's the problem — nobody leaks a
> secret on purpose. You screenshot one thing and a second thing is in the frame."

## 0:20–0:40 · The argument

**Show:** a simple diagram, or just talk to camera.

> "So I built a gate. And the obvious question is why use a 2B local model instead of just
> asking an LLM.
>
> Because to ask a cloud model 'is this screenshot too sensitive to send to the cloud',
> you have to send it the screenshot. You've already done the thing you were preventing.
>
> The checker has to run on my machine. Not because it's cheaper — because anything else
> is incoherent."

## 0:40–1:20 · The agent running

**Show:** `evalset/out/deploy_failure.png` full screen for three seconds. Point out the
terminal error on the left and `production.env` on the right.

> "Terminal error on the left. Production credentials on the right. I want help with the
> error. I do not want to send the keys."

**Run it:**

```bash
aperture ask "my deploy is failing, what's wrong?" --screen evalset/out/deploy_failure.png
```

**Narrate as the gate log appears:**

> "Decider is reading the frame on 127.0.0.1. Private conversation, 0.04. Financial
> records, 0.07. And then — credential, 0.82, on region 1. That's the env file."

*(cut or talk through the wait)*

## 1:20–1:50 · The payoff

**Show:** the final lines of output.

> "The agent asked for all four regions. It got three.
>
> It still diagnosed the error — PoolTimeout, connection pool exhausted. And it told me
> region one was withheld because it held a credential, and that I should rotate it.
>
> Twenty-five percent of my screen never left this machine."

**Point at the last line:**

```
narrowed  requested [0, 1, 2, 3] -> allowed [0, 2, 3], withheld [1]
```

> "That's Strands' `Transform` action. The gate didn't refuse the call, it narrowed it.
> The agent never learned region one existed."

## 1:50–2:20 · The numbers

**Show:** the benchmark summary table.

> "Ten labelled screens, against two Amazon Nova models on Bedrock.
>
> Decider got ten out of ten. Nova 2 Lite, nine. Nova Pro — the biggest model — eight.
>
> Both Nova models flagged a screen full of git SHAs as secrets. Nova Pro also flagged a
> public revenue dashboard. Generative models see a long hex string and cry secret.
>
> And I should be straight with you: on this laptop, with no GPU, my gate is forty times
> *slower* than asking Bedrock. It's not faster. What it buys is zero cost per image, and
> the four screens that should never have been sent were never sent."

## 2:20–2:40 · The lesson, and the pattern

**Show:** the two-row table from the article — broad question confidence 0.29–0.36 versus
narrow questions at 0.86 and 0.98.

> "The thing I actually learned: don't ask the model for the verdict.
>
> My first version asked 'how sensitive is this screen?' Everything came back within half a
> point, confidence 0.3. I switched to narrow yes/no questions and composed the verdict in
> my own code — five out of five.
>
> And the confidence told me the question was bad. That's the number you cannot get from a
> frontier API.
>
> The reusable pattern is in the repo — `RegionGate`, two files, drop it into any agent
> that's about to send an image somewhere. Thanks for watching."

---

## Shot list

| Shot | Source |
|---|---|
| Your real leaked screenshot, key blurred | your own screen recording |
| `aperture doctor` all green | terminal |
| `deploy_failure.png` full screen | `evalset/out/` |
| The gate log printing | terminal |
| The agent's answer with the withheld note | terminal |
| `narrowed requested [0,1,2,3] -> allowed [0,2,3]` | terminal |
| Benchmark summary table | `python evalset/bench.py` |
| Broad-vs-narrow confidence table | `research/README.md` |

## Checklist

- [ ] Under 3:00
- [ ] Publicly viewable, not unlisted-and-forgotten
- [ ] Decider's confidence numbers legible on screen (zoom the terminal font)
- [ ] The slowness is stated, not hidden
- [ ] Both tags mentioned or shown: `#strands-decider-2b`, `#context-memory`
- [ ] Your real key blurred in the opening shot
- [ ] No real credentials anywhere else in frame
