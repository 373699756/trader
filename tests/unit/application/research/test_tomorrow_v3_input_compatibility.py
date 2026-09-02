from dataclasses import dataclass
from datetime import date

from trader.application.research.tomorrow_v3_input_compatibility import (
    verify_tomorrow_v3_input_port,
)
from trader.domain.research.tomorrow_v3_input_compatibility import (
    REQUIRED_DAILY_FIELDS,
    FrozenDailyInputDescriptor,
)


@dataclass
class _FrozenPort:
    descriptor: FrozenDailyInputDescriptor
    describe_calls: int = 0

    def describe_frozen_daily_input(self) -> FrozenDailyInputDescriptor:
        self.describe_calls += 1
        return self.descriptor


def test_use_case_reads_only_descriptor_and_returns_hash_bound_compatibility() -> None:
    descriptor = FrozenDailyInputDescriptor(
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
    port = _FrozenPort(descriptor)

    report = verify_tomorrow_v3_input_port(port, expected_manifest_hash=descriptor.manifest_hash)

    assert report.status == "compatible"
    assert report.parent_manifest_hash == descriptor.manifest_hash
    assert port.describe_calls == 1
    assert not hasattr(port, "rows")
    assert not hasattr(report, "predictions")
    assert not hasattr(report, "returns")
