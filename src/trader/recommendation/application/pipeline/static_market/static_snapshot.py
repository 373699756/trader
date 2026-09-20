"""Stage-2 static collection output identity."""

from typing import TypeAlias

from trader.recommendation.application.pipeline.stage_output import PipelineStageOutput
from trader.recommendation.domain.market.models import FeatureSnapshot

StaticMarketSnapshot: TypeAlias = PipelineStageOutput[FeatureSnapshot]

__all__ = ["StaticMarketSnapshot"]
