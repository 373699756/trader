from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from trader.application.research.preregistered_shadow import PreregisteredShadowCollector, PreregisteredShadowGate
from trader.application.research.preregistered_shadow_models import (
    PreregisteredShadowDayRecord,
    PreregisteredShadowPair,
)
from trader.domain.research.tomorrow_shadow_preregistration import (
    TOMORROW_SHADOW_CHALLENGER_FAMILY,
    TOMORROW_SHADOW_P1_SPEC,
    TomorrowShadowCalendarAttestation,
)


def _attestation() -> TomorrowShadowCalendarAttestation:
    spec = TOMORROW_SHADOW_P1_SPEC
    return TomorrowShadowCalendarAttestation(
        research_spec_hash=spec.content_hash,
        confirmed_on=date(2026, 12, 31),
        authority_document_hash="a" * 64,
        trading_dates=(*spec.historical_dates, *spec.forward_dates),
    )


def _record(challenger: str, trade_date: date, phase: str, day_index: int) -> PreregisteredShadowDayRecord:
    pairs = []
    for index in range(8):
        code = f"{100000 + day_index * 10 + index:06d}"
        board = ("main", "chinext", "star")[index % 3]
        baseline_weight = 1.0 / 6.0 if index < 6 else 0.0
        challenger_weight = 1.0 / 6.0 if index >= 2 else 0.0
        gross = -0.02 if index < 2 else (0.01 if index < 6 else 0.04)
        pairs.append(
            PreregisteredShadowPair(
                code=code,
                board=board,
                baseline_weight=baseline_weight,
                challenger_weight=challenger_weight,
                hybrid_weight=challenger_weight,
                gross_excess_return=gross,
                turnover=0.20,
                mae_atr20=-2.0 if index < 2 else -0.25,
                score=float(index),
                oracle_member=index >= 6,
            )
        )
    return PreregisteredShadowDayRecord(
        research_spec_hash=TOMORROW_SHADOW_P1_SPEC.content_hash,
        calendar_attestation_hash=_attestation().content_hash,
        historical_gate_hash=None if phase == "historical" else "e" * 64,
        challenger_id=challenger,
        phase=phase,
        planned_trade_date=trade_date,
        status="valid",
        feature_batch_hash="b" * 64,
        shadow_report_hash="c" * 64,
        selection_report_hash="d" * 64,
        pairs=tuple(pairs),
        failure_reason=None,
    )


def _records(phase: str) -> tuple[PreregisteredShadowDayRecord, ...]:
    dates = TOMORROW_SHADOW_P1_SPEC.historical_dates if phase == "historical" else TOMORROW_SHADOW_P1_SPEC.forward_dates
    return tuple(
        _record(challenger, trade_date, phase, day_index)
        for challenger in TOMORROW_SHADOW_CHALLENGER_FAMILY
        for day_index, trade_date in enumerate(dates)
    )


class _MemoryStore:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str, date], PreregisteredShadowDayRecord] = {}

    def read(self, challenger_id: str, phase: str, trade_date: date) -> PreregisteredShadowDayRecord | None:
        return self.records.get((challenger_id, phase, trade_date))

    def append(self, record: PreregisteredShadowDayRecord) -> PreregisteredShadowDayRecord:
        self.records[(record.challenger_id, record.phase, record.planned_trade_date)] = record
        return record


def test_historical_gate_keeps_the_fixed_holm_family_and_passes_complete_evidence() -> None:
    report = PreregisteredShadowGate(_attestation()).evaluate(_records("historical"), scope="historical")

    assert report.state == "historical_passed"
    assert tuple(item.challenger_id for item in report.variants) == TOMORROW_SHADOW_CHALLENGER_FAMILY
    assert len(report.holm) == 5
    assert {item.challenger_id for item in report.holm} == set(TOMORROW_SHADOW_CHALLENGER_FAMILY)
    assert all(item.state == "passed" for item in report.variants)
    assert all(item.pair_count == 320 for item in report.variants)
    assert all(item.mean_increment_20bp >= 0.002 for item in report.variants)
    assert all(
        tuple(item.cost_rate for item in variant.cost_sensitivities) == (0.002, 0.005, 0.01)
        for variant in report.variants
    )
    assert all(
        tuple(result.block_days for result in sensitivity.bootstrap) == (3, 5, 10)
        for variant in report.variants
        for sensitivity in variant.cost_sensitivities
    )
    assert all(item.oracle_recall == 1.0 for item in report.variants)
    assert report.calendar_attestation_hash == _attestation().content_hash
    assert len(report.evidence_manifest_hash) == 64
    assert report.production_authority is False


