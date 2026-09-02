from dataclasses import replace
from datetime import date

from trader.domain.research.tomorrow_v3_input_compatibility import (
    REQUIRED_DAILY_FIELDS,
    TOMORROW_V3_ALPHA_NAMES,
    TOMORROW_V3_ALPHA_UNITS,
    FrozenDailyInputDescriptor,
    evaluate_tomorrow_v3_input_compatibility,
)


def _descriptor() -> FrozenDailyInputDescriptor:
    return FrozenDailyInputDescriptor(
        manifest_hash="a" * 64,
        source_identity="score_baostock_daily_core_v2",
        source_cutoff=date(2026, 8, 31),
        requested_sessions=2000,
        primary_key=("code", "trade_date"),
        fields=REQUIRED_DAILY_FIELDS,
        raw_qfq_layout="same_row",
        row_hash_algorithm="sha256",
        frozen=True,
    )


def test_compatible_frozen_daily_input_binds_six_alpha_contract_and_parent_hash() -> None:
    descriptor = _descriptor()

    report = evaluate_tomorrow_v3_input_compatibility(
        descriptor,
        expected_manifest_hash=descriptor.manifest_hash,
    )

    assert report.status == "compatible"
    assert report.parent_manifest_hash == descriptor.manifest_hash
    assert report.input_descriptor_hash == descriptor.content_hash
    assert report.feature_names == TOMORROW_V3_ALPHA_NAMES
    assert report.feature_units == TOMORROW_V3_ALPHA_UNITS
    assert report.failure_reasons == ()
    assert report.training_started is False
    assert report.terminal_holdout_opened is False
    assert report.production_authority is False
    assert report.automatic_model_update is False


def test_incompatible_input_reports_all_contract_failures_without_training() -> None:
    fields = tuple(
        replace(field, unit="percent") if field.name == "qfq_close" else field
        for field in REQUIRED_DAILY_FIELDS
        if field.name != "qfq_amount"
    )
    descriptor = replace(
        _descriptor(),
        source_identity="score_baostock_daily_core_v1",
        source_cutoff=date(2026, 9, 1),
        requested_sessions=1500,
        primary_key=("trade_date", "code"),
        fields=fields,
        raw_qfq_layout="separate_rows",
        row_hash_algorithm="md5",
        frozen=False,
        production_authority=True,
    )

    report = evaluate_tomorrow_v3_input_compatibility(
        descriptor,
        expected_manifest_hash="b" * 64,
    )

    assert report.status == "incompatible"
    assert report.parent_manifest_hash == "b" * 64
    assert report.input_manifest_hash == "a" * 64
    assert set(report.failure_reasons) == {
        "field_qfq_amount_missing",
        "field_qfq_close_unit_invalid",
        "input_not_frozen",
        "manifest_hash_mismatch",
        "primary_key_invalid",
        "production_authority_forbidden",
        "raw_qfq_layout_invalid",
        "requested_sessions_invalid",
        "row_hash_algorithm_invalid",
        "source_cutoff_invalid",
        "source_identity_invalid",
    }
    assert report.training_started is False
    assert report.terminal_holdout_opened is False
    assert report.production_authority is False
    assert report.automatic_model_update is False


def test_extra_source_fields_do_not_break_the_consumer_contract() -> None:
    descriptor = replace(
        _descriptor(),
        fields=(*REQUIRED_DAILY_FIELDS, replace(REQUIRED_DAILY_FIELDS[0], name="supplier_extension")),
    )

    report = evaluate_tomorrow_v3_input_compatibility(
        descriptor,
        expected_manifest_hash=descriptor.manifest_hash,
    )

    assert report.status == "compatible"
