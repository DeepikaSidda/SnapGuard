"""Wiring a RegionGate into a Strands agent, so the decision sits in the agent's path.

THE SECOND HALF OF THE REUSABLE PATTERN. `region_gate.py` decides; this puts the decision
somewhere it cannot be skipped.

Strands' intervention system calls `before_tool_call` before any tool executes, and what
you return is a typed action rather than prose:

    Proceed    let it run unchanged
    Transform  mutate the call in place, then let it run
    Guide      hand the model its turn back with feedback
    Deny       block it; the reason is shown to the model
    Confirm    stop and ask a human

Most gates only ever use Proceed and Deny, which makes them a bouncer. The interesting
one is `Transform`: it lets the gate *narrow* a request instead of refusing it. An agent
that asked to read nine regions of your screen gets to read the two that were clean and
relevant, and never learns the other seven existed. That is a better outcome than either
allowing the lot or blocking the lot, and it is why this handler maps a four-value
verdict onto four different actions.

A note on where the images come from. The handler cannot conjure the screenshot out of
the tool call, so capture and inspection share a `ScreenBuffer`: the capture tool writes
the frame into it, and the handler reads it back when the agent tries to send regions
onward. Pass the same buffer to both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from PIL import Image

from ..tiling import Tile, tile_grid
from .region_gate import GateResult, RegionGate, Verdict

try:  # Strands is an optional dependency: the gate itself does not need it.
    from strands.interventions import (
        Confirm,
        Deny,
        Guide,
        InterventionHandler,
        Proceed,
        Transform,
    )

    STRANDS_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only in a bare install
    STRANDS_AVAILABLE = False

    class InterventionHandler:  # type: ignore[no-redef]
        """Stand-in so this module imports without strands-agents installed."""

        name = "stub"


@dataclass
class ScreenBuffer:
    """The last captured frame, its tiles, and what the gate concluded about it.

    Shared mutable state between a capture tool and the handler. Deliberately small and
    deliberately not a global: construct one, hand it to both.
    """

    image: Image.Image | None = None
    tiles: list[Tile] = field(default_factory=list)
    result: GateResult | None = None
    asking_about: str = ""
    # Coarse on purpose, and deliberately coarser than the gate's own default of 6x4.
    # These tiles are not redaction boxes, they are the regions the agent names and the
    # crops that get sent to the cloud model, so they have two constraints a redaction
    # box does not have: a region has to be large enough for the cloud model to read
    # (a 6x4 region of a laptop screen is a 180x152 thumbnail, which is useless to it),
    # and few enough that an LLM can reason about them by number. 2x2 maps onto what a
    # person would say anyway -- the editor, the terminal, the sidebar.
    #
    # The consequence is that this path is all-or-nothing per quarter: a region holding
    # a credential is withheld whole rather than sent with the credential blacked out.
    # Redacting the fine gate's boxes inside a coarse crop would be strictly better and
    # is the obvious next piece of work; it is not built.
    columns: int = 2
    rows: int = 2

    def put(self, image: Image.Image) -> list[Tile]:
        self.image = image
        self.tiles = tile_grid(image, columns=self.columns, rows=self.rows)
        self.result = None
        return self.tiles

    def tile(self, index: int) -> Tile | None:
        for t in self.tiles:
            if t.index == index:
                return t
        return None

    def describe(self) -> str:
        """What the agent is told about the capture -- shape only, never content.

        The model is given the geometry so it can ask for regions by number, but no
        pixels. Nothing has been cleared at this point, so nothing is shown.
        """
        if self.image is None:
            return "No screen has been captured yet."
        width, height = self.image.size
        lines = [
            f"Captured a {width}x{height} screen, cut into "
            f"{self.columns}x{self.rows} = {len(self.tiles)} numbered regions:"
        ]
        for t in self.tiles:
            lines.append(f"  region {t.index}: row {t.row}, column {t.column}")
        lines.append(
            "Call read_screen_region with the region numbers you want read. A local "
            "gate inspects them first and may narrow your request."
        )
        return "\n".join(lines)


class ScreenGate(InterventionHandler):
    """Inspect captured screen regions before a tool can send them anywhere.

    Example::

        buffer = ScreenBuffer()
        gate = RegionGate(Decider(), rules=REGION_RULES, context_rules=CONTEXT_RULES)
        agent = Agent(
            model="us.amazon.nova-2-lite-v1:0",
            tools=[look_at_screen(buffer), read_screen_region(buffer)],
            interventions=[ScreenGate(gate, buffer, guarded_tools=["read_screen_region"])],
        )
    """

    name = "aperture-screen-gate"

    def __init__(
        self,
        gate: RegionGate,
        buffer: ScreenBuffer,
        *,
        guarded_tools: Sequence[str] = ("read_screen_region",),
        region_argument: str = "regions",
        confirm_when_unreadable: bool = False,
        on_decision: Callable[[GateResult], None] | None = None,
    ) -> None:
        if not STRANDS_AVAILABLE:  # pragma: no cover
            raise ImportError(
                "ScreenGate needs strands-agents. Install with: pip install "
                "'aperture-gate[agent]'"
            )
        self.gate = gate
        self.buffer = buffer
        self.guarded_tools = set(guarded_tools)
        self.region_argument = region_argument
        self.confirm_when_unreadable = confirm_when_unreadable
        self.on_decision = on_decision
        self.log: list[dict[str, Any]] = []

    # ---- the seam ----------------------------------------------------------

    def before_tool_call(self, event, **kwargs):  # noqa: ANN001, ANN201
        tool_name = event.tool_use["name"]
        if tool_name not in self.guarded_tools:
            return Proceed()

        if self.buffer.image is None:
            return Guide(
                feedback=(
                    "There is no captured screen to read. Call look_at_screen first."
                )
            )

        # One inspection per capture. An agent often asks for regions more than once,
        # and re-running a 12-second gate on identical pixels is waste.
        result = self.buffer.result
        if result is None:
            result = self.gate.inspect(
                self.buffer.image,
                # The user's actual words, so regions can be scored for relevance as
                # well as safety. Set by Session.__call__ before the agent runs.
                asking_about=self.buffer.asking_about or None,
            )
            self.buffer.result = result
            if self.on_decision:
                self.on_decision(result)

        requested = self._requested(event)
        # Safe AND relevant AND readable, and empty if the whole frame was condemned.
        # Falls back to the full permitted set when the model asked for regions that
        # are all excluded but others are available.
        permitted = {t.index for t in result.injectable}
        allowed = sorted(i for i in requested if i in permitted)
        if not allowed and permitted:
            allowed = sorted(permitted)
        withheld = sorted(set(requested) - set(allowed))

        action, verdict = self._decide(result, allowed, withheld, event)
        self.log.append(
            {
                "tool": tool_name,
                "requested": requested,
                "allowed": allowed,
                "withheld": withheld,
                "verdict": verdict,
                "gate": result.verdict.value,
                "reasons": result.reasons,
                "decider_calls": result.decider_calls,
                "decider_ms": round(result.decider_ms),
            }
        )
        return action

    # ---- verdict -> typed action ------------------------------------------

    def _decide(
        self,
        result: GateResult,
        allowed: list[int],
        withheld: list[int],
        event,  # noqa: ANN001
    ):
        reasons = ", ".join(result.reasons) or "no reason recorded"

        if result.verdict is Verdict.UNREADABLE:
            if self.confirm_when_unreadable:
                return (
                    Confirm(
                        prompt=(
                            "Aperture could not judge this screen capture "
                            f"({reasons}). Send it anyway?"
                        )
                    ),
                    "confirm",
                )
            return (
                Guide(
                    feedback=(
                        f"The capture could not be judged ({reasons}), so nothing was "
                        "sent. Ask the user to describe the problem in text, or to "
                        "bring the relevant window to the front and try again."
                    )
                ),
                "guided",
            )

        if result.verdict is Verdict.WITHHOLD:
            # Nothing on this screen can be sent, and no crop fixes it. Record the
            # denial honestly: nothing was allowed, everything requested was withheld.
            allowed.clear()
            withheld[:] = sorted(set(self._requested(event)))
            return (
                Deny(
                    reason=(
                        f"Aperture withheld this screen capture: {reasons}. The whole "
                        "frame is sensitive, so no region of it was sent. Ask the user "
                        "to describe the problem in words instead."
                    )
                ),
                "denied",
            )

        if not allowed:
            # Safe, but nothing on screen bears on the question. Not a safety refusal,
            # and it would be wrong to tell the user their screen was withheld.
            return (
                Guide(
                    feedback=(
                        "Nothing on the captured screen appears to relate to what the "
                        "user asked. Nothing was sent. Ask them which window the "
                        "problem is in, or to describe it in words."
                    )
                ),
                "irrelevant",
            )

        if withheld:
            # The interesting branch. Part of the screen is unsafe, the rest is useful,
            # so narrow the call rather than refusing it. The model never sees the
            # withheld regions and is told plainly that this happened.
            def narrow(e, allowed=allowed):  # noqa: ANN001, ANN202
                e.tool_use["input"][self.region_argument] = allowed

            return (
                Transform(
                    apply=narrow,
                    reason=(
                        f"Aperture withheld regions {withheld} ({reasons}) and allowed "
                        f"{allowed}."
                    ),
                ),
                "narrowed",
            )

        return Proceed(reason=f"all {len(allowed)} requested regions cleared"), "passed"

    # ---- helpers -----------------------------------------------------------

    def _requested(self, event) -> list[int]:  # noqa: ANN001
        raw = (event.tool_use.get("input") or {}).get(self.region_argument)
        if raw is None:
            return [t.index for t in self.buffer.tiles]
        if isinstance(raw, int):
            return [raw]
        out: list[int] = []
        for item in raw:
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                continue
        return out

    # ---- reporting ---------------------------------------------------------

    def summary(self) -> str:
        if not self.log:
            return "the gate was never consulted"
        narrowed = sum(1 for r in self.log if r["verdict"] == "narrowed")
        denied = sum(1 for r in self.log if r["verdict"] == "denied")
        calls = sum(r["decider_calls"] for r in self.log)
        return (
            f"{len(self.log)} guarded calls: {narrowed} narrowed, {denied} denied; "
            f"{calls} local decisions"
        )
