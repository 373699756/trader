from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from trader.application.research.preregistered_shadow import PreregisteredShadowGate
from trader.application.research.preregistered_shadow_models import (
    PreregisteredShadowDayRecord,
    PreregisteredShadowPair,
)
from trader.domain.research.tomorrow_shadow_preregistration import (
    TOMORROW_SHADOW_P1_SPEC,
    TomorrowShadowCalendarAttestation,
)
from trader.infra.research.preregistered_shadow_artifacts import (
    PreregisteredShadowArtifactConflictError,
    PreregisteredShadowArtifactStore,
)


def _attestation() -> TomorrowShadowCalendarAttestation:
    spec = TOMORROW_SHADOW_P1_SPEC
    return TomorrowShadowCalendarAttestation(
        research_spec_hash=spec.content_hash,
        confirmed_on=date(2026, 12, 31),
        authority_document_hash="a" * 64,
        trading_dates=(*spec.historical_dates, *spec.forward_dates),
    )


def _record() -> PreregisteredShadowDayRecord:
    spec = TOMORROW_SHADOW_P1_SPEC
    pairs = tuple(
        PreregisteredShadowPair(
            code=f"{600000 + index:06d}",
            board=("main", "chinext", "star")[index % 3],
            baseline_weight=1.0 / 6.0 if index < 6 else 0.0,
            challenger_weight=1.0 / 6.0 if index >= 2 else 0.0,
            hybrid_weight=1.0 / 6.0 if index >= 2 else 0.0,
            gross_excess_return=float(index) / 100.0,
            turnover=0.2,
            mae_atr20=-0.5,
            score=float(index),
            oracle_member=index >= 6,
        )
        for index in range(8)
    )
    return PreregisteredShadowDayRecord(
        research_spec_hash=spec.content_hash,
        calendar_attestation_hash=_attestation().content_hash,
        historical_gate_hash=None,
        challenger_id="residual_reversal_v1",
        phase="historical",
        planned_trade_date=spec.historical_dates[0],
        status="valid",
        feature_batch_hash="b" * 64,
        shadow_report_hash="c" * 64,
        selection_report_hash="d" * 64,
        pairs=pairs,
        failure_reason=None,
    )


def test_preregistered_shadow_store_seals_calendar_and_day_idempotently(tmp_path) -> None:  # noqa: ANN001
    store = PreregisteredShadowArtifactStore(tmp_path, TOMORROW_SHADOW_P1_SPEC)
    attestation = _attestation()
    record = _record()

    assert store.seal_calendar(attestation) == attestation
    assert store.seal_calendar(attestation) == attestation
    assert store.append(record) == record
    assert store.append(record) == record
    assert store.read(record.challenger_id, record.phase, record.planned_trade_date) == record
    collecting = PreregisteredShadowGate(attestation).evaluate((), scope="historical")
    with pytest.raises(ValueError, match="collecting"):
        store.seal_report(collecting)


def test_preregistered_shadow_store_rejects_conflict_and_tampering(tmp_path) -> None:  # noqa: ANN001
    store = PreregisteredShadowArtifactStore(tmp_path, TOMORROW_SHADOW_P1_SPEC)
    store.seal_calendar(_attestation())
    record = store.append(_record())

    with pytest.raises(PreregisteredShadowArtifactConflictError, match="identity conflict"):
        store.append(replace(record, selection_report_hash="e" * 64))

    path = (
        tmp_path
        / TOMORROW_SHADOW_P1_SPEC.research_identity
        / "residual_reversal_v1"
        / "historical"
        / f"{record.planned_trade_date.isoformat()}.json"
    )
    path.write_text(path.read_text(encoding="utf-8").replace("valid", "failed", 1), encoding="utf-8")
    with pytest.raises(PreregisteredShadowArtifactConflictError, match="hash or schema"):
        store.read(record.challenger_id, record.phase, record.planned_trade_date)


def test_preregistered_shadow_store_requires_a_matching_calendar_attestation(tmp_path) -> None:  # noqa: ANN001
    store = PreregisteredShadowArtifactStore(tmp_path, TOMORROW_SHADOW_P1_SPEC)
    record = _record()

    with pytest.raises(ValueError, match="calendar attestation"):
        store.append(record)
    with pytest.raises(ValueError, match="calendar attestation"):
        store.seal_calendar(replace(_attestation(), research_spec_hash="f" * 64))


def test_preregistered_shadow_store_requires_sealed_historical_gate_before_forward(tmp_path) -> None:  # noqa: ANN001
    store = PreregisteredShadowArtifactStore(tmp_path, TOMORROW_SHADOW_P1_SPEC)
    store.seal_calendar(_attestation())
    forward = replace(
        _record(),
        phase="forward",
        planned_trade_date=TOMORROW_SHADOW_P1_SPEC.forward_dates[0],
        historical_gate_hash="e" * 64,
    )

    with pytest.raises(ValueError, match="historical shadow gate report"):
        store.append(forward)
