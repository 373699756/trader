"""Offline download persistence measurements on disposable, synthetic databases."""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from trader.download.domain.company_evidence import Disclosure, DisclosurePage
from trader.download.infra.company_evidence_codec import DecodeCompanySnapshot
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex, SQLiteCompanyEvidenceReader
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_download_index import HistoryDownloadIndex
from trader.infra.market_data.history.history import BuildHistoryContext, DailyBar, PriceAdjustment
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.recommendation.infra.market_data.published_company_research import PublishedCompanyResearch

from .reporting import emit_report


def _measure(root: Path) -> dict[str, float | int]:
    usage_before = resource.getrusage(resource.RUSAGE_SELF)
    now = datetime(2026, 10, 18, 9, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    codes = tuple(str(600000 + offset) for offset in range(4402))
    index = SQLiteCompanyEvidenceIndex(root / "company.sqlite3")
    index.initialize()
    partition = index.partitions(("sse",), now)[0]
    rows = tuple(
        Disclosure("fixture", f"{code}:{offset}", (code,), "普通公告", now, "", str(offset))
        for code in codes
        for offset in range(3)
    )
    started = time.monotonic()
    index.commit_page(partition, DisclosurePage(rows, len(rows), False), now)
    write = time.monotonic() - started
    started = time.monotonic()
    index.publish(codes, now)
    publish = time.monotonic() - started
    reader = SQLiteCompanyEvidenceReader(index.path)
    started = time.monotonic()
    cold = reader.read(codes)
    cold_read = time.monotonic() - started
    started = time.monotonic()
    assert reader.read(codes) == cold
    warm_read = time.monotonic() - started
    started = time.monotonic()
    index.publish(codes, now + timedelta(minutes=1))
    repeat_publish = time.monotonic() - started
    assert reader.read(codes) == cold
    with SqliteSession(index.path, SqliteConnectionOptions(read_only=True)) as connection:
        payloads = tuple(row[0] for row in connection.run("SELECT payload FROM published ORDER BY code"))
    started = time.monotonic()
    for payload in payloads:
        DecodeCompanySnapshot(payload)
    decode = time.monotonic() - started
    del cold, payloads
    research = PublishedCompanyResearch(
        reader,
        wall_clock=lambda: now,
        open_dates=lambda: tuple((now + timedelta(days=offset)).date() for offset in range(-5, 5)),
        monotonic=time.monotonic,
    )
    started = time.monotonic()
    research.initialize(codes, cancelled=lambda: False)
    preload = time.monotonic() - started
    assert research.status().entries == len(codes)
    started = time.monotonic()
    assert len(research.load(codes, now, include_structured=True)) == len(codes)
    memory_read = time.monotonic() - started
    bars = tuple(
        DailyBar(
            (now.date() - timedelta(days=251 - offset)).isoformat(),
            10,
            10 + offset / 1000,
            11,
            9,
            1000,
            10000,
            0.1,
            adjustment=PriceAdjustment.QFQ,
            source="fixture",
        )
        for offset in range(251)
    )
    started = time.monotonic()
    for _ in codes:
        BuildHistoryContext(bars, lookback_sessions=251)
    derive = time.monotonic() - started
    gaps = HistoryDownloadIndex(root / "catalog.sqlite3")
    HistoryControlIndex(root / "catalog.sqlite3").initialize()
    with SqliteSession(root / "catalog.sqlite3") as connection, SqliteTransaction(connection):
        connection.run_many("INSERT INTO download_gaps VALUES(?,?)", ((code, "2026-10-16") for code in codes))
    started = time.monotonic()
    # Reproduce the retired per-stock connection/query workload, only on this fixture.
    all_gaps = 0
    for code in codes:
        with SqliteSession(root / "catalog.sqlite3") as connection:
            all_gaps += len(
                connection.run(
                    "SELECT gap.trade_date,unusable.checked_cutoff FROM download_gaps gap "
                    "LEFT JOIN download_qfq_unusable unusable ON unusable.code=gap.code AND unusable.trade_date=gap.trade_date "
                    "WHERE gap.code=? ORDER BY gap.trade_date",
                    (code,),
                ).fetchall()
            )
    individual = time.monotonic() - started
    started = time.monotonic()
    batched = gaps.gaps_by_code((now.date(),))
    batch = time.monotonic() - started
    assert all_gaps == sum(len(days) for days in batched.values()) == len(codes)
    return {
        "company_write_seconds": write,
        "company_publish_seconds": publish,
        "company_read_cold_seconds": cold_read,
        "company_read_warm_seconds": warm_read,
        "company_reuse_publish_seconds": repeat_publish,
        "company_decode_seconds": decode,
        "company_startup_preload_seconds": preload,
        "company_memory_read_seconds": memory_read,
        "history_derive_seconds": derive,
        "gap_individual_seconds": individual,
        "gap_batch_seconds": batch,
        "stocks": len(codes),
        "physical_http_requests": 0,
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        * (1 if sys.platform == "darwin" else 1024),
        "major_page_faults": resource.getrusage(resource.RUSAGE_SELF).ru_majflt - usage_before.ru_majflt,
    }


def _compare(baseline: Path) -> dict[str, object]:
    current = Path(__file__).resolve().parents[2]
    baseline = baseline.resolve(strict=True)
    if baseline == current or current in baseline.parents or not (baseline / "src/trader").is_dir():
        raise ValueError("baseline must be an isolated checkout outside the active repository")
    runs: dict[str, list[dict[str, float]]] = {"baseline": [], "candidate": []}
    for number in range(5):
        order = ("baseline", "candidate") if number % 2 == 0 else ("candidate", "baseline")
        for label in order:
            checkout = baseline if label == "baseline" else current
            completed = subprocess.run(
                [sys.executable, "-m", "scripts.runtime_diagnostics.download_performance", "--rounds", "1"],
                cwd=current,
                env={**os.environ, "PYTHONPATH": str(checkout / "src"), "PYTHONHASHSEED": "0"},
                capture_output=True,
                text=True,
                timeout=120,
                check=True,
            )
            runs[label].append(json.loads(completed.stdout)["summary"]["medians"])
    comparison = [
        {
            "metric": key,
            "baseline_median": median(run[key] for run in runs["baseline"]),
            "candidate_median": median(run[key] for run in runs["candidate"]),
            "median_paired_ratio": median(
                after[key] / before[key] for before, after in zip(runs["baseline"], runs["candidate"], strict=True)
            )
            if all(run[key] for run in runs["baseline"])
            else None,
        }
        for key in runs["baseline"][0]
    ]
    return {
        "schema_version": "download-performance",
        "status": "passed",
        "summary": {
            "fixture": True,
            "rounds": 5,
            "comparison": comparison,
            "runs": runs,
            "cache_policy": "fresh process and synthetic database; warm operating-system cache; alternating order",
            "acceptance": "measurement only; production latency and RSS gates require the production workload",
        },
        "findings": [],
    }


def Main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-checkout", type=Path)
    parser.add_argument("--rounds", type=int, choices=(1, 3), default=3)
    args = parser.parse_args()
    if args.baseline_checkout is not None:
        emit_report(_compare(args.baseline_checkout))
        return 0
    rounds = []
    for _ in range(args.rounds):
        with TemporaryDirectory(prefix="trader-download-perf-") as directory:
            rounds.append(_measure(Path(directory)))
    summary = {key: round(median(float(result[key]) for result in rounds), 6) for key in rounds[0]}
    emit_report(
        {
            "schema_version": "download-performance",
            "status": "passed",
            "summary": {"fixture": True, "rounds": args.rounds, "medians": summary},
            "findings": [],
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(Main())
