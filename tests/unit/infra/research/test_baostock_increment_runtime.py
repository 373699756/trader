from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from trader.application.research.baostock_history_runtime import BaoStockRuntimeRequest, BaoStockRuntimeStatus
from trader.domain.research.baostock_active_archive import (
    BaoStockActiveArchiveContext,
    BaoStockArchiveFieldFamily,
    BaoStockArchiveRecordKey,
)
from trader.infra.research import baostock_increment_runtime as increment_runtime
from trader.infra.research.baostock_active_archive import (
    BaoStockActiveArchive,
    BaoStockIncrementRecord,
)
from trader.infra.research.baostock_archive_plan import (
    ArchiveFieldCoverage,
    ArchiveIncrementPlan,
    ArchiveRequestEstimate,
    FieldFamilyCoverage,
    StockArchivePlan,
)
from trader.infra.research.baostock_increment_runtime import (
    BaoStockIncrementFetch,
    IncrementApplyOptions,
    _BaoStockIncrementSource,
    apply_increment_plan,
    run_baostock_increment_update,
)


class _Rows:
    error_code = "0"
    error_msg = "success"

    def __init__(self, fields: tuple[str, ...], rows: tuple[tuple[str, ...], ...]) -> None:
        self.fields = fields
        self._rows = iter(rows)
        self._current: tuple[str, ...] = ()

    def next(self) -> bool:
        try:
            self._current = next(self._rows)
        except StopIteration:
            return False
        return True

    def get_row_data(self) -> tuple[str, ...]:
        return self._current


class _Sdk:
    __version__ = "fixture"

    def query_history_k_data_plus(self, code: str, fields: str, *_args: object, **kwargs: object) -> _Rows:
        values = {
            "date": "2026-09-01",
            "code": code,
            "open": "10",
            "high": "10",
            "low": "9",
            "close": "9",
            "preclose": "10",
            "volume": "1",
            "amount": "9",
            "adjustflag": str(kwargs["adjustflag"]),
            "turn": "1",
            "tradestatus": "1",
            "pctChg": "-10",
            "isST": "0",
        }
        requested = tuple(fields.split(","))
        return _Rows(requested, (tuple(values[item] for item in requested),))

    def query_stock_industry(self, *, code: str = "", date: str = "") -> _Rows:
        del code, date
        return _Rows(("code",), ())


class _Source:
    def __init__(self, *, fail_qfq: bool = False, unavailable_qfq: bool = False) -> None:
        self.fail_qfq = fail_qfq
        self.unavailable_qfq = unavailable_qfq
        self.calls: list[tuple[str, BaoStockArchiveFieldFamily, tuple[date, ...]]] = []

    def fetch_daily(
        self,
        code: str,
        family: BaoStockArchiveFieldFamily,
        dates: tuple[date, ...],
    ) -> BaoStockIncrementFetch:
        self.calls.append((code, family, dates))
        if family == "daily_qfq" and self.fail_qfq:
            raise RuntimeError("qfq_query_failed")
        if family == "daily_qfq" and self.unavailable_qfq:
            return BaoStockIncrementFetch(
                (),
                tuple(BaoStockArchiveRecordKey(code, day, family) for day in dates),
                "supplier_adjustment_unavailable",
            )
        records = [_record(code, day, family) for day in dates]
        if family == "daily_raw":
            records.extend(_record(code, day, "is_st") for day in dates)
        return BaoStockIncrementFetch(tuple(records))

    def fetch_industry(
        self,
        codes: frozenset[str],
        *,
        as_of: date,
    ) -> tuple[BaoStockIncrementRecord, ...]:
        assert codes == frozenset({"600001"})
        return (_record("600001", as_of, "industry"),)


