"""Strategy-specific score composition."""

from trader.domain.models import FeatureSnapshot, Strategy
from trader.domain.strategies.composition import LocalScoreResult
from trader.domain.strategies.d25 import score_d25
from trader.domain.strategies.long import score_long
from trader.domain.strategies.today import score_today
from trader.domain.strategies.tomorrow import score_tomorrow


def score_strategy(strategy: Strategy, snapshot: FeatureSnapshot) -> LocalScoreResult:
    scorers = {
        Strategy.TODAY: score_today,
        Strategy.TOMORROW: score_tomorrow,
        Strategy.D25: score_d25,
        Strategy.LONG: score_long,
    }
    return scorers[strategy](snapshot)


__all__ = ["LocalScoreResult", "score_strategy"]
