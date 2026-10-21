"""The agent's tools.

Only one of these sends pixels anywhere, and that one is the tool the gate guards.

The split is deliberate. `look_at_screen` captures and describes the *geometry* of the
screen -- how many regions there are and where they sit -- without showing the model any
content. The model then asks for regions by number, and `read_screen_region` is where
pixels would actually leave. Putting the gate on the second call means the agent can
reason about what to look at before anything is exposed, and the gate gets to narrow the
request rather than veto the whole idea.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Sequence

import boto3
from PIL import Image
from strands import tool

from ..patterns.intervention import ScreenBuffer

# Nova 2 Lite reads the approved regions. Chosen on measurement rather than taste: on our
# eval screens it was the only Nova model that correctly called an editor an editor,
# where Nova Lite and Nova Pro both said "terminal" (research/README.md).
READER_MODEL = "us.amazon.nova-2-lite-v1:0"
REGION = "us-east-1"


def _png(image: Image.Image, long_side: int = 1100) -> bytes:
    """Encode a region for the cloud reader.

    Larger than the 448 px the decider gets, because the reader's job is to read small
    text and it is not subject to the same window. Still bounded, because an unbounded
    screenshot is an unbounded bill.
    """
    img = image.convert("RGB")
    if max(img.size) > long_side:
        scale = long_side / max(img.size)
        img = img.resize(
            (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
            Image.LANCZOS,
        )
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def make_look_at_screen(buffer: ScreenBuffer, *, source: Path | None = None):
    """Build the capture tool.

    `source` pins the capture to an image file, which is what the demo and the tests
    use: a reproducible screen beats a live one when you need the same result twice.
    Without it, the live screen is grabbed.
    """

    @tool
    def look_at_screen(reason: str = "") -> str:
        """Capture the user's screen and describe its regions.

        This does not show you any screen content. It tells you how the screen has been
        divided into numbered regions so you can request the ones you need with
        read_screen_region. A local gate inspects regions before any are revealed.

        Args:
            reason: Why you need to look, in a few words.
        """
        if source is not None:
            image = Image.open(source)
        else:
            try:
                import mss
            except ImportError:
                return (
                    "Screen capture is unavailable (install the 'capture' extra). "
                    "Ask the user to describe the problem in text."
                )
            with mss.mss() as sct:
                shot = sct.grab(sct.monitors[1])
                image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        buffer.put(image)
        return buffer.describe()

    return look_at_screen


def make_read_screen_region(buffer: ScreenBuffer, *, model_id: str = READER_MODEL):
    """Build the guarded tool: the only one that sends pixels off the machine.

    By the time this runs, `ScreenGate` has already inspected the capture and may have
    rewritten `regions` to a narrower set. The tool re-checks against the gate's result
    anyway, so that a misconfigured intervention list cannot turn into a leak -- the
    gate is the policy, but this is the enforcement point.
    """
    client = boto3.client("bedrock-runtime", region_name=REGION)

    @tool
    def read_screen_region(regions: Sequence[int], question: str = "") -> str:
        """Read the contents of specific numbered screen regions.

        Args:
            regions: Which region numbers to read, from look_at_screen.
            question: What you are trying to find out from them.
        """
        if buffer.image is None:
            return "No screen captured. Call look_at_screen first."

        result = buffer.result
        permitted = {t.index for t in result.injectable} if result else set()
        wanted = [int(i) for i in regions]
        allowed = [i for i in wanted if i in permitted]

        # What was held back is a property of the gate's findings, not of this call's
        # arguments. ScreenGate rewrites `regions` via Transform before we run, so by
        # the time we get here the excluded regions are already gone from the list --
        # ask the arguments and the answer is always "nothing was withheld".
        #
        # Two kinds of exclusion, and conflating them would mislead the user. A region
        # held for safety is a warning they need to act on; a region skipped as
        # irrelevant is just housekeeping.
        unsafe = sorted(t.index for t in result.flagged) if result else []
        irrelevant = (
            sorted(
                t.index
                for t in result.cleared
                if t.index not in permitted and t not in result.illegible
            )
            if result
            else []
        )

        if not allowed:
            return (
                "Every region you asked for was withheld by the local gate"
                + (f" ({', '.join(result.reasons)})" if result else "")
                + ". Nothing was sent. Ask the user to describe the problem in words."
            )

        tiles = [t for i in allowed if (t := buffer.tile(i)) is not None]
        content: list[dict] = [
            {
                "text": (
                    f"These are {len(tiles)} region(s) of a developer's screen "
                    f"(numbers {allowed}). "
                    + (f"Question: {question}. " if question else "")
                    + "Transcribe any error messages verbatim and describe what the "
                    "screen shows. Be concise."
                )
            }
        ]
        for t in tiles:
            content.append(
                {"image": {"format": "png", "source": {"bytes": _png(t.image)}}}
            )

        response = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": content}],
            inferenceConfig={"maxTokens": 600, "temperature": 0.0},
        )
        text = response["output"]["message"]["content"][0]["text"].strip()
        usage = response["usage"]

        # The agent has to be told *why* a region was held back, not just that it was.
        # Without the reason it cannot give the advice that matters -- rotate the key,
        # check whether the file is ignored by git -- and in testing it simply ignored
        # the omission and answered as though it had seen everything.
        parts: list[str] = []
        if unsafe:
            why = ", ".join(result.reasons) if result and result.reasons else "unspecified"
            parts.append(
                f"regions {unsafe} were WITHHELD FOR SAFETY and never transmitted "
                f"(reason: {why}). You MUST tell the user which part of their screen "
                "was withheld and why. If a credential was involved, tell them to "
                "rotate it and check whether the file holding it is ignored by git."
            )
        if irrelevant:
            parts.append(
                f"regions {irrelevant} were skipped as unrelated to the question, not "
                "for safety. No need to warn the user about these."
            )
        note = ""
        if parts:
            note = (
                f"\n\n[APERTURE: {len(allowed)} of {len(buffer.tiles)} regions were "
                f"sent. " + " ".join(parts) + "]"
            )
        return (
            f"{text}{note}\n\n[reader: {usage['inputTokens']} in / "
            f"{usage['outputTokens']} out tokens]"
        )

    return read_screen_region


def make_check_local_file():
    """A tool that reads the filesystem but never the screen.

    Included because the useful advice after catching a leaked key is usually about the
    repository, not the screenshot: is the file that held it ignored by git? The agent
    can establish that locally, with no pixels involved.
    """

    @tool
    def check_gitignore(filename: str, start_directory: str = ".") -> str:
        """Check whether a filename is covered by .gitignore in or above a directory.

        Args:
            filename: The file to check, for example "production.env".
            start_directory: Where to start looking for .gitignore files.
        """
        here = Path(start_directory).resolve()
        checked: list[str] = []
        for directory in [here, *here.parents]:
            candidate = directory / ".gitignore"
            if not candidate.exists():
                continue
            checked.append(str(candidate))
            patterns = [
                line.strip()
                for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines()
                if line.strip() and not line.startswith("#")
            ]
            for pattern in patterns:
                bare = pattern.strip("/*")
                if bare and (bare in filename or filename.endswith(bare)):
                    return (
                        f"'{filename}' appears to be ignored by {candidate} "
                        f"(pattern '{pattern}')."
                    )
        if not checked:
            return f"No .gitignore found at or above {here}."
        return (
            f"'{filename}' does NOT appear to be ignored. Checked: "
            f"{', '.join(checked)}."
        )

    return check_gitignore
