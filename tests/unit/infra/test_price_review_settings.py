from __future__ import annotations

import json
from pathlib import Path

import pytest

from trader.download.infra.download_settings import LoadHistorySyncConfiguration, LoadQfqWindowConfiguration
from trader.infra.settings.data_paths import DataPaths
from trader.infra.settings.parser import ConfigurationError

_PROJECT = Path(__file__).resolve().parents[3]


def _write_settings(root: Path, review: dict[str, object] | None) -> None:
    document = json.loads((_PROJECT / "config/download.json").read_text(encoding="utf-8"))
    if review is None:
        document.pop("price_review")
    else:
        document["price_review"] = review
    (root / "config").mkdir()
    (root / "config/download.json").write_text(json.dumps(document), encoding="utf-8")


def test_history_and_market_use_one_explicit_review_policy(tmp_path: Path) -> None:
    policy = {"interval_sessions": 7, "maximum_codes": 3, "maximum_requests": 9, "maximum_seconds": 4.5}
    _write_settings(tmp_path, policy)
    history = LoadHistorySyncConfiguration(tmp_path, DataPaths(tmp_path / "data"))
    market = LoadQfqWindowConfiguration(tmp_path)
    assert history.price_review == market.price_review
    assert history.price_review.interval_sessions == 7
    assert history.price_review.maximum_codes == 3
    assert history.price_review.maximum_requests == 9
    assert history.price_review.maximum_seconds == 4.5
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize(
    "review",
    (
        None,
        {},
        {"interval_sessions": True, "maximum_codes": 8, "maximum_requests": 16, "maximum_seconds": 15.0},
        {"interval_sessions": 5, "maximum_codes": 9, "maximum_requests": 16, "maximum_seconds": 15.0},
        {"interval_sessions": 5, "maximum_codes": 8, "maximum_requests": 17, "maximum_seconds": 15.0},
        {"interval_sessions": 5, "maximum_codes": 8, "maximum_requests": 16, "maximum_seconds": 15.1},
        {"interval_sessions": 5, "maximum_codes": 8, "maximum_requests": 16, "maximum_seconds": 0},
        {"interval_sessions": 5, "maximum_codes": 8, "maximum_requests": 16, "maximum_seconds": 15.0, "unknown": 1},
    ),
)
def test_invalid_review_policy_cannot_silently_fall_back(tmp_path: Path, review: dict[str, object] | None) -> None:
    _write_settings(tmp_path, review)
    with pytest.raises((ConfigurationError, ValueError)):
        LoadQfqWindowConfiguration(tmp_path)
    with pytest.raises((ConfigurationError, ValueError)):
        LoadHistorySyncConfiguration(tmp_path, DataPaths(tmp_path / "data"))