def _record(code: str, day: date, family: BaoStockArchiveFieldFamily) -> BaoStockIncrementRecord:
    if family in {"daily_raw", "daily_qfq"}:
        payload: dict[str, object] = {
            "adjustment": "unadjusted" if family == "daily_raw" else "qfq",
            "open_price": 1.0,
            "high_price": 1.0,
            "low_price": 1.0,
            "close_price": 1.0,
            "volume": 1.0,
            "amount": 1.0,
            "preclose": 1.0 if family == "daily_raw" else None,
            "pct_change": 0.0 if family == "daily_raw" else None,
            "turnover": 0.0 if family == "daily_raw" else None,
            "trading_status": "trading",
        }
    elif family == "is_st":
        payload = {"is_st": False}
    elif family == "industry":
        payload = {"effective_to": None, "industry": "bank", "classification": "fixture"}
    else:
        payload = {"effective_at": day.isoformat()}
    encoded = json.dumps(
        {"code": code, "trade_date": day.isoformat(), **payload},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return BaoStockIncrementRecord(BaoStockArchiveRecordKey(code, day, family), encoded)


def _plan(parent_file_hash: str) -> ArchiveIncrementPlan:
    missing = FieldFamilyCoverage(0, 1)
    absent = FieldFamilyCoverage(0, 1, "historical_effective_at_source_unavailable")
    return ArchiveIncrementPlan(
        "a" * 64,
        parent_file_hash,
        "b" * 64,
        date(2026, 8, 31),
        date(2026, 9, 1),
        (date(2026, 8, 31), date(2026, 9, 1)),
        1,
        1,
        (
            StockArchivePlan(
                "600001",
                date(2026, 8, 31),
                date(2026, 8, 31),
                2,
                1,
                (date(2026, 9, 1),),
                (date(2026, 9, 1),),
                (date(2026, 9, 1),),
                (date(2026, 9, 1),),
            ),
        ),
        ArchiveFieldCoverage(missing, missing, missing, missing, absent, absent, absent),
        ArchiveRequestEstimate(1, 1, 0, 1, 2),
        None,
        None,
    )


def _archive(root: Path) -> tuple[ArchiveIncrementPlan, BaoStockActiveArchive]:
    parent = root / "manifest.json"
    shard = root / "shards" / "main-6000.sqlite3"
    shard.parent.mkdir()
    with sqlite3.connect(shard) as connection:
        connection.execute("CREATE TABLE daily_cells(code TEXT, trade_date TEXT, payload_json TEXT, content_hash TEXT)")
        connection.execute("CREATE TABLE daily_facts(code TEXT, trade_date TEXT, is_st INTEGER, content_hash TEXT)")
        connection.execute(
            "CREATE TABLE industry_intervals(code TEXT, effective_from TEXT, effective_to TEXT, "
            "industry TEXT, classification TEXT, content_hash TEXT)"
        )
    parent.write_text(
        json.dumps(
            {
                "content_hash": "a" * 64,
                "partitions": [{"relative_path": "shards/main-6000.sqlite3", "codes": ["600001"]}],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    file_hash = hashlib.sha256(parent.read_bytes()).hexdigest()
    plan = _plan(file_hash)
    context = BaoStockActiveArchiveContext(
        plan.parent_manifest_hash,
        file_hash,
        plan.target_source_cutoff,
        "c" * 64,
        "d" * 64,
        (("600001", "main-6000"),),
    )
    return plan, BaoStockActiveArchive(root, context)


def test_increment_plan_requests_raw_qfq_once_and_reuses_raw_is_st(tmp_path: Path) -> None:
    plan, archive = _archive(tmp_path)
    writer = archive.resume_writer()
    source = _Source()

    status = apply_increment_plan(plan, archive, writer, source, IncrementApplyOptions(sessions=2000))

    assert status.state == "completed"
    assert tuple(family for _code, family, _dates in source.calls) == ("daily_raw", "daily_qfq")
    first_active = archive.verify()
    assert first_active.production_authority is False

    repeated_source = _Source()
    repeated = apply_increment_plan(
        plan,
        archive,
        archive.resume_writer(),
        repeated_source,
        IncrementApplyOptions(sessions=2000),
    )
    assert repeated.state == "completed"
    assert repeated_source.calls == []
    assert archive.verify().content_hash == first_active.content_hash


def test_increment_failure_keeps_active_pointer_and_retry_skips_completed_raw(tmp_path: Path) -> None:
    plan, archive = _archive(tmp_path)
    failed_source = _Source(fail_qfq=True)
    first = apply_increment_plan(
        plan,
        archive,
        archive.resume_writer(),
        failed_source,
        IncrementApplyOptions(sessions=2000),
    )

    assert first.state == "completed_with_failures"
    assert not (tmp_path / "active-manifest.json").exists()
    retry_source = _Source()
    second = apply_increment_plan(
        plan,
        archive,
        archive.resume_writer(),
        retry_source,
        IncrementApplyOptions(sessions=2000),
    )

    assert second.state == "completed"
    assert tuple(family for _code, family, _dates in retry_source.calls) == ("daily_qfq",)


def test_increment_supplier_accepts_negative_price_change_and_reuses_raw_is_st() -> None:
    source = _BaoStockIncrementSource(_Sdk())

    result = source.fetch_daily("600001", "daily_raw", (date(2026, 9, 1),))

    assert tuple(item.key.family for item in result.records) == ("daily_raw", "is_st")
    assert '"pct_change":-0.1' in result.records[0].payload_json


def test_deterministic_supplier_adjustment_gap_publishes_degraded_active_manifest(tmp_path: Path) -> None:
    plan, archive = _archive(tmp_path)

    status = apply_increment_plan(
        plan,
        archive,
        archive.resume_writer(),
        _Source(unavailable_qfq=True),
        IncrementApplyOptions(sessions=2000),
    )

    assert status.state == "completed_with_failures"
    assert status.failure_reasons == ("supplier_adjustment_unavailable",)
    assert archive.verify().content_hash == status.manifest_hash


def test_parent_integrity_is_verified_once_before_worker_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "manifest.json").write_text("{}\n", encoding="utf-8")
    verifications = 0
    attempts = 0

    def verify(_archive: object) -> None:
        nonlocal verifications
        verifications += 1

    def fail_attempt(*args: object) -> tuple[None, str, object | None]:
        nonlocal attempts
        attempts += 1
        return None, "baostock_login_failed", args[2]

    monkeypatch.setattr(increment_runtime.BaoStockDailyPartitionedArchive, "verify", verify)
    monkeypatch.setattr(increment_runtime, "_run_increment_attempt", fail_attempt)

    status = run_baostock_increment_update(
        BaoStockRuntimeRequest(runtime_dir=tmp_path, sessions=2000, retries=2, mode="update"),
        tmp_path,
        cancel_requested=lambda: False,
        progress=None,
    )

    assert status.state == "failed"
    assert status.failure_reasons == ("baostock_login_failed",)
    assert verifications == 1
    assert attempts == 3


def test_worker_retry_reuses_the_plan_prepared_by_the_first_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, archive = _archive(tmp_path)
    prepared = increment_runtime._PreparedIncrement(plan, archive.context)
    observed: list[object] = []

    def attempt(*args: object) -> tuple[BaoStockRuntimeStatus | None, str, object | None]:
        current = args[2]
        observed.append(current)
        if current is None:
            return None, "supplier_call_timeout", prepared
        return BaoStockRuntimeStatus(state="completed", sessions=2000), "", current

    monkeypatch.setattr(increment_runtime.BaoStockDailyPartitionedArchive, "verify", lambda _archive: None)
    monkeypatch.setattr(increment_runtime, "_run_increment_attempt", attempt)

    status = run_baostock_increment_update(
        BaoStockRuntimeRequest(runtime_dir=tmp_path, sessions=2000, retries=2, mode="update"),
        tmp_path,
        cancel_requested=lambda: False,
        progress=None,
    )

    assert status.state == "completed"
    assert observed == [None, prepared]
