from __future__ import annotations
from datetime import datetime
from zoneinfo import ZoneInfo
from trader.infra.market_data.observations import SourceObservation

SHANGHAI = ZoneInfo("Asia/Shanghai")

NOW = datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI)


def _observation(
    source: str,
    *,
    price: float = 10.0,
    observed_at: datetime = NOW,
    source_time: datetime = NOW,
    received_at: datetime = NOW,
    data_version: str | None = None,
    status: str = "success",
) -> SourceObservation:
    return SourceObservation(
        source=source,
        subject_key="600001",
        observed_at=observed_at,
        source_time=source_time,
        received_at=received_at,
        effective_at=source_time,
        data_version=data_version if data_version is not None else f"{source}-source",
        fields={
            "name": "测试股份",
            "price": price,
            "previous_close": 9.8,
            "open_price": 9.9,
            "high": max(price, 10.1),
            "low": 9.7,
            "pct_change": 2.0,
            "amount": 300_000_000.0,
            "industry": "工业",
        },
        missing_reasons={},
        payload_hash=f"{source}-payload",
        status=status,
        error_code=None if status == "success" else "offline",
    )
