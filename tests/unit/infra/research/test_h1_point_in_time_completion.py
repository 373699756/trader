from datetime import date

import pytest

from trader.training.application.h1_point_in_time_completion import CompleteH1Research
from trader.training.domain.evaluation.h1_point_in_time import (
    BuildH1CapabilityAudit,
    H1CapabilityProbe,
    H1PointInTimeSpec,
)
from trader.training.infra.research.h1_point_in_time_completion import (
    H1ResearchCompletionEvidenceConflictError,
    H1ResearchCompletionEvidenceFiles,
)
from trader.training.infra.research.h1_point_in_time_snapshot import SQLiteH1PointInTimeSnapshot


def _completion(tmp_path):
    capability = BuildH1CapabilityAudit(
        (
            H1CapabilityProbe("tencent_qfq_daily", date(2023, 1, 10), False, "qfq", False, 640, 12500, 1000, 0.1),
            H1CapabilityProbe("eastmoney_historical_minute", None, False, "unsupported", False, 0, 8000000, 500, 0.1),
        )
    )
    archive = SQLiteH1PointInTimeSnapshot(tmp_path / "archive")
    return CompleteH1Research(
        capability=capability,
        metadata=tuple(archive.label_metadata(H1PointInTimeSpec(item)) for item in ("tomorrow", "d25")),
    )


def test_completion_index_seals_every_terminal_hash_and_rejects_tampering(tmp_path) -> None:
    completion = _completion(tmp_path)
    archive = H1ResearchCompletionEvidenceFiles(tmp_path / "artifacts")

    index = archive.write(completion)

    assert index.completion_hash == completion.content_hash
    assert index.label_batch_hash == completion.labels.content_hash
    assert index.residual_terminal_hashes == tuple(
        (item.strategy, item.content_hash) for item in completion.residual_ledgers
    )
    assert index.daily_close_selection_hash == completion.daily_close_selection.content_hash
    assert archive.write(completion) == index
    path = tmp_path / "artifacts" / "h1_research_terminal.json"
    path.write_text(path.read_text().replace(completion.daily_close_selection.content_hash, "0" * 64), encoding="utf-8")
    with pytest.raises(H1ResearchCompletionEvidenceConflictError, match="schema or hash"):
        archive.verify()
