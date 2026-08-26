from __future__ import annotations

from argparse import Namespace

import pytest

from scripts.runtime_diagnostics.tushare_daily import _validate


def _args(**overrides: object) -> Namespace:
    values: dict[str, object] = {
        "codes": ["000001"],
        "days": 61,
    }
    values.update(overrides)
    return Namespace(**values)


def test_tushare_probe_accepts_a_bounded_unique_code_set() -> None:
    assert _validate(_args(codes=["600519", "000001", "600519"])) == ("600519", "000001")


@pytest.mark.parametrize("codes", [[], ["60051"], ["60051x"], [f"{index:06d}" for index in range(51)]])
def test_tushare_probe_rejects_invalid_or_over_minute_limit_codes(codes: list[str]) -> None:
    with pytest.raises(ValueError, match="six-digit|at most 50"):
        _validate(_args(codes=codes))


def test_tushare_probe_rejects_non_positive_days() -> None:
    with pytest.raises(ValueError, match="--days must be positive"):
        _validate(_args(days=0))
