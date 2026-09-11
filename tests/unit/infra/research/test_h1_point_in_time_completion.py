from datetime import date

import pytest

from trader.application.research.h1_point_in_time_completion import complete_h1_research
from trader.domain.research.h1_point_in_time import H1CapabilityProbe, H1PointInTimeSpec, build_h1_capability_audit
from trader.infra.research.h1_point_in_time_archive import SQLiteH1PointInTimeArchive
from trader.infra.research.h1_point_in_time_completion import (
    H1ResearchCompletionArtifactConflictError,
    H1ResearchCompletionArtifactStore,
)


def _completion(tmp_path):
    capability = build_h1_capability_audit(
        (
            H1CapabilityProbe(
                "tencent_qfq_daily", date(2023, 1, 10), False, False, "qfq", False, 640, 12500, 1000, 0.1
            ),
            H1CapabilityProbe(
                "eastmoney_historical_minute", None, False, False, "unsupported", False, 0, 8000000, 500, 0.1
            ),
        )
    )
    archive = SQLiteH1PointInTimeArchive(tmp_path / "archive")
    return complete_h1_research(
        capability=capability,
        metadata=tuple(archive.label_metadata(H1PointInTimeSpec(item)) for item in ("today", "tomorrow", "d25")),
    )


def test_completion_index_seals_every_terminal_hash_and_rejects_tampering(tmp_path) -> None:
    completion = _completion(tmp_path)
    store = H1ResearchCompletionArtifactStore(tmp_path / "artifacts")

    index = store.write(completion)

    assert index.completion_hash == completion.content_hash
    assert index.label_batch_hash == completion.labels.content_hash
    assert index.residual_terminal_hashes == tuple(
        (item.strategy, item.content_hash) for item in completion.residual_ledgers
    )
    assert index.c3_terminal_hash == completion.c3.content_hash
    assert store.write(completion) == index
    path = tmp_path / "artifacts" / "h1_research_terminal.json"
    path.write_text(path.read_text().replace(completion.c3.content_hash, "0" * 64), encoding="utf-8")
    with pytest.raises(H1ResearchCompletionArtifactConflictError, match="schema or hash"):
        store.verify()
