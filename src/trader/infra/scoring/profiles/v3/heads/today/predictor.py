"""Today V3 predictor with its own artifact identity."""

from trader.domain.recommendation.models import Strategy
from trader.infra.scoring.profiles.v3.bundle_codec import V3HeadBundleArtifact
from trader.infra.scoring.profiles.v3.heads.inference import V3HeadInference


class V3TodayPredictor(V3HeadInference):
    def __init__(self, artifact: V3HeadBundleArtifact) -> None:
        super().__init__(artifact, Strategy.TODAY)


__all__ = ["V3TodayPredictor"]
