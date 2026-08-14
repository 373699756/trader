"""Research-only application boundary contracts."""

from trader.application.research.challengers import ScoreR4ChallengerReplayer
from trader.application.research.extraction import ScoreR2ExtractionPolicy, ScoreR2HistoricalExtractor
from trader.application.research.replay import ScoreR3BaselineReplayer
from trader.application.research.score_r5 import ScoreR5FinalSealer, ScoreR5ForwardCollector, ScoreR5StatisticalGate

__all__ = [
    "ScoreR2ExtractionPolicy",
    "ScoreR2HistoricalExtractor",
    "ScoreR3BaselineReplayer",
    "ScoreR4ChallengerReplayer",
    "ScoreR5FinalSealer",
    "ScoreR5ForwardCollector",
    "ScoreR5StatisticalGate",
]
