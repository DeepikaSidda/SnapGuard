"""Reusable Decider 2B decision patterns.

`region_gate` is the pattern this project contributes: score the regions of an image
against a written rubric, coarse to fine, and compose a verdict in your own code. It
knows nothing about Aperture and is meant to be copied out.

`intervention` wires that gate into a Strands agent, so a decision sits in the agent's
path rather than beside it.

See README.md in this directory for how to use either one.
"""

from .region_gate import (
    GateResult,
    Policy,
    RegionFinding,
    RegionGate,
    Rule,
    Verdict,
)

__all__ = [
    "GateResult",
    "Policy",
    "RegionFinding",
    "RegionGate",
    "Rule",
    "Verdict",
]
