"""Strict settings boundary for the isolated historical quote lane."""

import json
from pathlib import Path

from trader.recommendation.application.history_quotes import HistoryQuoteSettings


def LoadHistoryQuoteSettings(path: Path) -> HistoryQuoteSettings:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    keys = {"interval_seconds", "max_age_seconds", "timeout_seconds", "lease_seconds", "maximum_viewers"}
    if not isinstance(raw, dict) or set(raw) != keys:
        raise ValueError("history quote configuration must define exactly the current settings")
    values: dict[str, float] = {}
    for key in keys - {"maximum_viewers"}:
        value: object = raw[key]
        if type(value) not in (int, float):
            raise ValueError(f"history quote {key} must be numeric")
        assert isinstance(value, (int, float))
        values[key] = float(value)
    viewers: object = raw["maximum_viewers"]
    if type(viewers) is not int:
        raise ValueError("history quote maximum_viewers must be an integer")
    assert isinstance(viewers, int)
    return HistoryQuoteSettings(values["interval_seconds"], values["max_age_seconds"],
                                values["timeout_seconds"], values["lease_seconds"], viewers)
