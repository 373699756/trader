"""Read-only saved freeze values; these cannot enter current freeze publication."""

from dataclasses import dataclass
from datetime import date, datetime

from trader.recommendation.domain.publication.freeze_identity import FreezeItem, FreezeStage, SelectionDiagnostics
from trader.recommendation.domain.publication.historical_pipeline import HistoricalPipelineAudit
from trader.recommendation.domain.publication.models import Strategy


@dataclass(frozen=True)
class HistoricalFreezeSnapshot:
    strategy: Strategy
    trade_date: date
    sequence: int
    observed_at: datetime
    stage: FreezeStage
    parent_version: str | None
    input_versions: tuple[tuple[str, str], ...]
    config_version: str
    strategy_version: str
    items: tuple[FreezeItem, ...]
    filter_aggregates: tuple[tuple[str, int], ...]
    degraded_reasons: tuple[str, ...]
    population_count: int | None
    rejected_count: int | None
    selection_diagnostics: SelectionDiagnostics | None
    pipeline: HistoricalPipelineAudit | None
    is_formal: bool | None
    version: str
    content_hash: str


@dataclass(frozen=True)
class HistoricalFreezeRecord:
    freeze: HistoricalFreezeSnapshot
    committed_at: datetime
    commit_kind: str
    payload_hash: str
    version: str
    record_bytes: bytes

    @property
    def strategy(self) -> Strategy:
        return self.freeze.strategy

    @property
    def trade_date(self) -> date:
        return self.freeze.trade_date
