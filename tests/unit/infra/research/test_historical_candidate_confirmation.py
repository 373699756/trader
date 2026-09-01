import json
from datetime import date

import pytest

from trader.application.research.h1_point_in_time_completion import complete_codex_a_research
from trader.application.research.historical_candidate_confirmation import seal_codex_b_insufficient_batch
from trader.domain.research.h1_point_in_time import H1CapabilityProbe, H1PointInTimeSpec, build_h1_capability_audit
from trader.infra.research.h1_point_in_time_archive import SQLiteH1PointInTimeArchive
from trader.infra.research.historical_candidate_confirmation import (
    CodexBTerminalArtifactConflictError,
    CodexBTerminalArtifactStore,
)


def _batch(tmp_path, *, failure: str = "eastmoney_historical_minute_probe_failed"):
    capability = build_h1_capability_audit(
        (
            H1CapabilityProbe("tencent_qfq_daily", date(2023, 1, 10), False, False, "qfq", False, 640, 3, 1024, 0.5),
            H1CapabilityProbe("eastmoney_historical_minute", None, False, False, "unsupported", False, 0, 3, 512, 0.5),
        ),
        probe_failures=(failure,),
    )
    archive = SQLiteH1PointInTimeArchive(tmp_path / "archive")
    completion = complete_codex_a_research(
        capability=capability,
        metadata=tuple(archive.label_metadata(H1PointInTimeSpec(item)) for item in ("today", "tomorrow", "d25")),
    )
    return seal_codex_b_insufficient_batch(completion)


def test_codex_b_terminal_store_is_idempotent_and_hash_bound(tmp_path) -> None:
    batch = _batch(tmp_path)
    store = CodexBTerminalArtifactStore(tmp_path / "artifacts")

    index = store.write(batch)

    assert index.completion_hash == batch.parent_completion_hash
    assert index.strategy_terminal_hashes == tuple((item.strategy, item.content_hash) for item in batch.strategies)
    assert index.joint_report_hash == batch.joint_report_hash
    assert store.write(batch) == index

    path = tmp_path / "artifacts" / "codex_b_insufficient_terminal.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["joint_report_hash"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CodexBTerminalArtifactConflictError, match="schema or hash"):
        store.verify()


def test_codex_b_terminal_store_rejects_different_batch(tmp_path) -> None:
    first = _batch(tmp_path / "first")
    second = _batch(tmp_path / "second", failure="historical_minute_probe_failed_again")
    store = CodexBTerminalArtifactStore(tmp_path / "artifacts")
    store.write(first)

    with pytest.raises(CodexBTerminalArtifactConflictError, match="identity conflict"):
        store.write(second)
