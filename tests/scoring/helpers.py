from __future__ import annotations

from contextlib import contextmanager
from typing import Iterable, Mapping
import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from stock_analyzer import config
from stock_analyzer.app import create_app
from stock_analyzer.strategy_validation import StrategyValidationStore


def quote_frame(rows: Iterable[Mapping[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(list(rows))


def history_frame(start_date: str, prices: Iterable[float]) -> pd.DataFrame:
    values = [float(price) for price in prices]
    dates = pd.date_range(start_date, periods=len(values), freq="D").strftime("%Y%m%d").tolist()
    return pd.DataFrame(
        {
            "trade_date": dates,
            "open": values,
            "high": [price * 1.01 for price in values],
            "low": [price * 0.99 for price in values],
            "price": values,
        }
    )


def validation_history(start_date: str, future_days: int, final_price: float) -> pd.DataFrame:
    future_prices = [
        10 + (final_price - 10) * (idx + 1) / max(1, future_days)
        for idx in range(future_days)
    ]
    return history_frame(start_date, [10, *future_prices])


class FakeHistoryProvider:
    def __init__(self, histories: Mapping[str, pd.DataFrame] | None = None, default_history: pd.DataFrame | None = None):
        self.histories = {str(code): frame for code, frame in (histories or {}).items()}
        self.default_history = default_history

    def get_history(self, code, days=180):
        key = str(code)
        if key in self.histories:
            return self.histories[key]
        if self.default_history is not None:
            return self.default_history
        raise KeyError(key)


def fake_provider(histories: Mapping[str, pd.DataFrame] | None = None, default_history: pd.DataFrame | None = None):
    return FakeHistoryProvider(histories, default_history)


def make_validation_store(tmp_path: Path, name: str = "validation.sqlite3") -> StrategyValidationStore:
    return StrategyValidationStore(str(Path(tmp_path) / name))


def code_set(frame: pd.DataFrame) -> set[str]:
    return set(frame["code"].astype(str))


def write_json(path: Path, payload: object) -> str:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def write_text(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


@contextmanager
def app_patch_context(tmp_path: Path, **overrides):
    root = Path(tmp_path)
    values = {
        "STATE_PATH": str(root / "state.json"),
        "VALIDATION_DB_PATH": str(root / "validation.sqlite3"),
        "RECOMMENDATION_SNAPSHOT_PATH": str(root / "latest_recommendations.json"),
        "VALIDATION_AUTO_UPDATE_ENABLED": False,
        "VALIDATION_AUTO_SNAPSHOT_ENABLED": False,
        "ENABLE_INLINE_SENTIMENT": False,
        "ENABLE_MARKET_NEWS": False,
        "ENABLE_HISTORY_FACTORS": False,
        "ENABLE_HOT_RANKS": False,
        "ENABLE_INDUSTRY_STRENGTH": False,
        "ENABLE_FUNDAMENTALS": False,
    }
    values.update(overrides)
    patches = [patch.object(config, name, value) for name, value in values.items()]
    exits = []
    try:
        for item in patches:
            exits.append(item.__enter__())
        yield create_app()
    finally:
        for item in reversed(patches):
            item.__exit__(None, None, None)


def score_tech_potential_candidates(candidates: pd.DataFrame, top_n: int = 10):
    from stock_analyzer.scoring_core.explanations import _attach_signal_explanation
    from stock_analyzer.scoring_core.theme_scores import _tech_theme_score

    rows = []
    for _, row in candidates.iterrows():
        theme, theme_score = _tech_theme_score(row)
        if not theme:
            continue
        heat_penalty = max(0.0, float(row.get("sixty_day_pct", 0) or 0) - 45.0) * 0.5
        score = max(0.0, min(100.0, float(theme_score) - heat_penalty))
        item = row.to_dict()
        item.update(
            {
                "score": round(score, 2),
                "theme": theme,
                "tech_theme": theme,
                "horizon": "tech_potential",
            }
        )
        _attach_signal_explanation(item, row, "tech_potential", "科技潜力", "科技潜力")
        rows.append(item)
    rows.sort(key=lambda item: item.get("score", 0), reverse=True)
    for index, row in enumerate(rows[:top_n], start=1):
        row["rank"] = index
    return rows[:top_n], {"matched_count": len(rows), "strategy_version": "tech_potential_legacy_test"}
