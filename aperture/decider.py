"""A small client for a Strands Decider server, with images.

Deliberately `urllib` and nothing else, because this module is meant to be copied into
other people's projects. Taking a dependency should not be the price of reading it.

Two endpoints are supported, because the two that exist in the wild disagree:

  local   POST /v1/systemone   the server from `strands-decider serve`
  hosted  POST /v1/decide      the AWS User Group Madurai community API, Bearer auth

The local server is the default and the one everything here was measured on.

The hosted path is offered so a reader can try the pattern without a 4.5 GB download, but
two caveats. **It is untested**: it is written against the documented request shape and
was never exercised against the live service, because the only key available during
development had to be treated as compromised. And its free tier caps you at 4 questions
and 1 image per request, which defeats the point of asking many questions about one
image -- Aperture's own rubric exceeds both limits.

Use the local server. The hosted switch is a convenience for reading, not a supported
path.
"""

from __future__ import annotations

import base64
import io
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from PIL import Image

LOCAL_URL = "http://127.0.0.1:8099"
HOSTED_URL = "https://da25tnxy06gjk.cloudfront.net"

# The server scales every image so its longest side is at most this, and never upscales.
# A 448x448 image costs 196 tokens (patch 16, merge 2). Matching it client-side keeps the
# request small and makes the token cost predictable.
IMAGE_LONG_SIDE = 448

# Fixed by the server (VisionEngineConfig.max_images).
MAX_IMAGES_PER_REQUEST = 4

SERVE_HINT = (
    "strands-decider serve StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 "
    "--vision --device cpu --port 8099"
)


class DeciderUnavailable(RuntimeError):
    """Nothing is answering. Carries the command that starts a server."""


class VisionNotEnabled(RuntimeError):
    """A server is answering but was started without --vision, so it refuses images."""


# --------------------------------------------------------------------------- questions
#
# Three builders, one per question type. Written as free functions rather than methods
# because question sets are data: define them at module level, reuse them everywhere.
#
# A note earned the hard way, from research/README.md: keep questions NARROW. A yes/no
# question about one concrete, visible thing answers reliably. A broad question -- "how
# sensitive is this screen", "what changed between these images" -- comes back as mush,
# and the confidence honestly tells you so (0.12 to 0.52 in our runs). Ask for facts and
# compose the verdict in your own code.


def noul(instructions: str, true: str, false: str) -> dict[str, Any]:
    """Yes/no. The answer is P(true), which with two outcomes *is* the confidence.

    `true` and `false` describe what each outcome means. They are not decoration: a noul
    renders as a two-option list and those descriptions carry much of the signal.
    """
    return {
        "type": "noul",
        "instructions": instructions,
        "criteria": {"true": true, "false": false},
    }


def choice(instructions: str, options: Mapping[str, str]) -> dict[str, Any]:
    """Pick one of N. `options` maps each option name to what it means.

    Option names come from the request, not the weights, so you can change them freely.
    Keep the set small and the options visually distinct; long option lists were the
    least reliable thing we measured.
    """
    return {"type": "choice", "instructions": instructions, "criteria": dict(options)}


def score(instructions: str, levels: Sequence[str]) -> dict[str, Any]:
    """Rate against an ordered rubric, lowest level first.

    Measured caveat: scores over subjective judgements were unreliable for us. Prefer
    several narrow nouls and combine them yourself.
    """
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


# --------------------------------------------------------------------------- answers


@dataclass(frozen=True)
class Answer:
    """One answer, normalised across the two server flavours.

    `value` is the number you branch on: P(true) for a noul, the level for a score, and
    the winning option's probability for a choice. `confidence` is how much to trust it.
    For a noul the two coincide, which is why both are kept.
    """

    name: str
    kind: Literal["noul", "choice", "score"]
    value: float
    confidence: float
    label: str | None = None
    probabilities: Mapping[str, float] = field(default_factory=dict)

    def __str__(self) -> str:
        if self.kind == "noul":
            return f"{self.name}={self.value:.3f}"
        if self.kind == "choice":
            return f"{self.name}={self.label}({self.confidence:.2f})"
        return f"{self.name}={self.value:.2f}({self.confidence:.2f})"


