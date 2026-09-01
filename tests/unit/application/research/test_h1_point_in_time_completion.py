from datetime import date

from trader.application.research.h1_point_in_time_completion import complete_codex_a_research
from trader.domain.research.h1_point_in_time import H1CapabilityProbe, H1PointInTimeSpec, build_h1_capability_audit
from trader.infra.research.h1_point_in_time_archive import SQLiteH1PointInTimeArchive


def _insufficient_capability():
    return build_h1_capability_audit(
        (
            H1CapabilityProbe(
                "tencent_qfq_daily",
                date(2023, 1, 10),
                False,
                False,
                "qfq",
                False,
                640,
                3,
                1024,
                0.5,
            ),
            H1CapabilityProbe(
                "eastmoney_historical_minute",
                None,
                False,
                False,
                "unsupported",
                False,
                0,
                3,
                512,
                0.5,
            ),
        ),
        probe_failures=("eastmoney_historical_minute_probe_failed",),
    )


def test_codex_a_completion_seals_all_downstream_insufficient_states_without_fake_rows(tmp_path) -> None:
    archive = SQLiteH1PointInTimeArchive(tmp_path)

    completion = complete_codex_a_research(
        capability=_insufficient_capability(),
        metadata=tuple(archive.label_metadata(H1PointInTimeSpec(item)) for item in ("today", "tomorrow", "d25")),
    )

    assert completion.status == "historical_data_insufficient"
    assert {item.status for item in completion.labels.strategies} == {"historical_data_insufficient"}
    assert {item.status for item in completion.residual_ledgers} == {"historical_data_insufficient"}
    assert completion.c3.status == "historical_data_insufficient"
    assert completion.c3.oof_artifact_hash is None
    assert completion.c3.candidate_model_artifact_hash is None
    assert "eastmoney_historical_minute_probe_failed" in completion.c3.failure_reasons
    assert completion.terminal_holdout_opened is False
    assert completion.production_authority is False


def test_codex_a_completion_projects_a_terminal_development_handoff_with_parent_hashes(tmp_path) -> None:
    archive = SQLiteH1PointInTimeArchive(tmp_path)
    completion = complete_codex_a_research(
        capability=_insufficient_capability(),
        metadata=tuple(archive.label_metadata(H1PointInTimeSpec(item)) for item in ("today", "tomorrow", "d25")),
    )

    handoff = completion.to_development_handoff(
        parent_graph_hash="f" * 64,
        resource_probe_artifact_hash="e" * 64,
    )

    assert handoff.stage == "development_training"
    assert handoff.outcome == "historical_data_insufficient"
    assert tuple(item.artifact_id for item in handoff.artifacts) == ("h1_coverage_audit",)
    assert handoff.artifacts[0].owner == "codex_a"
    assert handoff.parent_graph_hash == "f" * 64
    assert handoff.artifacts[0].parent_hashes == ("e" * 64,)
    assert handoff.artifacts[0].content_hash == completion.content_hash
    assert handoff.evidence_partitions == ()
    assert handoff.production_authority is False
