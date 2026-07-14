import json
import os
import sqlite3
from datetime import datetime
from typing import Dict, List, Tuple

import pandas as pd

from . import config
from .execution_policy import execution_cost_for_strategy, policy_from_signal
from .normalization import coerce_number, normalize_code
from .portfolio import build_portfolio
from .risk_rules import simulate_exit
from .sqlite_support import sqlite_transaction
from .validation_policy import (
    daily_limit_pct as _daily_limit_pct,
    is_unbuyable_limit_up as _is_unbuyable_limit_up,
    primary_return_config as _primary_return_config,
)


_connect_paper_db = sqlite_transaction


class PaperTradingStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._init_db()

    def run_paper_trade(
        self,
        provider,
        validation_store,
        strategy_name: str,
        signal_date: str = "",
    ) -> Dict[str, object]:
        resolved_date, rows = _snapshot_rows(validation_store, strategy_name, signal_date)
        if not resolved_date:
            return {"ok": True, "strategy": strategy_name, "status": "no_signal", "saved": 0}
        portfolio = build_portfolio(rows)
        selected = [row for row in portfolio.get("rows", []) if coerce_number(row.get("suggested_weight")) > 0]
        if not selected:
            self._upsert_nav(strategy_name, resolved_date, 0.0, 0.0, 0, 0, 0, 0, "no_trade")
            return {
                "ok": True,
                "strategy": strategy_name,
                "signal_date": resolved_date,
                "status": "no_trade",
                "saved": 0,
                "portfolio": portfolio.get("summary", {}),
            }

        saved = 0
        statuses: Dict[str, int] = {}
        for row in selected:
            trade = _evaluate_trade(provider, strategy_name, resolved_date, row)
            self._upsert_trade(strategy_name, resolved_date, row, trade)
            statuses[trade["status"]] = statuses.get(trade["status"], 0) + 1
            saved += 1
        self._recompute_nav(strategy_name)
        return {
            "ok": True,
            "strategy": strategy_name,
            "signal_date": resolved_date,
            "status": "updated",
            "saved": saved,
            "statuses": statuses,
            "portfolio": portfolio.get("summary", {}),
        }

    def performance(self, strategy_name: str = "all", days: int = 120) -> Dict[str, object]:
        rows = self._daily_rows(strategy_name, days)
        if not rows:
            return {
                "metrics": {
                    "day_count": 0,
                    "trade_count": 0,
                    "closed_count": 0,
                    "total_return_pct": 0.0,
                    "max_drawdown_pct": 0.0,
                    "win_rate_pct": 0.0,
                    "avg_period_return_pct": 0.0,
                    "avg_invested_weight_pct": 0.0,
                    "latest_cash_pct": 100.0,
                    "benchmark_return_pct": 0.0,
                    "excess_return_pct": 0.0,
                    "benchmark_label": "未接入基准",
                },
                "daily": [],
            }
        daily = _rebuild_daily_nav(rows)
        metrics = _performance_metrics(daily)
        metrics["strategy_name"] = strategy_name
        if strategy_name == "all":
            metrics["by_strategy"] = [self.performance(name, days=days)["metrics"] for name in self._strategy_names()]
        return {"metrics": metrics, "daily": daily}

    def trades(self, strategy_name: str = "all", limit: int = 200) -> List[Dict[str, object]]:
        where = ""
        params: List[object] = []
        if strategy_name and strategy_name != "all":
            where = "WHERE strategy_name = ?"
            params.append(strategy_name)
        params.append(max(1, int(limit)))
        with _connect_paper_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT *
                FROM paper_trades
                {where}
                ORDER BY signal_date DESC, strategy_name ASC, rank ASC
                LIMIT ?
                """.format(where=where),
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def _upsert_trade(
        self,
        strategy_name: str,
        signal_date: str,
        row: Dict[str, object],
        trade: Dict[str, object],
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with _connect_paper_db(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO paper_trades
                (strategy_name, signal_date, code, name, rank, theme, suggested_weight_pct,
                 entry_date, entry_price, exit_date, exit_price, gross_return_pct,
                 trade_cost_pct, net_return_pct, weighted_return_pct, status, exit_reason,
                 raw_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_name, signal_date, code) DO UPDATE SET
                  name=excluded.name,
                  rank=excluded.rank,
                  theme=excluded.theme,
                  suggested_weight_pct=excluded.suggested_weight_pct,
                  entry_date=excluded.entry_date,
                  entry_price=excluded.entry_price,
                  exit_date=excluded.exit_date,
                  exit_price=excluded.exit_price,
                  gross_return_pct=excluded.gross_return_pct,
                  trade_cost_pct=excluded.trade_cost_pct,
                  net_return_pct=excluded.net_return_pct,
                  weighted_return_pct=excluded.weighted_return_pct,
                  status=excluded.status,
                  exit_reason=excluded.exit_reason,
                  raw_json=excluded.raw_json,
                  updated_at=excluded.updated_at
                """,
                (
                    strategy_name,
                    signal_date,
                    normalize_code(row.get("code")),
                    str(row.get("name") or ""),
                    int(row.get("rank") or 0),
                    str(row.get("portfolio_theme") or row.get("theme") or row.get("industry") or ""),
                    coerce_number(row.get("suggested_weight")),
                    trade.get("entry_date", ""),
                    coerce_number(trade.get("entry_price")),
                    trade.get("exit_date", ""),
                    coerce_number(trade.get("exit_price")),
                    coerce_number(trade.get("gross_return_pct")),
                    coerce_number(trade.get("trade_cost_pct")),
                    coerce_number(trade.get("net_return_pct")),
                    coerce_number(trade.get("weighted_return_pct")),
                    trade.get("status", "pending"),
                    trade.get("exit_reason", ""),
                    json.dumps(row, ensure_ascii=False),
                    now,
                    now,
                ),
            )

    def _recompute_nav(self, strategy_name: str) -> None:
        with _connect_paper_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT *
                FROM paper_trades
                WHERE strategy_name = ?
                ORDER BY signal_date ASC, rank ASC
                """,
                (strategy_name,),
            ).fetchall()
        grouped: Dict[str, List[sqlite3.Row]] = {}
        for row in rows:
            event_date = str(row["exit_date"] or row["signal_date"])
            grouped.setdefault(event_date, []).append(row)

        with _connect_paper_db(self.db_path) as conn:
            conn.execute("DELETE FROM portfolio_nav WHERE strategy_name = ?", (strategy_name,))

        nav = 100.0
        high = 100.0
        for event_date in sorted(grouped):
            items = grouped[event_date]
            closed = [row for row in items if row["status"] == "closed"]
            pending = [row for row in items if row["status"] in ("pending_entry", "pending_data", "open")]
            skipped = [row for row in items if row["status"] == "skipped"]
            invested = sum(coerce_number(row["suggested_weight_pct"]) for row in items if row["status"] != "skipped")
            period_return = sum(coerce_number(row["weighted_return_pct"]) for row in closed)
            nav = round(nav * (1.0 + period_return / 100.0), 6)
            high = max(high, nav)
            drawdown = round((nav / high - 1.0) * 100.0, 4) if high > 0 else 0.0
            status = "closed" if closed and not pending else "pending" if pending else "skipped" if skipped else "no_trade"
            self._upsert_nav(
                strategy_name,
                event_date,
                period_return,
                nav,
                len(items),
                len(closed),
                len(pending),
                len(skipped),
                status,
                invested_weight_pct=invested,
                cash_pct=max(0.0, 100.0 - invested),
                max_drawdown_pct=drawdown,
            )

    def _upsert_nav(
        self,
        strategy_name: str,
        signal_date: str,
        period_return_pct: float,
        nav: float,
        trade_count: int,
        closed_count: int,
        pending_count: int,
        skipped_count: int,
        status: str,
        invested_weight_pct: float = 0.0,
        cash_pct: float = 100.0,
        max_drawdown_pct: float = 0.0,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with _connect_paper_db(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO portfolio_nav
                (strategy_name, signal_date, trade_count, closed_count, pending_count,
                 skipped_count, invested_weight_pct, cash_pct, period_return_pct, nav,
                 max_drawdown_pct, turnover_pct, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_name, signal_date) DO UPDATE SET
                  trade_count=excluded.trade_count,
                  closed_count=excluded.closed_count,
                  pending_count=excluded.pending_count,
                  skipped_count=excluded.skipped_count,
                  invested_weight_pct=excluded.invested_weight_pct,
                  cash_pct=excluded.cash_pct,
                  period_return_pct=excluded.period_return_pct,
                  nav=excluded.nav,
                  max_drawdown_pct=excluded.max_drawdown_pct,
                  turnover_pct=excluded.turnover_pct,
                  status=excluded.status,
                  updated_at=excluded.updated_at
                """,
                (
                    strategy_name,
                    signal_date,
                    int(trade_count),
                    int(closed_count),
                    int(pending_count),
                    int(skipped_count),
                    round(coerce_number(invested_weight_pct), 4),
                    round(coerce_number(cash_pct), 4),
                    round(coerce_number(period_return_pct), 4),
                    round(coerce_number(nav), 6),
                    round(coerce_number(max_drawdown_pct), 4),
                    round(coerce_number(invested_weight_pct), 4),
                    status,
                    now,
                ),
            )

    def _daily_rows(self, strategy_name: str, days: int) -> List[Dict[str, object]]:
        where = ""
        params: List[object] = []
        if strategy_name and strategy_name != "all":
            where = "WHERE strategy_name = ?"
            params.append(strategy_name)
        with _connect_paper_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT *
                FROM portfolio_nav
                {where}
                ORDER BY signal_date DESC, strategy_name ASC
                LIMIT ?
                """.format(where=where),
                [*params, max(1, int(days)) * (20 if strategy_name == "all" else 1)],
            ).fetchall()
        return [dict(row) for row in rows]

    def _strategy_names(self) -> List[str]:
        with _connect_paper_db(self.db_path) as conn:
            rows = conn.execute("SELECT DISTINCT strategy_name FROM portfolio_nav ORDER BY strategy_name").fetchall()
        return [str(row[0]) for row in rows if row and row[0]]

    def _init_db(self) -> None:
        with _connect_paper_db(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_name TEXT NOT NULL,
                    signal_date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    rank INTEGER NOT NULL DEFAULT 0,
                    theme TEXT NOT NULL DEFAULT '',
                    suggested_weight_pct REAL NOT NULL DEFAULT 0,
                    entry_date TEXT NOT NULL DEFAULT '',
                    entry_price REAL NOT NULL DEFAULT 0,
                    exit_date TEXT NOT NULL DEFAULT '',
                    exit_price REAL NOT NULL DEFAULT 0,
                    gross_return_pct REAL NOT NULL DEFAULT 0,
                    trade_cost_pct REAL NOT NULL DEFAULT 0,
                    net_return_pct REAL NOT NULL DEFAULT 0,
                    weighted_return_pct REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    exit_reason TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(strategy_name, signal_date, code)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_nav (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_name TEXT NOT NULL,
                    signal_date TEXT NOT NULL,
                    trade_count INTEGER NOT NULL DEFAULT 0,
                    closed_count INTEGER NOT NULL DEFAULT 0,
                    pending_count INTEGER NOT NULL DEFAULT 0,
                    skipped_count INTEGER NOT NULL DEFAULT 0,
                    invested_weight_pct REAL NOT NULL DEFAULT 0,
                    cash_pct REAL NOT NULL DEFAULT 100,
                    period_return_pct REAL NOT NULL DEFAULT 0,
                    nav REAL NOT NULL DEFAULT 100,
                    max_drawdown_pct REAL NOT NULL DEFAULT 0,
                    turnover_pct REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    updated_at TEXT NOT NULL,
                    UNIQUE(strategy_name, signal_date)
                )
                """
            )


