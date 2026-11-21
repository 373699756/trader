"""Construction of the profile's publication persistence resources."""

from pathlib import Path

from trader.infra.process_lock import ProcessLock
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.recommendation.infra.runtime import composition_inputs
from trader.recommendation.infra.deepseek.budget import DeepSeekBudgetLedger
from trader.recommendation.infra.deepseek.budget_file import BudgetStateFile
from trader.recommendation.infra.deepseek.health_gate import DeepSeekHealthPolicy
from trader.recommendation.infra.persistence.freeze_records import SQLiteFreezeRecords
from trader.recommendation.infra.persistence.outcome_evidence_index import SQLiteOutcomeEvidenceIndex
from trader.recommendation.infra.persistence.publication_sequence import PublicationSequence
from trader.recommendation.infra.persistence.strategy_freeze_records import StrategyFreezeRecords
from trader.recommendation.application.ports.freeze_records import FreezeRecordPort
from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget


def BuildPublicationPersistence(
    context: composition_inputs.RuntimeBuildContext,
    shared_budget: DeepSeekBudgetLedger | None = None,
    *,
    budget_path: Path,
    sqlite_policy: SqliteIoPolicy,
    tomorrow_target: PredictionTarget | None = None,
) -> composition_inputs.RuntimePersistenceContext:
    settings = context.settings
    profile_root = settings.data_paths.recommendation(context.strategy.scoring_profile)
    records: FreezeRecordPort = SQLiteFreezeRecords(
        profile_root / "freeze.sqlite3", cache=context.workers.market_cache, wall_clock=context.now, sqlite_policy=sqlite_policy,
    )
    tomorrow_settlement_records = None
    if tomorrow_target is not None:
        tomorrow_records = SQLiteFreezeRecords(
            settings.data_paths.root / "recommendation/tomorrow/freeze.sqlite3",
            cache=context.workers.market_cache, wall_clock=context.now, sqlite_policy=sqlite_policy,
        )
        grouped_records = SQLiteFreezeRecords(
            settings.data_paths.root / "recommendation/tomorrow/targets.sqlite3", sqlite_policy=sqlite_policy, grouped=True,
        )
        routed = StrategyFreezeRecords(
            records,
            tomorrow_records,
            grouped_records,
            context.strategy.scoring_profile,
            tomorrow_target,
            context.owns_shared_runtime,
        )
        records = routed
        tomorrow_settlement_records = routed.tomorrow_settlement_records
    outcomes = SQLiteOutcomeEvidenceIndex(
        settings.data_paths.research(context.strategy.scoring_profile), records, sqlite_policy=sqlite_policy,
        tomorrow_database=settings.data_paths.root / "research/tomorrow/outcomes.sqlite3" if tomorrow_target is not None else None,
        tomorrow_settlement_records=tomorrow_settlement_records,
    )
    budget = shared_budget or DeepSeekBudgetLedger(
        BudgetStateFile(budget_path, ProcessLock(settings.data_paths.root / ".deepseek.lock", label="DeepSeek")),
        daily_hard_limit=settings.deepseek.daily_hard_limit, strategy_limits=settings.deepseek.strategy_limits,
        stage_targets=settings.deepseek.stage_targets, stage_limits=settings.deepseek.stage_limits,
        health_policy=DeepSeekHealthPolicy.from_settings(settings.deepseek.adaptive), enabled=settings.deepseek.enabled,
    )
    sequence = PublicationSequence(profile_root / "publication_state.sqlite3", sqlite_policy=sqlite_policy)
    return composition_inputs.RuntimePersistenceContext(sequence, records, budget, outcomes)
