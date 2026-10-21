"""The agent: Amazon Bedrock does the thinking, Decider decides what it may look at.

The system prompt is deliberately eager about looking at the screen, for the same reason
the upstream Strands example makes its weather agent eager to guess a city: the gate is
only interesting if the agent would otherwise overreach. A cautious agent would never
trigger it, and a demo where nothing happens proves nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from strands import Agent
from strands.models import BedrockModel

from ..decider import Decider
from ..patterns import Policy, RegionGate
from ..patterns.intervention import ScreenBuffer, ScreenGate
from ..questions import (
    CONTEXT_RULES,
    LEGIBLE,
    REGION_RULES,
    SCREEN_KIND,
    relevance_rule,
)

# Drives the loop and writes the answer. Nova 2 Lite reads images, calls tools, and was
# the most accurate Nova on our eval screens.
AGENT_MODEL = "us.amazon.nova-2-lite-v1:0"

SYSTEM_PROMPT = """You are a developer's assistant who can look at their screen.

When the user describes a problem they are seeing, look at the screen straight away with
look_at_screen, then read the regions you need with read_screen_region. Be decisive: ask
for any region that might hold the error.

A local gate inspects screen regions before you see them. It may withhold some, narrow
your request, or refuse entirely. Tool results will tell you when this happened, in a
line beginning [APERTURE.

When a region was withheld you must do all of the following:
  1. Answer the user's original question with what you were given.
  2. Say explicitly which part of their screen was withheld and why. Do not pretend you
     saw the whole screen.
  3. If a credential was involved, tell them to rotate it, and call check_gitignore on
     the file that held it to see whether git is tracking it.

Never ask the user to send withheld content another way, and never ask them to read a
credential out to you. It was withheld to protect them.

Be brief and concrete."""


@dataclass
class Session:
    """An assembled agent, plus the pieces worth inspecting afterwards."""

    agent: Agent
    gate: ScreenGate
    buffer: ScreenBuffer
    decider: Decider

    def __call__(self, prompt: str):
        self.buffer.asking_about = prompt
        return self.agent(prompt)

    def report(self) -> str:
        lines = [self.gate.summary(), f"decider: {self.decider}"]
        if self.buffer.result is not None:
            r = self.buffer.result
            total = len(self.buffer.tiles) or 1
            sending = len(r.injectable)
            lines.append(
                f"regions: {sending} of {total} sent to Bedrock "
                f"({len(r.flagged)} withheld for safety, "
                f"{total - len(r.flagged) - sending} unrelated or unreadable)"
            )
            lines.append(
                f"{1 - sending / total:.0%} of the frame never left this machine"
            )
        return "\n".join(lines)


def build(
    *,
    screen: Path | None = None,
    decider_url: str | None = None,
    model_id: str = AGENT_MODEL,
    columns: int = 2,
    rows: int = 2,
    on_decision=None,
) -> Session:
    """Assemble the agent, the gate, and the buffer they share."""
    from .tools import make_check_local_file, make_look_at_screen, make_read_screen_region

    decider = Decider(decider_url)
    decider.require_vision()

    buffer = ScreenBuffer(columns=columns, rows=rows)
    region_gate = RegionGate(
        decider,
        rules=REGION_RULES,
        context_rules=CONTEXT_RULES,
        info_questions=SCREEN_KIND,
        # Both axes of the context-and-memory decision. Safety alone tells you what you
        # *may* send; relevance tells you what is worth sending, and the intersection is
        # what actually reaches Bedrock.
        relevance=relevance_rule,
        legibility=LEGIBLE,
        policy=Policy(fail_closed=True),
        columns=columns,
        rows=rows,
    )
    screen_gate = ScreenGate(
        region_gate,
        buffer,
        guarded_tools=["read_screen_region"],
        region_argument="regions",
        on_decision=on_decision,
    )

    agent = Agent(
        model=BedrockModel(model_id=model_id, region_name="us-east-1", temperature=0.0),
        tools=[
            make_look_at_screen(buffer, source=screen),
            make_read_screen_region(buffer),
            make_check_local_file(),
        ],
        interventions=[screen_gate],
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,  # the gate's own log is the interesting output
    )
    return Session(agent=agent, gate=screen_gate, buffer=buffer, decider=decider)