def _snapshot_rows(validation_store, strategy_name: str, signal_date: str = "") -> Tuple[str, List[Dict[str, object]]]:
    selected_date = signal_date
    if not selected_date:
        dates = validation_store.list_signal_dates(strategy_name)
        selected_date = dates[0]["signal_date"] if dates else ""
    if not selected_date:
        return "", []
    signals = validation_store.signals_for_date(selected_date, strategy_name)
    rows = []
    for signal in signals:
        raw = signal.get("raw") if isinstance(signal.get("raw"), dict) else {}
        item = dict(raw or {})
        item["rank"] = signal.get("rank")
        item.setdefault("code", signal.get("code"))
        item.setdefault("name", signal.get("name"))
        item.setdefault("price", signal.get("price_at_signal"))
        item.setdefault("turnover", signal.get("turnover"))
        item.setdefault("theme", signal.get("theme"))
        item.setdefault("market_label", signal.get("market"))
        item.setdefault("market", signal.get("market"))
        item.setdefault("score", signal.get("score"))
        item.setdefault("execution_policy_json", signal.get("execution_policy_json"))
        item.setdefault("strategy_name", signal.get("strategy_name"))
        rows.append(item)
    rows.sort(key=lambda row: int(row.get("rank") or 9999))
    return selected_date, rows