@dataclass(frozen=True)
class Reply:
    answers: dict[str, Answer]
    input_tokens: int
    latency_ms: float
    model: str

    def __getitem__(self, name: str) -> Answer:
        return self.answers[name]

    def value(self, name: str) -> float:
        return self.answers[name].value

    def __str__(self) -> str:
        return "  ".join(str(a) for a in self.answers.values())


def _parse(name: str, raw: Mapping[str, Any]) -> Answer:
    kind = raw.get("type", "noul")
    if kind == "noul":
        p = float(raw["noul"])
        return Answer(name, "noul", p, p, "true" if p >= 0.5 else "false")
    if kind == "choice":
        return Answer(
            name,
            "choice",
            float(raw.get("confidence", 0.0)),
            float(raw.get("confidence", 0.0)),
            str(raw.get("choice")),
            dict(raw.get("probabilities") or {}),
        )
    return Answer(
        name,
        "score",
        float(raw.get("score", 0.0)),
        float(raw.get("confidence", 0.0)),
        None,
        dict(raw.get("probabilities") or {}),
    )


# --------------------------------------------------------------------------- images


def prepare_image(image: Image.Image | bytes | str) -> bytes:
    """Normalise to PNG bytes, downscaled to the server's window.

    Doing the resize here rather than letting the server do it means the bytes on the
    wire are small, which on a loopback connection with a 9 GB model is still worth it,
    and it makes the token cost of a request predictable before you send it.
    """
    if isinstance(image, (bytes, bytearray)):
        img = Image.open(io.BytesIO(bytes(image)))
    elif isinstance(image, str):
        img = Image.open(image)
    else:
        img = image

    img = img.convert("RGB")
    long_side = max(img.size)
    if long_side > IMAGE_LONG_SIDE:
        scale = IMAGE_LONG_SIDE / long_side
        img = img.resize(
            (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
            Image.LANCZOS,
        )
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def looks_blank(image: Image.Image | bytes | str, *, tolerance: float = 2.0) -> bool:
    """Is this image effectively featureless?

    This exists because of a documented trap. From the model's own docs: "Its confidence
    does not tell you that an image is missing: check that the image is there before you
    ask." With no image, or a blank one, the model does not become unsure -- it answers
    confidently at chance. Any gate that trusts those numbers is reading noise.

    So we check for ourselves, by the cheapest honest measure: the spread of pixel
    values. A solid colour has almost none.
    """
    if isinstance(image, (bytes, bytearray)):
        img = Image.open(io.BytesIO(bytes(image)))
    elif isinstance(image, str):
        img = Image.open(image)
    else:
        img = image
    grey = img.convert("L")
    lo, hi = grey.getextrema()
    if hi - lo < 8:
        return True
    # Standard deviation the slow-but-dependency-free way.
    histogram = grey.histogram()
    total = sum(histogram) or 1
    mean = sum(i * n for i, n in enumerate(histogram)) / total
    variance = sum(n * (i - mean) ** 2 for i, n in enumerate(histogram)) / total
    return variance**0.5 < tolerance


# --------------------------------------------------------------------------- the client


class Decider:
    """One state, one or more images, many questions, one round trip.

    The questions cannot see each other, so their answers are independent -- but the
    state and the images are encoded once and shared across all of them. That is the
    whole economic argument for this model: measured on an i5-1235U, one question about
    an image took 9.5s and four took 18.1s, so the fourth question cost 2.8s instead of
    another 9.5s. Ask everything you need in one call.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        api_key: str | None = None,
        timeout: float = 600.0,
    ) -> None:
        env_url = os.environ.get("APERTURE_DECIDER_URL") or os.environ.get(
            "STRANDS_DECIDER_URL"
        )
        self.url = (url or env_url or LOCAL_URL).rstrip("/")
        self.api_key = api_key or os.environ.get("APERTURE_DECIDER_KEY")
        # Hosted deployments use a different path and require auth.
        self.hosted = "/v1/decide" if self.api_key else None
        self.timeout = timeout

        # Running totals, for the benchmark and the status line.
        self.calls = 0
        self.questions_asked = 0
        self.input_tokens = 0
        self.total_latency_ms = 0.0

    # ---- plumbing ----------------------------------------------------------

    @property
    def _endpoint(self) -> str:
        return f"{self.url}{self.hosted or '/v1/systemone'}"

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _unavailable(self, exc: Exception) -> DeciderUnavailable:
        return DeciderUnavailable(
            f"no decider at {self.url} ({exc}).\nStart one with:\n    {SERVE_HINT}"
        )

    # ---- asking ------------------------------------------------------------

    def ask(
        self,
        state: str,
        questions: Mapping[str, dict[str, Any]],
        images: Sequence[Image.Image | bytes | str] = (),
    ) -> Reply:
        """Evaluate `state` plus `images` against `questions`.

        Answers come back keyed by the names you used. Raises rather than guessing if
        the server is absent, refuses images, or is handed more images than it takes.
        """
        if not questions:
            raise ValueError("at least one question is required")
        if len(images) > MAX_IMAGES_PER_REQUEST:
            raise ValueError(
                f"{len(images)} images, but a server takes at most "
                f"{MAX_IMAGES_PER_REQUEST} per request"
            )

        payload: dict[str, Any] = {"state": state, "questions": dict(questions)}
        if images:
            payload["images"] = [
                base64.b64encode(prepare_image(i)).decode() for i in images
            ]

        request = urllib.request.Request(
            self._endpoint, data=json.dumps(payload).encode(), headers=self._headers()
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            if exc.code == 422 and images:
                raise VisionNotEnabled(
                    "the server refused an image request (HTTP 422). It was almost "
                    "certainly started without --vision. Restart it with:\n"
                    f"    {SERVE_HINT}"
                ) from exc
            raise RuntimeError(f"decider returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise self._unavailable(exc) from exc

        wall_ms = (time.perf_counter() - started) * 1000
        answers = {
            name: _parse(name, raw) for name, raw in (body.get("answers") or {}).items()
        }

        self.calls += 1
        self.questions_asked += len(answers)
        self.input_tokens += int((body.get("usage") or {}).get("input_tokens", 0))
        reported = float(body.get("latency_ms") or wall_ms)
        self.total_latency_ms += reported

        return Reply(
            answers=answers,
            input_tokens=int((body.get("usage") or {}).get("input_tokens", 0)),
            latency_ms=reported,
            model=str(body.get("model", "unknown")),
        )

    # ---- health ------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=15) as response:
                return json.loads(response.read())
        except urllib.error.URLError as exc:
            raise self._unavailable(exc) from exc

    def require_vision(self) -> dict[str, Any]:
        """Fail now, with a useful message, rather than on the first image."""
        if self.hosted:
            return {"vision": True, "checkpoint": "hosted", "device": "remote"}
        info = self.health()
        if not info.get("vision"):
            raise VisionNotEnabled(
                f"the server at {self.url} is running without --vision, so it cannot "
                f"read images. Restart it with:\n    {SERVE_HINT}"
            )
        return info

    def banner(self) -> str:
        info = self.require_vision()
        return (
            f"decider: {info.get('checkpoint', '?')} "
            f"[{info.get('device', '?')}, vision] at {self.url}"
        )

    @property
    def mean_latency_ms(self) -> float:
        return self.total_latency_ms / self.calls if self.calls else 0.0

    def __str__(self) -> str:
        return (
            f"{self.calls} calls, {self.questions_asked} questions, "
            f"{self.input_tokens} input tokens, {self.mean_latency_ms:.0f} ms mean"
        )
