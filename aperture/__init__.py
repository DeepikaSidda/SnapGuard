"""Aperture: decide locally how much of your screen may reach a cloud model.

Strands Decider 2B scores the regions of a screenshot on your own machine. Only the
regions that are both clean and relevant are sent to Amazon Bedrock, which does the
generative work. The decision about what may leave is made somewhere it cannot leave
from -- 127.0.0.1.
"""

from .decider import Decider, DeciderUnavailable, VisionNotEnabled, choice, noul, score
from .patterns import GateResult, Policy, RegionGate, Rule, Verdict
from .tiling import Tile, tile_grid

__version__ = "0.1.0"

__all__ = [
    "Decider",
    "DeciderUnavailable",
    "VisionNotEnabled",
    "GateResult",
    "Policy",
    "RegionGate",
    "Rule",
    "Tile",
    "Verdict",
    "choice",
    "noul",
    "score",
    "tile_grid",
]
