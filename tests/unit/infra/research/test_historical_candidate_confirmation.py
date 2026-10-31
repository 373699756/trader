
from tests.helpers.configuration import SqlitePolicyFixture
import json
from datetime import date

import pytest

from trader.training.application.h1_point_in_time_completion import CompleteH1Research
from trader.training.application.historical_candidate_confirmation import (
    SealHistoricalConfirmationTerminalBatch,
)
from trader.training.domain.evaluation.h1_point_in_time import (
    BuildH1CapabilityAudit,
    H1CapabilityProbe,
    H1PointInTimeSpec,
)
from trader.training.infra.research.h1_point_in_time_snapshot import SQLiteH1PointInTimeSnapshot
from trader.training.infra.research.historical_candidate_confirmation import (
    HistoricalConfirmationReportConflictError,
    HistoricalConfirmationReportPublication,
)


def _batch(tmp_path, *, failure: str = "eastmoney_historical_minute_probe_failed"):
    capability = BuildH1CapabilityAudit(
        (
            H1CapabilityProbe("tencent_qfq_daily", date(2023, 1, 10), False, "qfq", False, 640, 3, 1024, 0.5),
            H1CapabilityProbe("eastmoney_historical_minute", None, False, "unsupported", False, 0, 3, 512, 0.5),
        ),
        probe_failures=(failure,),
    )
    archive = SQLiteH1PointInTimeSnapshot(tmp_path / "archive", sqlite_policy=SqlitePolicyFixture())
    completion = CompleteH1Research(
        capability=capability,
        metadata=tuple(archive.label_metadata(H1PointInTimeSpec(item)) for item in ("tomorrow", "d25")),
    )
    return SealHistoricalConfirmationTerminalBatch(completion)


def test_historical_confirmation_archive_is_idempotent_and_hash_bound(tmp_path) -> None:
    batch = _batch(tmp_path)
    archive = HistoricalConfirmationReportPublication(tmp_path / "artifacts")

    index = archive.write(batch)

    assert index.completion_hash == batch.parent_completion_hash
    assert index.strategy_terminal_hashes == tuple((item.strategy, item.content_hash) for item in batch.strategies)
    assert batch.schema_version == "historical_confirmation_terminal_batch"
    assert {item.schema_version for item in batch.strategies} == {"historical_confirmation_strategy_terminal"}
    assert archive.write(batch) == index

    path = tmp_path / "artifacts" / "historical_confirmation_terminal.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["daily_close_selection_hash"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(HistoricalConfirmationReportConflictError, match="schema or hash"):
        archive.verify()


def test_historical_confirmation_archive_rejects_different_batch(tmp_path) -> None:
    first = _batch(tmp_path / "first")
    second = _batch(tmp_path / "second", failure="historical_minute_probe_failed_again")
    archive = HistoricalConfirmationReportPublication(tmp_path / "artifacts")
    archive.write(first)

    with pytest.raises(HistoricalConfirmationReportConflictError, match="identity conflict"):
        archive.write(second)
