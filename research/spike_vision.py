"""Feasibility spike for Airlock: what can Decider 2B actually resolve in a screenshot?

Images are downscaled so the longer side is at most 448px, so a 1920x1080 screen loses
most of its fine detail. This measures the consequence, and it is the only question that
matters before we commit:

  A. Holistic judgement -- "what kind of screen is this" -- should survive downscaling,
     because it depends on layout and colour rather than glyphs.
  B. Fine-text reading -- "is there a credential visible" -- probably does not survive at
     full size, but may survive on a cropped region.

If A works, Airlock works as a hybrid: code reads text, Decider judges context.
If B also works on crops, the design gets stronger.

Server:
    strands-decider serve StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 \
        --vision --device cpu --port 8099
"""

from __future__ import annotations

import base64
import io
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

URL = "http://127.0.0.1:8099"
HERE = Path(__file__).parent

# Ground truth: should this screen be allowed to leave the machine?
SCREENS = {
    "ide_env": ("BLOCK", "editor with a .env file of production credentials"),
    "bank": ("BLOCK", "online banking dashboard with balances"),
    "dm": ("BLOCK", "private one-to-one conversation about health and resignation"),
    "terminal_error": ("ALLOW", "a failing test and a stack trace"),
    "docs": ("ALLOW", "public API documentation"),
}

# A. Holistic: context classification. No fine text needed.
HOLISTIC = {
    "screen_kind": {
        "type": "choice",
        "instructions": "What kind of screen is shown in this image?",
        "criteria": {
            "code_editor": "a source code editor or IDE with files and code",
            "terminal": "a command line terminal with console output",
            "banking": "a bank, payroll or financial account page showing money",
            "private_chat": "a personal one-to-one messaging conversation",
            "documentation": "public documentation, a reference manual or an article",
        },
    },
    "shows_money": {
        "type": "noul",
        "instructions": "Does this screen show someone's account balances or financial records?",
        "criteria": {
            "true": "monetary amounts belonging to a person or company are visible",
            "false": "no personal financial information is shown",
        },
    },
    "private_conversation": {
        "type": "noul",
        "instructions": "Does this screen show a private conversation between named people?",
        "criteria": {
            "true": "a personal message thread between individuals is visible",
            "false": "no private conversation is shown",
        },
    },
    "sensitive": {
        "type": "score",
        "instructions": (
            "A screen is sensitive when it shows secrets, credentials, personal data, "
            "private messages or financial records. How sensitive is this screen?"
        ),
        "criteria": ["safe to share publicly", "mildly private", "clearly confidential"],
    },
}

# B. Fine text: can it see a credential at all?
FINE = {
    "credential_visible": {
        "type": "noul",
        "instructions": "Is an access key, password, token or secret value visible as text?",
        "criteria": {
            "true": "a credential or secret value can be seen in the image",
            "false": "no credential or secret value is visible",
        },
    },
}


def ask(state: str, questions: dict, image_bytes: bytes) -> dict:
    body = {
        "state": state,
        "questions": questions,
        "images": [base64.b64encode(image_bytes).decode()],
    }
    req = urllib.request.Request(
        f"{URL}/v1/systemone",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=900) as r:
        payload = json.loads(r.read())
    payload["_wall_ms"] = (time.perf_counter() - started) * 1000
    return payload


def as_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main() -> int:
    with urllib.request.urlopen(f"{URL}/health", timeout=15) as r:
        health = json.loads(r.read())
    print("health:", json.dumps(health))
    if not health.get("vision"):
        print("FAIL: server not started with --vision")
        return 1

    print("\n" + "=" * 78)
    print("A. HOLISTIC JUDGEMENT at full 1920x1080 (server downscales to 448px)")
    print("=" * 78)

    correct = 0
    for name, (truth, description) in SCREENS.items():
        path = HERE / f"{name}.png"
        payload = ask(
            "A screenshot of an application window on a developer's computer.",
            HOLISTIC,
            path.read_bytes(),
        )
        a = payload["answers"]
        kind = a["screen_kind"]["choice"]
        kind_conf = a["screen_kind"]["confidence"]
        sens = a["sensitive"]["score"]
        verdict = "BLOCK" if sens >= 1.0 else "ALLOW"
        hit = "OK " if verdict == truth else "MISS"
        correct += verdict == truth

        print(f"\n[{hit}] {name}  (truth {truth}: {description})")
        print(f"       screen_kind          {kind} ({kind_conf:.2f})")
        print(f"       shows_money          {a['shows_money']['noul']:.3f}")
        print(f"       private_conversation {a['private_conversation']['noul']:.3f}")
        print(f"       sensitive            {sens:.2f} (conf {a['sensitive']['confidence']:.2f})")
        print(f"       -> {verdict};  {payload['usage']['input_tokens']} tokens, "
              f"{payload['latency_ms']:.0f} ms server, {payload['_wall_ms']:.0f} ms wall")

    print(f"\nholistic verdict accuracy: {correct}/{len(SCREENS)}")

    print("\n" + "=" * 78)
    print("B. FINE TEXT: credential visible, full frame vs cropped region")
    print("=" * 78)

    full = Image.open(HERE / "ide_env.png")
    # The credential block occupies roughly this region of the editor.
    crop = full.crop((320, 100, 1320, 460))
    safe_crop = Image.open(HERE / "docs.png").crop((400, 420, 1400, 660))

    for label, img in (
        ("ide_env FULL 1920x1080", full),
        ("ide_env CROP 1000x360 (credential block)", crop),
        ("docs CROP 1000x240 (code sample, no secret)", safe_crop),
    ):
        payload = ask(
            "A region of a screenshot from a developer's computer.", FINE, as_png(img)
        )
        noul = payload["answers"]["credential_visible"]["noul"]
        print(f"\n  {label}")
        print(f"      credential_visible = {noul:.3f}   "
              f"({payload['latency_ms']:.0f} ms, {payload['usage']['input_tokens']} tokens)")

    print("\n" + "=" * 78)
    print("C. COST OF ASKING MANY QUESTIONS ABOUT ONE IMAGE")
    print("=" * 78)
    img = (HERE / "ide_env.png").read_bytes()
    one = ask("A screenshot.", {"a": HOLISTIC["shows_money"]}, img)
    four = ask("A screenshot.", HOLISTIC, img)
    print(f"  1 question : {one['latency_ms']:.0f} ms")
    print(f"  4 questions: {four['latency_ms']:.0f} ms")
    print(f"  marginal cost per extra question: "
          f"{(four['latency_ms'] - one['latency_ms']) / 3:.0f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