def test_missing_day_stays_collecting_without_shrinking_the_holm_family() -> None:
    records = _records("historical")[:-1]

    report = PreregisteredShadowGate(_attestation()).evaluate(records, scope="historical")

    assert report.state == "collecting"
    assert len(report.variants) == len(report.holm) == 5
    missing = next(item for item in report.variants if item.challenger_id == TOMORROW_SHADOW_CHALLENGER_FAMILY[-1])
    assert missing.state == "collecting"
    assert missing.failure_reasons == ("planned_dates_incomplete",)


def test_forward_and_combined_gates_enforce_the_100_and_300_pair_floors() -> None:
    gate = PreregisteredShadowGate(_attestation())
    historical = gate.evaluate(_records("historical"), scope="historical")
    forward_records = tuple(replace(item, historical_gate_hash=historical.content_hash) for item in _records("forward"))
    forward = gate.evaluate(forward_records, scope="forward", historical_report=historical)
    combined = gate.evaluate(
        (*_records("historical"), *forward_records),
        scope="combined",
        historical_report=historical,
    )

    assert forward.state == "forward_passed"
    assert all(item.pair_count == 160 for item in forward.variants)
    assert combined.state == "promotion_eligible"
    assert all(item.pair_count == 480 for item in combined.variants)
    assert all(item.production_scope == "local_only" for item in combined.variants)


def test_forward_collector_requires_calendar_identity_and_historical_pass() -> None:
    record = _record(
        TOMORROW_SHADOW_CHALLENGER_FAMILY[0],
        TOMORROW_SHADOW_P1_SPEC.forward_dates[0],
        "forward",
        0,
    )
    store = _MemoryStore()

    with pytest.raises(ValueError, match="historical gate report"):
        PreregisteredShadowCollector(_attestation(), store).append(record)

    historical = PreregisteredShadowGate(_attestation()).evaluate(_records("historical"), scope="historical")
    mismatched_calendar = replace(historical, calendar_attestation_hash="f" * 64)
    with pytest.raises(ValueError, match="calendar attestation"):
        PreregisteredShadowCollector(_attestation(), store, mismatched_calendar)
    collecting = PreregisteredShadowGate(_attestation()).evaluate(_records("historical")[:-1], scope="historical")
    with pytest.raises(ValueError, match="completed historical gate"):
        PreregisteredShadowCollector(_attestation(), store, collecting)
    collector = PreregisteredShadowCollector(_attestation(), store, historical)
    bound = replace(record, historical_gate_hash=historical.content_hash)
    assert collector.append(bound) == bound


def test_gate_binds_the_calendar_and_exact_day_evidence_even_when_metrics_are_unchanged() -> None:
    records = _records("historical")
    gate = PreregisteredShadowGate(_attestation())

    original = gate.evaluate(records, scope="historical")
    changed = replace(records[0], selection_report_hash="f" * 64)
    revised = gate.evaluate((changed, *records[1:]), scope="historical")

    assert revised.evidence_manifest_hash != original.evidence_manifest_hash
    assert revised.content_hash != original.content_hash
    with pytest.raises(ValueError, match="calendar attestation"):
        gate.evaluate((replace(records[0], calendar_attestation_hash="f" * 64), *records[1:]), scope="historical")
    with pytest.raises(ValueError, match="scope"):
        gate.evaluate(records, scope="unknown")  # type: ignore[arg-type]


def test_day_record_rejects_runtime_phase_and_status_values_outside_the_typed_contract() -> None:
    record = _records("historical")[0]

    with pytest.raises(ValueError, match="phase or status"):
        replace(record, phase="unknown")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="phase or status"):
        replace(record, status="unknown")  # type: ignore[arg-type]


def test_legal_all_no_decision_evidence_is_rejected_by_metrics_instead_of_raising() -> None:
    records = tuple(
        replace(
            record,
            status="no_decision",
            pairs=tuple(
                replace(pair, baseline_weight=0.0, challenger_weight=0.0, hybrid_weight=0.0) for pair in record.pairs
            ),
        )
        for record in _records("historical")
    )

    report = PreregisteredShadowGate(_attestation()).evaluate(records, scope="historical")

    assert report.state == "rejected"
    assert all(item.state == "rejected" for item in report.variants)
    assert all("mean_increment_floor" in item.failure_reasons for item in report.variants)
