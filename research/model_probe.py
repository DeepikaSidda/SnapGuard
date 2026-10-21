"""Which models on this account actually work? Anthropic is refusing on billing.

Earlier a text-only call to Claude Haiku 4.5 succeeded, and now every Anthropic model
returns INVALID_PAYMENT_INSTRUMENT. Anthropic models are provisioned through AWS
Marketplace, so this is a billing/subscription problem rather than a model-access one.
Amazon's own Nova models are first-party and may be unaffected.

This maps the ground truth: for each candidate, can it take an image, and will it call
a tool?
"""

from __future__ import annotations

import time
from pathlib import Path

import boto3

HERE = Path(__file__).parent
REGION = "us-east-1"

CANDIDATES = [
    "us.amazon.nova-lite-v1:0",
    "us.amazon.nova-pro-v1:0",
    "us.amazon.nova-2-lite-v1:0",
    "global.amazon.nova-2-lite-v1:0",
    "us.amazon.nova-premier-v1:0",
    "us.amazon.nova-micro-v1:0",          # text-only, expected to refuse the image
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",  # re-check: did text break too?
]

TOOL_CONFIG = {
    "tools": [{
        "toolSpec": {
            "name": "read_screen_region",
            "description": "Read a numbered region of the user's screen.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {"tile_ids": {"type": "array", "items": {"type": "integer"}}},
                "required": ["tile_ids"],
            }},
        }
    }]
}


def short(exc: Exception) -> str:
    message = str(exc)
    for marker in ("INVALID_PAYMENT_INSTRUMENT", "AccessDenied", "ValidationException",
                   "don't have access", "not supported"):
        if marker in message:
            return marker
    return message[:70]


def probe(client, model_id: str, image: bytes) -> tuple[str, str, str]:
    """Returns (text, vision, tools) as OK / reason."""
    results = []

    # text
    try:
        client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "Reply with OK"}]}],
            inferenceConfig={"maxTokens": 10, "temperature": 0},
        )
        results.append("OK")
    except Exception as exc:  # noqa: BLE001
        results.append(short(exc))

    # vision
    try:
        started = time.perf_counter()
        r = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [
                {"text": "One short line: what kind of screen is this?"},
                {"image": {"format": "png", "source": {"bytes": image}}},
            ]}],
            inferenceConfig={"maxTokens": 60, "temperature": 0},
        )
        wall = (time.perf_counter() - started) * 1000
        said = r["output"]["message"]["content"][0]["text"].strip().replace("\n", " ")
        results.append(f"OK {wall:.0f}ms \"{said[:60]}\"")
    except Exception as exc:  # noqa: BLE001
        results.append(short(exc))

    # tools
    try:
        r = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "Read tiles 4 and 5 of my screen."}]}],
            toolConfig=TOOL_CONFIG,
            inferenceConfig={"maxTokens": 200, "temperature": 0},
        )
        uses = [b["toolUse"] for b in r["output"]["message"]["content"] if "toolUse" in b]
        results.append("OK" if uses else f"no call ({r.get('stopReason')})")
    except Exception as exc:  # noqa: BLE001
        results.append(short(exc))

    return tuple(results)  # type: ignore[return-value]


def main() -> int:
    client = boto3.client("bedrock-runtime", region_name=REGION)
    image = (HERE / "ide_env.png").read_bytes()
    print(f"{'model':<46} {'text':<8} {'tools':<10} vision")
    print("-" * 110)
    for model_id in CANDIDATES:
        text, vision, tools = probe(client, model_id, image)
        print(f"{model_id:<46} {text:<8} {tools:<10} {vision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