def _evaluate_trade(provider, strategy_name: str, signal_date: str, row: Dict[str, object]) -> Dict[str, object]:
    code = normalize_code(row.get("code"))
    weight = coerce_number(row.get("suggested_weight"))
    signal_price = coerce_number(row.get("price"))
    policy = policy_from_signal(row, strategy_name=strategy_name)
    trade_cost = execution_cost_for_strategy(
        {"turnover": row.get("turnover"), "strategy_name": str(strategy_name), "market": str(row.get("market_label") or row.get("market") or "")},
        strategy_name=strategy_name,
        policy=policy,
    )
    try:
        history = provider.get_history(code, days=int(getattr(config, "PAPER_TRADING_HISTORY_DAYS", 220)))
    except Exception:
        history = None
    if history is None or history.empty or "trade_date" not in history.columns:
        return _pending_trade("pending_data", signal_price, trade_cost)

    df = history.sort_values("trade_date").reset_index(drop=True).copy()
    if "prev_close" not in df.columns:
        df["prev_close"] = df["price"].shift(1)
    date_key = str(signal_date).replace("-", "")
    future = df[df["trade_date"].astype(str).str.replace("-", "", regex=False) > date_key].reset_index(drop=True)
    if future.empty:
        return _pending_trade("pending_entry", signal_price, trade_cost)

    first = future.iloc[0]
    previous_rows = df[df["trade_date"].astype(str).str.replace("-", "", regex=False) <= date_key]
    previous_close = coerce_number(previous_rows.iloc[-1].get("price")) if not previous_rows.empty else coerce_number(first.get("prev_close"))
    limit_pct = _daily_limit_pct(code, str(row.get("market_label") or row.get("market") or ""))
    if _is_unbuyable_limit_up(first, previous_close, limit_pct):
        return {
            "status": "skipped",
            "entry_date": str(first.get("trade_date", "")),
            "entry_price": 0.0,
            "exit_date": "",
            "exit_price": 0.0,
            "gross_return_pct": 0.0,
            "trade_cost_pct": 0.0,
            "net_return_pct": 0.0,
            "weighted_return_pct": 0.0,
            "exit_reason": "unbuyable_limit_up",
        }

    entry_price = coerce_number(first.get("open")) or coerce_number(first.get("price")) or signal_price
    if entry_price <= 0:
        return _pending_trade("pending_entry", signal_price, trade_cost)
    _, holding_days, _ = _primary_return_config(strategy_name)
    exit_result = simulate_exit(future, entry_price, holding_days=holding_days, policy={"limit_down_pct": limit_pct})
    exit_reason = str(exit_result.get("exit_reason") or "hold_to_term")
    ready = exit_reason != "hold_to_term" or len(future) >= max(1, int(holding_days))
    gross = coerce_number(exit_result.get("exit_return"))
    net = round(gross - trade_cost, 4)
    status = "closed" if ready else "open"
    capital_divisor = max(1, int(holding_days)) if getattr(config, "PAPER_TRADING_SPREAD_CAPITAL_BY_HOLDING_DAYS", True) else 1
    return {
        "status": status,
        "entry_date": str(first.get("trade_date", "")),
        "entry_price": round(entry_price, 4),
        "exit_date": str(exit_result.get("exit_date") or ""),
        "exit_price": coerce_number(exit_result.get("exit_price")),
        "gross_return_pct": gross,
        "trade_cost_pct": trade_cost,
        "net_return_pct": net,
        "weighted_return_pct": round(weight / 100.0 / capital_divisor * net, 4) if status == "closed" else 0.0,
        "exit_reason": exit_reason if status == "closed" else "mark_to_market",
    }


