"""Cold recovery preserves independently committed profile records after a hard exit."""

from __future__ import annotations

import multiprocessing
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock

import pytest

from tests.helpers.recommendation.publication.freeze_identity import freeze
from tests.helpers.recommendation.runtime.tomorrow_freezing import _Clock, _at
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedFreezeDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.freeze_coordinator import (
    FreezeRuntimeIdentity,
    ScoredFreezeCoordinator,
)
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedFreezeQueries
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import (
    UnifiedFreezeIndex,
    UnifiedProfilePair,
)
from trader.recommendation.domain.publication.freeze_identity import FreezeOverlay, ScoredFreeze
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS, ScoringProfileId
from trader.recommendation.infra.persistence.freeze_record_codec import CommittedRecordBytes
from trader.recommendation.infra.persistence.freeze_records import SQLiteFreezeRecords

_STRATEGIES = (Strategy.TOMORROW, Strategy.D25)
_HARD_EXIT_CODE = 73


@dataclass(frozen=True)
class _Profile:
    name: ScoringProfileId
    records: SQLiteFreezeRecords
    index: UnifiedFreezeIndex
    queries: UnifiedFreezeQueries
    freezers: tuple[ScoredFreezeCoordinator, ...]


def _source(profile: ScoringProfileId, strategy: Strategy, *, restarted: bool = False) -> ScoredFreeze:
    at = _at(15, 5) if restarted else _at(14, 59, 59)
    source = freeze(strategy, sequence=99 if restarted else SCORING_PROFILE_IDS.index(profile) + 1)
    item = source.items[0]
    assert item.quote is not None
    generation = "restarted" if restarted else "original"
    return replace(
        source,
        observed_at=at,
        config_version=f"{profile}:config:{generation}",
        strategy_version=f"{profile}:strategy:{generation}",
        fusion_version=f"{profile}:fusion:{generation}",
        input_versions=(("market", f"{profile}:{generation}"), ("daily", "fixture-history")),
        items=(replace(item, quote=replace(item.quote, source_time=at)),),
    )


def _profiles(
    root: Path,
    *,
    restarted: bool,
    sqlite_policy: SqliteIoPolicy,
    fault: Callable[[str], None] | None = None,
) -> tuple[_Profile, ...]:
    lock = RLock()
    clock = _Clock(_at(15, 5) if restarted else _at(15, 0))
    profiles = []
    for name in SCORING_PROFILE_IDS:
        records = SQLiteFreezeRecords(
            root / name / "freeze.sqlite3", fault_injector=fault, sqlite_policy=sqlite_policy,
        )
        records.initialize()
        index = UnifiedFreezeIndex(transaction_lock=lock)
        source = _source(name, Strategy.TOMORROW, restarted=restarted)
        identity = FreezeRuntimeIdentity(source.config_version, source.strategy_version, source.fusion_version)
        freezers = tuple(
            ScoredFreezeCoordinator(index, records, clock, runtime_identity=identity, strategy=strategy)
            for strategy in _STRATEGIES
        )
        profiles.append(_Profile(
            name, records, index, UnifiedFreezeQueries(index, UnifiedFreezeDraftIndex(), records, clock), freezers,
        ))
    return tuple(profiles)


def _pair(profile: _Profile, *, restarted: bool) -> UnifiedProfilePair:
    tomorrow = _source(profile.name, Strategy.TOMORROW, restarted=restarted)
    d25 = _source(profile.name, Strategy.D25, restarted=restarted)

    def overlay(source: ScoredFreeze) -> FreezeOverlay:
        quote = source.items[0].quote
        assert quote is not None
        return FreezeOverlay(source.strategy, source.trade_date, source.version, source.observed_at, (quote,), sequence=1)

    current_tomorrow = profile.index.snapshot(Strategy.TOMORROW).current
    current_d25 = profile.index.snapshot(Strategy.D25).current
    return UnifiedProfilePair(
        profile.name, profile.index, (tomorrow, d25), (overlay(tomorrow), overlay(d25)),
        (current_tomorrow.version if current_tomorrow else None, current_d25.version if current_d25 else None),
    )


def _interrupt_writer(root: Path, stop_head: int, fault_point: str, sqlite_policy: SqliteIoPolicy) -> None:
    current_head = -1

    def interrupt(point: str) -> None:
        if current_head == stop_head and point == fault_point:
            os._exit(_HARD_EXIT_CODE)

    profiles = _profiles(root, restarted=False, sqlite_policy=sqlite_policy, fault=interrupt)
    assert UnifiedFreezeIndex.publish_scored_batch(tuple(_pair(profile, restarted=False) for profile in profiles)).accepted
    for profile in profiles:
        for freezer in profile.freezers:
            current_head += 1
            assert freezer.freeze_scheduled().status == "frozen"
    raise AssertionError("the requested hard exit did not happen")


