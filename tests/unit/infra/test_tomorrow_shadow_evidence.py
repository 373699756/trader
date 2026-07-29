from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from trader.application.tomorrow_shadow import (
    TomorrowCutoverPolicy,
    TomorrowShadowObservation,
)
from trader.infra.persistence.tomorrow_shadow_evidence import (
    TomorrowShadowEvidenceRepository,
    TomorrowShadowEvidenceUnavailableError,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 7, 28)


def test_evidence_repository_round_trips_newest_identity_and_builds_verified_report(tmp_path) -> None:
    repository = TomorrowShadowEvidenceRepository(tmp_path, maximum_samples=3)
    repository.initialize()
    morning = _observation("morning", datetime(2026, 7, 28, 9, 40, tzinfo=SHANGHAI))
    frozen = _observation(
        "freeze",
        datetime(2026, 7, 28, 14, 50, tzinfo=SHANGHAI),
        frozen=True,
    )

    repository.record(morning)
    repository.record(frozen)
    repository.record(replace(morning, observed_at=morning.observed_at.replace(minute=41)))

    restored = repository.load_recent()
    assert restored == (replace(morning, observed_at=morning.observed_at.replace(minute=41)), frozen)
    report = repository.build_report(TomorrowCutoverPolicy(minimum_samples=2, minimum_trade_days=1, maximum_samples=3))
    assert report["schema_version"] == "tomorrow_shadow_evidence_v1"
    assert report["observation_count"] == 2
    assert report["config_versions"] == ["runtime:test"]
    assert report["decision_schema_versions"] == ["decision_epoch_v1"]
    assert report["evidence_hash"]
    assert report["cutover_status"]["eligible"] is True


def test_evidence_repository_rejects_tampered_payload(tmp_path) -> None:
    repository = TomorrowShadowEvidenceRepository(tmp_path)
    repository.initialize()
    repository.record(_observation("morning", datetime(2026, 7, 28, 9, 40, tzinfo=SHANGHAI)))
    database = tmp_path / "tomorrow-v2" / "tomorrow-shadow-evidence.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE tomorrow_shadow_evidence SET payload = ?",
            (b'{"tampered":true}',),
        )

    with pytest.raises(TomorrowShadowEvidenceUnavailableError, match="hash"):
        repository.load_recent()


def test_evidence_repository_read_does_not_create_missing_database(tmp_path) -> None:
    repository = TomorrowShadowEvidenceRepository(tmp_path)

    with pytest.raises(TomorrowShadowEvidenceUnavailableError, match="does not exist"):
        repository.load_recent()

    assert not (tmp_path / "tomorrow-v2").exists()


def test_evidence_report_rejects_a_policy_with_a_different_retention_window(tmp_path) -> None:
    repository = TomorrowShadowEvidenceRepository(tmp_path, maximum_samples=3)
    repository.initialize()

    with pytest.raises(ValueError, match="capacities must match"):
        repository.build_report(TomorrowCutoverPolicy(minimum_samples=1, maximum_samples=4))


def _observation(
    identity: str,
    observed_at: datetime,
    *,
    frozen: bool = False,
) -> TomorrowShadowObservation:
    return TomorrowShadowObservation(
        trade_date=TRADE_DATE,
        observed_at=observed_at,
        baseline_snapshot_id=f"legacy:{identity}",
        decision_version=f"decision:{identity}",
        input_version=f"input:{identity}",
        config_version="runtime:test",
        strategy_version="strategy:test",
        fusion_version="fusion:test",
        decision_schema_version="decision_epoch_v1",
        parent_decision_version="",
        selected_codes_match=True,
        filter_reasons_match=True,
        local_publish_seconds=0.8,
        decision_age_seconds=2.0,
        processing_seconds=0.2,
        deepseek_request_delta=0,
        resource_limits_passed=True,
        baseline_frozen=frozen,
        v2_frozen=frozen,
        freeze_codes_match=frozen,
        freeze_content_hash="a" * 64 if frozen else "",
    )
