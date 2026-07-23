from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import queue
import resource
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import run_v15_market_data as scalar_runner

import trader.infra.market_data.merge as merge_module
from trader.application.cache import CacheIdentitySpec, build_cache_identity, canonical_json_bytes
from trader.application.events import EventAuditRecord
from trader.application.published_snapshots import PublishedSnapshotIndex
from trader.application.publisher import SnapshotPublisher
from trader.domain.market.models import FeatureSnapshot, MarketQuote
from trader.domain.recommendation.models import (
    FusionMode,
    LiveOverlay,
    Recommendation,
    RecommendationAction,
    RecommendationSnapshot,
    ScoreBreakdown,
    Strategy,
)
from trader.domain.review.models import DeepSeekReview, ReviewOutcome
from trader.infra.cache import BoundedLruCache
from trader.infra.deepseek.cache import ReviewCache
from trader.infra.market_data.columnar import ColumnarQuoteBatch, market_changes
from trader.infra.market_data.merge import merge_market_observations, observation_from_quote, snapshot_payload_hash
from trader.infra.settings import load_runtime_settings

NOW = datetime(2026, 7, 23, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
POOL_FILL_RATIO = 0.70


@dataclass(frozen=True)
class _MarketArtifacts:
    scalar_hash: str
    columnar_hash: str
    old_epoch: str
    new_epoch: str
    dirty_codes: int
    polars_bytes: int
    snapshot_bytes: int
    retained: tuple[object, ...]


class _Archive:
    def __init__(self, snapshots: Sequence[RecommendationSnapshot]) -> None:
        self._snapshots = {(item.strategy, item.trade_date): item for item in snapshots}

    def latest(self, strategy: Strategy) -> RecommendationSnapshot | None:
        dates = self.recommendation_dates(strategy)
        return self.load_frozen(strategy, dates[0]) if dates else None

    def recommendation_dates(self, strategy: Strategy) -> Sequence[str]:
        return tuple(sorted((day for candidate, day in self._snapshots if candidate is strategy), reverse=True))

    def load_frozen(self, strategy: Strategy, trade_date: str) -> RecommendationSnapshot | None:
        return self._snapshots.get((strategy, trade_date))

    def load_live_overlay(self, strategy: Strategy, trade_date: str) -> LiveOverlay | None:
        return None

    def list_events(self, *, cursor: int, limit: int) -> Sequence[EventAuditRecord]:
        return ()


def _measure(config_path: Path, fixture_path: Path) -> dict[str, Any]:
    settings = load_runtime_settings(config_path)
    stages: dict[str, int | None] = {"start_rss_bytes": _rss_bytes()}
    market = _exercise_market_paths(settings, fixture_path)
    gc.collect()
    stages["market_rss_bytes"] = _rss_bytes()

    cache = BoundedLruCache[object](settings.market_data.cache_policy, wall_clock=lambda: NOW)
    pool_fill = _fill_cache_pools(cache, settings.market_data.cache_policy)
    gc.collect()
    stages["cache_rss_bytes"] = _rss_bytes()

    deepseek, deepseek_retained = _retain_maximum_deepseek_batch()
    delivery, delivery_retained = _exercise_delivery_pressure()
    retained = (market.retained, deepseek_retained, delivery_retained)
    gc.collect()
    stages["combined_rss_bytes"] = _rss_bytes()
    peak = _peak_rss_bytes()
    uss = _uss_bytes()
    cache_bytes = _cache_estimated_bytes(cache.status())
    logical_bytes = cache_bytes + market.polars_bytes + market.snapshot_bytes + delivery["logical_bytes"]
    memory_budget = settings.performance_budgets.memory
    near_limit = all(float(item["fill_ratio"]) >= POOL_FILL_RATIO for item in pool_fill.values())
    passed = (
        near_limit
        and market.scalar_hash == market.columnar_hash
        and market.old_epoch != market.new_epoch
        and market.dirty_codes > 0
        and deepseek["batch_size"] == 8
        and delivery["cold_loads"] >= 1
        and delivery["dropped_slow_clients"] >= 1
        and logical_bytes <= memory_budget.cache_logical_bytes
        and peak <= memory_budget.process_peak_rss_bytes
    )
    return {
        "schema_version": "youhua-a4-integrated-memory-v1",
        "passed": passed,
        "identity": {
            "head": _git_head(config_path.parents[2]),
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "workload": {
            "network_calls": 0,
            "p1_preheat_rows": settings.performance_budgets.workload.market_rows,
            "p2_p3_full_rows": settings.performance_budgets.workload.market_rows,
            "candidate_rows": settings.performance_budgets.workload.candidate_rows,
            "old_and_new_epochs": True,
            "scalar_and_columnar": True,
            "deepseek_batch_size": deepseek["batch_size"],
            "p6_resident_days": delivery["resident_days"],
            "p6_cold_prefetch_views": delivery["cold_prefetch_views"],
            "slow_clients_opened": delivery["slow_clients_opened"],
        },
        "market": {
            "scalar_sha256": market.scalar_hash,
            "columnar_sha256": market.columnar_hash,
            "old_epoch": market.old_epoch,
            "new_epoch": market.new_epoch,
            "dirty_codes": market.dirty_codes,
            "polars_estimated_bytes": market.polars_bytes,
            "scalar_columnar_snapshot_bytes": market.snapshot_bytes,
        },
        "cache_pools": pool_fill,
        "deepseek": deepseek,
        "delivery": delivery,
        "memory": {
            "cache_estimated_bytes": cache_bytes,
            "combined_logical_bytes": logical_bytes,
            "logical_budget_bytes": memory_budget.cache_logical_bytes,
            "process_rss_bytes": stages["combined_rss_bytes"],
            "process_peak_rss_bytes": peak,
            "process_peak_rss_budget_bytes": memory_budget.process_peak_rss_bytes,
            "process_uss_bytes": uss,
            "stages": stages,
            "transient_peak_reason": (
                "two 5500-row scalar/columnar epochs, six bounded cache pools at 70% byte limits, "
                "an eight-stock DeepSeek batch, 20 resident P6 dates, cold triplet prefetch, "
                "atomic replacements and undrained slow-client queues coexist in one process"
            ),
            "retained_scope_count": len(retained),
        },
    }


def _exercise_market_paths(settings: Any, fixture_path: Path) -> _MarketArtifacts:
    _manifest, template, _fixture_hash = scalar_runner._load_fixture(fixture_path)
    rows = scalar_runner._expand_rows(settings.performance_budgets.workload.market_rows, template)
    observed_at = datetime.fromisoformat(scalar_runner._text(template, "observed_at"))
    east = scalar_runner._normalize(rows, "eastmoney", observed_at, 1.0)
    sina = scalar_runner._normalize(rows, "sina", observed_at, scalar_runner._number(template, "sina_price_multiplier"))
    observations = (
        *(observation_from_quote(quote, source="eastmoney", observed_at=observed_at) for quote in east),
        *(observation_from_quote(quote, source="sina", observed_at=observed_at) for quote in sina),
    )
    columnar_hook = merge_module.try_merge_complete_realtime
    try:
        merge_module.try_merge_complete_realtime = lambda _observations: None
        scalar = merge_market_observations(observations, observed_at=observed_at)
    finally:
        merge_module.try_merge_complete_realtime = columnar_hook
    columnar = merge_market_observations(observations, observed_at=observed_at)
    if scalar != columnar:
        raise ValueError("fixed scalar and columnar snapshots differ")
    tencent = scalar_runner._normalize(
        rows[: settings.performance_budgets.workload.candidate_rows],
        "tencent",
        observed_at,
        scalar_runner._number(template, "tencent_price_multiplier"),
    )
    targeted = merge_market_observations(
        (*observations, *(observation_from_quote(item, source="tencent", observed_at=observed_at) for item in tencent)),
        observed_at=observed_at,
        targeted_codes=tuple(item.code for item in tencent),
    )
    base_batch = ColumnarQuoteBatch.from_snapshot(
        columnar,
        config_version=settings.config_version,
        schema_version=settings.market_data.cache_policy.policy_version,
    )
    targeted_batch = ColumnarQuoteBatch.from_snapshot(
        targeted,
        config_version=settings.config_version,
        schema_version=settings.market_data.cache_policy.policy_version,
    )
    changes = market_changes(base_batch, targeted_batch)
    return _MarketArtifacts(
        scalar_hash=snapshot_payload_hash(scalar),
        columnar_hash=snapshot_payload_hash(columnar),
        old_epoch=base_batch.identity.merge_epoch,
        new_epoch=targeted_batch.identity.merge_epoch,
        dirty_codes=len(changes.dirty_codes),
        polars_bytes=base_batch.frame.estimated_size() + targeted_batch.frame.estimated_size(),
        snapshot_bytes=len(canonical_json_bytes(scalar)) + len(canonical_json_bytes(columnar)),
        retained=(scalar, columnar, targeted, base_batch, targeted_batch, changes),
    )


def _fill_cache_pools(cache: BoundedLruCache[object], policy: Any) -> dict[str, dict[str, object]]:
    datasets = {
        "p1_observation": ("full_market_quotes", 8),
        "p2_canonical": ("canonical_market_snapshot", 3),
        "p3_features": ("history_summary", 4),
        "p4_local_scoring": ("local_score", 4),
        "p5_review": ("raw_deepseek_review", 4),
        "p6_delivery": ("published_recommendation_view", 4),
    }
    result: dict[str, dict[str, object]] = {}
    for group, (dataset, entries) in datasets.items():
        group_limit = policy.groups[group].max_bytes
        target = int(group_limit * POOL_FILL_RATIO)
        payload_size = max(1, target // entries)
        source = f"a4:{group}"
        for index in range(entries):
            identity = build_cache_identity(
                CacheIdentitySpec(
                    dataset=dataset,
                    source=source,
                    subject_key=f"{group}:{index}",
                    request={"index": index, "pool": group},
                    trade_date=NOW.date().isoformat(),
                    phase="today_main",
                    source_contract_version="a4-memory-v1",
                    config_version="a4-memory-v1",
                    schema_version="a4-memory-v1",
                )
            )
            payload = f"{group}:{index}:" + "x" * payload_size
            if not cache.put(identity, payload, data_version=f"a4:{index}", source_time=NOW):
                raise ValueError(f"cache rejected near-limit {group} fixture")
        measured = int(cache.status()[dataset][source]["estimated_bytes"])
        result[group] = {
            "dataset": dataset,
            "entries": entries,
            "estimated_bytes": measured,
            "limit_bytes": group_limit,
            "fill_ratio": round(measured / group_limit, 6),
        }
    return result


def _retain_maximum_deepseek_batch() -> tuple[dict[str, object], tuple[object, ...]]:
    cache = ReviewCache(maximum_entries=2000, ttl_seconds=600)
    reviews = tuple(
        DeepSeekReview(
            code=f"600{index:03d}",
            outcome=ReviewOutcome.APPLIED,
            dimensions={},
            risk_facts=(),
            completed_at=NOW,
            requested_model="deepseek-v4-flash",
            actual_model="deepseek-v4-flash",
        )
        for index in range(1, 9)
    )
    for review in reviews:
        cache.put_fusion(f"a4:{review.code}", review)
    return (
        {"batch_size": len(reviews), "cache_status": cache.status(), "physical_requests": 0},
        (cache, reviews),
    )


def _exercise_delivery_pressure() -> tuple[dict[str, Any], tuple[object, ...]]:
    archive_snapshots = tuple(
        _snapshot(
            f"archive:{day}:{strategy.value}",
            strategy,
            (NOW - timedelta(days=day)).date().isoformat(),
            frozen=True,
        )
        for day in range(1, 22)
        for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)
    )
    archive = _Archive(archive_snapshots)
    index = PublishedSnapshotIndex(archive, resident_days=20, cold_slots=6)
    initialized = index.initialize()
    cold_date = (NOW - timedelta(days=21)).date().isoformat()
    cold = index.load_frozen(Strategy.TODAY, cold_date)
    if cold is None:
        raise ValueError("P6 cold triplet prefetch failed")
    publisher = SnapshotPublisher(history_size=64, client_queue_size=1, maximum_subscribers=32, now=lambda: NOW)
    base = _snapshot("live:base", Strategy.TODAY, NOW.date().isoformat())
    index.publish(base)
    publisher.publish(base)
    slow = tuple(publisher.open_subscription(publisher.last_sequence()) for _index in range(32))
    for tick in range(12):
        current = _snapshot(f"live:{tick}", Strategy.TODAY, NOW.date().isoformat(), price=12.01 + tick / 1000)
        index.publish(current)
        publisher.publish(current)
    status = index.status()
    publisher_status = publisher.status()
    queued_bytes = sum(
        len(canonical_json_bytes(item.queue.queue[0]))
        for item in slow
        if isinstance(item.queue, queue.Queue) and item.queue.qsize()
    )
    history = publisher.events_after(0) or ()
    history_bytes = sum(len(canonical_json_bytes(item)) for item in history)
    p6_bytes = sum(len(canonical_json_bytes(item)) for item in archive_snapshots) + len(canonical_json_bytes(base))
    return (
        {
            "resident_days": initialized["resident_dates_preloaded"],
            "resident_views": status["resident_views"],
            "cold_loads": status["cold_loads"],
            "cold_prefetch_views": 3,
            "atomic_replacements": 12,
            "slow_clients_opened": len(slow),
            "dropped_slow_clients": publisher_status["dropped_subscribers"],
            "history_events": publisher_status["history_size"],
            "logical_bytes": p6_bytes + queued_bytes + history_bytes,
        },
        (archive, index, publisher, slow),
    )


def _snapshot(
    snapshot_id: str,
    strategy: Strategy,
    trade_date: str,
    *,
    frozen: bool = False,
    price: float = 12.0,
) -> RecommendationSnapshot:
    recommendations = tuple(_recommendation(strategy, rank, price) for rank in range(1, 19))
    return RecommendationSnapshot(
        snapshot_id=snapshot_id,
        strategy=strategy,
        trade_date=trade_date,
        phase="frozen" if frozen else "today_main",
        data_version=f"data:{snapshot_id}",
        strategy_version="strategy-v17",
        fusion_version="fusion-v2",
        fusion_mode=FusionMode.LOCAL_DEGRADED,
        published_at=NOW,
        recommendations=recommendations,
        filtered_count=342,
        filter_reasons={"hard_filter": 342},
        config_version="runtime-v17",
        frozen=frozen,
    )


def _recommendation(strategy: Strategy, rank: int, price: float) -> Recommendation:
    code = f"600{rank:03d}"
    quote = MarketQuote(
        code=code,
        name=f"测试{code}",
        price=price,
        previous_close=11.65,
        open_price=11.8,
        high=12.2,
        low=11.7,
        pct_change=3.0,
        change_5m=1.0,
        speed=0.8,
        volume_ratio=2.0,
        turnover_rate=3.0,
        amount=300_000_000.0,
        amplitude=4.0,
        market_cap=30_000_000_000.0,
        industry=f"行业{rank:02d}",
        source="a4-fixture",
        source_time=NOW,
        received_time=NOW,
        data_version=f"quote:{price}",
    )
    feature = FeatureSnapshot(quote=quote, values={}, observed_at=NOW, history_days=60)
    action = RecommendationAction.EXECUTABLE if rank <= 10 else RecommendationAction.OBSERVE
    return Recommendation(
        strategy=strategy,
        features=feature,
        score=ScoreBreakdown(
            components={},
            base_score=82.0,
            local_risk_penalty=0.0,
            local_score=82.0,
            deepseek_score=None,
            confidence_coverage=0.0,
            deepseek_risk_penalty=0.0,
            final_score=82.0,
            fusion_mode=FusionMode.LOCAL_DEGRADED,
            fusion_applied=False,
        ),
        local_risk_facts=(),
        deepseek_risk_facts=(),
        review=None,
        action=action,
        action_reason="threshold_met" if rank <= 10 else "near_threshold",
        veto=False,
        rank=rank,
    )


def _cache_estimated_bytes(status: Mapping[str, Mapping[str, Mapping[str, object]]]) -> int:
    return sum(int(source["estimated_bytes"]) for dataset in status.values() for source in dataset.values())


def _rss_bytes() -> int:
    statm = Path("/proc/self/statm").read_text(encoding="utf-8").split()
    return int(statm[1]) * os.sysconf("SC_PAGE_SIZE")


def _peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value * 1024) if platform.system() == "Linux" else int(value)


def _uss_bytes() -> int | None:
    try:
        fields = {
            line.partition(":")[0]: int(line.partition(":")[2].strip().split()[0]) * 1024
            for line in Path("/proc/self/smaps_rollup").read_text(encoding="utf-8").splitlines()
            if ":" in line and line.partition(":")[2].strip()
        }
    except (OSError, ValueError):
        return None
    return fields.get("Private_Clean", 0) + fields.get("Private_Dirty", 0)


def _git_head(root: Path) -> str:
    head = root / ".git" / "HEAD"
    raw = head.read_text(encoding="utf-8").strip()
    if raw.startswith("ref: "):
        return (root / ".git" / raw[5:]).read_text(encoding="utf-8").strip()
    return raw


def _atomic_write(path: Path, report: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the fixed youhua A4 integrated memory acceptance suite")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        report = _measure(arguments.config.resolve(), arguments.fixture.resolve())
    except Exception as exc:
        report = {
            "schema_version": "youhua-a4-integrated-memory-v1",
            "passed": False,
            "error": type(exc).__name__,
            "message": str(exc)[:300],
        }
        _atomic_write(arguments.output.resolve(), report)
        return 2
    _atomic_write(arguments.output.resolve(), report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