def _pending_trade(status: str, signal_price: float, trade_cost: float) -> Dict[str, object]:
    return {
        "status": status,
        "entry_date": "",
        "entry_price": signal_price,
        "exit_date": "",
        "exit_price": 0.0,
        "gross_return_pct": 0.0,
        "trade_cost_pct": trade_cost,
        "net_return_pct": 0.0,
        "weighted_return_pct": 0.0,
        "exit_reason": status,
    }


def _rebuild_daily_nav(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    ordered = sorted(rows, key=lambda row: (row["signal_date"], row["strategy_name"]))
    if not ordered:
        return []
    if len({row["strategy_name"] for row in ordered}) <= 1:
        return [
            {
                **row,
                "nav": round(coerce_number(row.get("nav")), 4),
                "period_return_pct": round(coerce_number(row.get("period_return_pct")), 4),
            }
            for row in ordered
        ]

    grouped: Dict[str, List[Dict[str, object]]] = {}
    for row in ordered:
        grouped.setdefault(row["signal_date"], []).append(row)
    nav = 100.0
    high = 100.0
    daily = []
    for signal_date in sorted(grouped):
        items = grouped[signal_date]
        period = sum(coerce_number(row.get("period_return_pct")) for row in items) / len(items)
        nav = round(nav * (1.0 + period / 100.0), 6)
        high = max(high, nav)
        daily.append(
            {
                "strategy_name": "all",
                "signal_date": signal_date,
                "trade_count": sum(int(row.get("trade_count") or 0) for row in items),
                "closed_count": sum(int(row.get("closed_count") or 0) for row in items),
                "pending_count": sum(int(row.get("pending_count") or 0) for row in items),
                "skipped_count": sum(int(row.get("skipped_count") or 0) for row in items),
                "invested_weight_pct": round(sum(coerce_number(row.get("invested_weight_pct")) for row in items) / len(items), 4),
                "cash_pct": round(sum(coerce_number(row.get("cash_pct")) for row in items) / len(items), 4),
                "period_return_pct": round(period, 4),
                "nav": round(nav, 4),
                "max_drawdown_pct": round((nav / high - 1.0) * 100.0, 4) if high > 0 else 0.0,
                "turnover_pct": round(sum(coerce_number(row.get("turnover_pct")) for row in items) / len(items), 4),
                "status": "mixed",
            }
        )
    return daily


def _performance_metrics(daily: List[Dict[str, object]]) -> Dict[str, object]:
    returns = [coerce_number(row.get("period_return_pct")) for row in daily]
    closed = sum(int(row.get("closed_count") or 0) for row in daily)
    total_trades = sum(int(row.get("trade_count") or 0) for row in daily)
    total_return = round(coerce_number(daily[-1].get("nav")) - 100.0, 4) if daily else 0.0
    win_rate = round(sum(1 for value in returns if value > 0) / len(returns) * 100.0, 2) if returns else 0.0
    max_drawdown = min((coerce_number(row.get("max_drawdown_pct")) for row in daily), default=0.0)
    avg_invested = round(sum(coerce_number(row.get("invested_weight_pct")) for row in daily) / len(daily), 4) if daily else 0.0
    latest_cash = coerce_number(daily[-1].get("cash_pct")) if daily else 100.0
    return {
        "day_count": len(daily),
        "trade_count": total_trades,
        "closed_count": closed,
        "total_return_pct": total_return,
        "max_drawdown_pct": round(max_drawdown, 4),
        "win_rate_pct": win_rate,
        "avg_period_return_pct": round(sum(returns) / len(returns), 4) if returns else 0.0,
        "avg_invested_weight_pct": avg_invested,
        "latest_cash_pct": round(latest_cash, 4),
        "benchmark_return_pct": 0.0,
        "excess_return_pct": total_return,
        "benchmark_label": "未接入基准",
    }