def _recover_and_complete(root: Path, committed_heads: int, sqlite_policy: SqliteIoPolicy) -> None:
    profiles = _profiles(root, restarted=True, sqlite_policy=sqlite_policy)
    queries = tuple((profile.name, profile.queries) for profile in profiles)
    assert all(view.status == "not_ready" for _, views in UnifiedFreezeQueries.current_batch(queries) for view in views)
    for head, freezer in enumerate(coordinator for lane in profiles for coordinator in lane.freezers):
        result = freezer.restore(_at(15, 0).date())
        assert result.status == ("already_frozen" if head < committed_heads else "formal_freeze_unavailable")
    partial = UnifiedFreezeQueries.current_batch(queries)
    for head, view in enumerate(view for _, views in partial for view in views):
        assert (view.status, view.frozen) == (("ready", True) if head < committed_heads else ("not_ready", False))
        if head < committed_heads:
            assert view.freeze_kind == "scheduled" and view.frozen_at == _at(15, 0)
        else:
            assert view.freeze_version is None and view.items == ()

    # A late complete input must not fill the holes by replacing already sealed heads.
    late = UnifiedFreezeIndex.publish_scored_batch(tuple(_pair(profile, restarted=True) for profile in profiles))
    assert not late.accepted and late.reason == "freeze_sealed"
    assert UnifiedFreezeQueries.current_batch(queries) == partial

    for head, (profile, strategy, freezer) in enumerate(
        (lane, target_strategy, coordinator)
        for lane in profiles
        for target_strategy, coordinator in zip(_STRATEGIES, lane.freezers, strict=True)
    ):
        before = profile.records.load(strategy, _at(15, 0).date())
        if head >= committed_heads:
            assert freezer.freeze_scheduled().status == "no_eligible_freeze"
        result = freezer.freeze_close_fallback(
            _source(profile.name, strategy, restarted=True),
            recovery_path="close_rebuild",
            official_close_version="official-close:isolated-recovery",
        )
        assert result.status == ("already_frozen" if before else "frozen")
        assert result.record is not None
        assert result.record.commit_kind == ("scheduled" if before else "close_fallback")
        assert result.record.freeze.config_version == f"{profile.name}:config:{'original' if before else 'restarted'}"
        if before is not None:
            assert CommittedRecordBytes(result.record) == CommittedRecordBytes(before)
        else:
            assert dict(result.record.freeze.input_versions)["official_close"] == "official-close:isolated-recovery"
        retry = freezer.freeze_close_fallback(
            replace(_source(profile.name, strategy, restarted=True), sequence=100),
            recovery_path="close_rebuild",
            official_close_version="official-close:later-fixture",
        )
        assert retry.status == "already_frozen" and retry.record == result.record
        assert profile.queries.current(strategy).freeze_version == result.record.freeze.version
        assert profile.queries.history(strategy, result.record.trade_date).freeze_version == result.record.freeze.version


def _run_child(target: Callable[..., None], arguments: tuple[object, ...], expected_exit: int) -> None:
    process = multiprocessing.get_context("spawn").Process(target=target, args=arguments)
    process.start()
    try:
        # This includes interpreter startup and FULL SQLite writes on a busy HDD.
        # It is a fixture wait bound, independent of the product's 30-second shutdown gate.
        process.join(60)
        assert process.exitcode == expected_exit, f"child exit: {process.exitcode}; expected: {expected_exit}"
    finally:
        if process.is_alive():
            process.terminate()
            process.join(3)
            if process.is_alive():
                process.kill()
                process.join(3)
        assert not process.is_alive()
        process.close()


def _persisted_bytes(root: Path) -> tuple[tuple[str, str, bytes], ...]:
    rows = []
    for name in SCORING_PROFILE_IDS:
        with SqliteSession(root / name / "freeze.sqlite3", SqliteConnectionOptions(read_only=True)) as connection:
            rows.extend((name, row[0], bytes(row[1])) for row in connection.run(
                "SELECT strategy, record_bytes FROM freeze_record ORDER BY strategy",
            ))
    return tuple(rows)


@pytest.mark.parametrize("fault_point", ("before_commit", "after_commit"))
@pytest.mark.parametrize("stop_head", (1, 4, 6))
def test_cold_restart_preserves_partial_profile_commits_and_only_fills_missing_heads(
    tmp_path: Path, stop_head: int, fault_point: str,
) -> None:
    from tests.helpers.shared.configuration import SqlitePolicyFixture

    sqlite_policy = SqlitePolicyFixture()
    _run_child(_interrupt_writer, (tmp_path, stop_head, fault_point, sqlite_policy), _HARD_EXIT_CODE)
    before = _persisted_bytes(tmp_path)
    committed_heads = stop_head + (fault_point == "after_commit")
    assert len(before) == committed_heads

    # Spawn a new interpreter: no indexes, seals, caches or pending receipts survive.
    _run_child(_recover_and_complete, (tmp_path, committed_heads, sqlite_policy), 0)
    after = _persisted_bytes(tmp_path)
    assert len(after) == 8 and set(before) <= set(after)
