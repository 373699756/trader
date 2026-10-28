from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.unit.application.test_tomorrow_freezing import _assert_formal_current_and_history, _at, _Clock, _coordinator
from tests.unit.domain.test_freeze_identity import freeze
from trader.http_api.route_services import UnifiedWebServices
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedFreezeDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedFreezeEventStream
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedFreezeQueries
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.domain.publication.freeze_identity import FormalScoredFreeze, ScoredFreeze
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.publication.preview import PreviewContext
from trader.recommendation.infra.persistence.freeze_records import SQLiteFreezeRecords
from trader.recommendation.infra.persistence.outcome_evidence_index import SQLiteOutcomeEvidenceIndex
from trader.web import CreateApp


def _production(strategy: Strategy) -> ScoredFreeze:
    source = freeze(strategy)
    return replace(
        source,
        input_versions=(
            ("score_scale", "learned_rank_percentile_0_100"),
            ("score_model_id", "learned_rank"),
            ("score_model", "learned_rank:" + "a" * 64),
            ("score_contract_hash", "b" * 64),
            ("rank_population", "learned-rank-population:" + "c" * 64),
            ("execution_mode", "production"),
            ("production_authority", "true"),
        ),
        items=(replace(source.items[0], final_score=99.5),),
    )


@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
@pytest.mark.parametrize("hour,minute", ((10, 0), (12, 30), (14, 59), (15, 0), (15, 10)))
@pytest.mark.parametrize("cold", (False, True), ids=("hot", "cold"))
def test_production_rank_freeze_and_history_keep_identity(
    tmp_path: Path, strategy: Strategy, hour: int, minute: int, cold: bool
) -> None:
    now = _at(hour, minute)
    source = _production(strategy)
    observed_at = min(now, _at(14, 59, 40))
    item = source.items[0]
    assert item.quote is not None
    current = replace(
        source, observed_at=observed_at, items=(replace(item, quote=replace(item.quote, source_time=observed_at)),)
    )
    path = tmp_path / "v1/freeze.sqlite3"
    records = SQLiteFreezeRecords(path)
    records.initialize()
    index = UnifiedFreezeIndex()
    if cold:
        records = SQLiteFreezeRecords(path)
    else:
        assert index.publish(current, expected_version=None).accepted
    clock = _Clock(now)
    coordinator = _coordinator(index, records, clock, strategy)
    result = coordinator.freeze_scheduled()
    if hour < 15:
        assert result.status == "before_freeze"
        assert records.load(strategy, now.date()) is None
        return
    if cold:
        assert result.status == "no_eligible_freeze"
        result = coordinator.freeze_close_fallback(
            current, recovery_path="close_rebuild", official_close_version="official-close:fixture"
        )
    assert result.status == "frozen" and result.record is not None
    assert result.record.commit_kind == ("close_fallback" if cold else "scheduled")
    assert all(dict(result.record.freeze.input_versions)[key] == value for key, value in current.input_versions)
    assert result.record.freeze.items[0].final_score == 99.5
    assert result.record.freeze.items[0].local_score == 88.0
    _assert_formal_current_and_history(index, records, clock, result.record)
    later = replace(current, sequence=2, observed_at=clock.now())
    assert (
        coordinator.freeze_close_fallback(
            later, recovery_path="close_rebuild", official_close_version="official-close:fixture"
        ).record
        == result.record
    )
    restored = UnifiedFreezeIndex()
    assert (
        _coordinator(restored, SQLiteFreezeRecords(path), clock, strategy).restore(now.date()).record == result.record
    )
    assert restored.snapshot(strategy).current == result.record.freeze


@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
def test_production_rank_close_fallback_is_first_wins_and_preview_remains_ephemeral(tmp_path: Path, strategy: Strategy):
    clock = _Clock(_at(15, 10))
    records = SQLiteFreezeRecords(tmp_path / "freeze.sqlite3")
    records.initialize()
    index = UnifiedFreezeIndex()
    coordinator = _coordinator(index, records, clock, strategy)
    source = _production(strategy)
    preview = replace(
        source,
        preview_context=PreviewContext("fixture-preview", source.trade_date - timedelta(days=1), _at(10, 0)),
        items=(replace(source.items[0], quote=None),),
    )
    assert (
        coordinator.freeze_close_fallback(
            preview, recovery_path="close_rebuild", official_close_version="official-close:fixture"
        ).status
        == "preview_only"
    )
    with pytest.raises(ValueError, match="preview"):
        FormalScoredFreeze(preview)
    assert records.list_dates(strategy) == ()
    result = coordinator.freeze_close_fallback(
        source, recovery_path="close_rebuild", official_close_version="official-close:fixture"
    )
    assert result.status == "frozen" and result.record is not None
    assert result.record.commit_kind == "close_fallback"
    assert coordinator.freeze_scheduled().record == result.record
    assert records.list_dates(strategy) == (source.trade_date,)
    targets = SQLiteOutcomeEvidenceIndex(tmp_path / "research", records).pending_outcome_targets(limit=10)
    assert len(targets) == 1
    assert targets[0].snapshot_id == result.record.version
    assert targets[0].strategy is strategy
    queries = UnifiedFreezeQueries(index, UnifiedFreezeDraftIndex(), records, clock)
    services = UnifiedWebServices(queries, UnifiedFreezeEventStream(), lambda: {}, profile_id="v1")
    client = CreateApp(services=services).test_client()
    for suffix in ("current", f"history?date={source.trade_date}"):
        response = client.get(f"/api/freezes/{strategy.value}/{suffix}")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["frozen"] is True and payload["freeze_kind"] == "close_fallback"
        assert payload["scoring_profile"] == "v1"
        assert payload["input_versions"] == dict(result.record.freeze.input_versions)
        assert payload["items"][0]["scores"]["final"] == 99.5
        assert payload["items"][0]["scores"]["local"] == 88.0


@pytest.mark.parametrize(
    "missing", ("execution_mode", "production_authority", "score_model", "score_contract_hash", "rank_population")
)
def test_production_rank_requires_complete_authority_and_model_identity(missing: str):
    source = _production(Strategy.TOMORROW)
    with pytest.raises(ValueError, match="execution and model identity"):
        replace(source, input_versions=tuple((key, value) for key, value in source.input_versions if key != missing))
