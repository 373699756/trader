"""In-process DeepSeek precompute scheduler with a SQLite lease."""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypedDict

from . import config

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOGGER = logging.getLogger(__name__)


class DeepSeekLastRun(TypedDict, total=False):
    slot: str
    status: str
    started_at: str
    completed_at: str
    exit_code: int | None
    message: str


class DeepSeekScheduleStatus(TypedDict):
    enabled: bool
    mode: str
    running: bool
    date: str
    precompute_times: list[str]
    on_demand_start: str
    deadline: str
    freeze_at: str
    daily_call_limit: int
    last_run: DeepSeekLastRun
    production_applied: bool


class DeepSeekPrecomputeScheduler:
    def __init__(self, *, thread_factory: Callable[..., threading.Thread] | None = None) -> None:
        self._thread_factory = thread_factory or threading.Thread
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if not self._enabled():
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            try:
                thread = self._thread_factory(
                    target=self._run_loop,
                    name="deepseek-precompute-scheduler",
                    daemon=True,
                )
                self._thread = thread
                thread.start()
            except Exception:
                self._thread = None
                self._stop_event.set()
                raise
            return True

    def stop(self, timeout_seconds: float = 5.0) -> None:
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(max(0.0, timeout_seconds))
        with self._lock:
            if thread is None or not thread.is_alive():
                self._thread = None

    def status(self, now: datetime | None = None) -> DeepSeekScheduleStatus:
        timestamp = now or datetime.now()
        thread = self._thread
        return {
            "enabled": self._enabled(),
            "mode": "in_process_sqlite_lease",
            "running": bool(thread is not None and thread.is_alive()),
            "date": timestamp.date().isoformat(),
            "precompute_times": list(self._schedule_times()),
            "on_demand_start": str(getattr(config, "DEEPSEEK_ON_DEMAND_START", "14:30")),
            "deadline": str(getattr(config, "DEEPSEEK_PRECOMPUTE_DEADLINE", "14:48")),
            "freeze_at": str(getattr(config, "RECOMMENDATION_FREEZE_CUTOFF_TIME", "14:50")),
            "daily_call_limit": int(getattr(config, "DEEPSEEK_DAILY_CALL_LIMIT", 50)),
            "last_run": self._last_run(),
            "production_applied": False,
        }

    def _run_loop(self) -> None:
        poll_seconds = max(5, int(getattr(config, "DEEPSEEK_SCHEDULER_POLL_SECONDS", 15)))
        while not self._stop_event.is_set():
            try:
                self._run_due_slot(datetime.now())
            except Exception:
                _LOGGER.exception("DeepSeek scheduler slot failed")
            self._stop_event.wait(poll_seconds)

    def _run_due_slot(self, now: datetime) -> None:
        if now.weekday() >= 5 or now.strftime("%H:%M") >= str(getattr(config, "DEEPSEEK_ON_DEMAND_START", "14:30"))[:5]:
            return
        window_seconds = max(30, int(getattr(config, "DEEPSEEK_SCHEDULER_SLOT_WINDOW_SECONDS", 180)))
        for clock in self._schedule_times():
            scheduled_at = datetime.fromisoformat(f"{now.date().isoformat()}T{clock[:5]}:00")
            delay = (now - scheduled_at).total_seconds()
            if 0 <= delay <= window_seconds:
                slot_key = f"{now.date().isoformat()}T{clock[:5]}"
                if self._claim(slot_key, now):
                    self._execute(slot_key)
                return

    def _claim(self, slot_key: str, now: datetime) -> bool:
        lease_seconds = max(60, int(getattr(config, "DEEPSEEK_SCHEDULER_LEASE_SECONDS", 1200)))
        owner = f"{os.getpid()}:{threading.get_ident()}"
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, lease_until FROM deepseek_scheduler_leases WHERE slot_key = ?",
                (slot_key,),
            ).fetchone()
            if row is not None and (row[0] in ("ok", "failed") or str(row[1] or "") > now.isoformat()):
                conn.commit()
                return False
            conn.execute(
                """
                INSERT INTO deepseek_scheduler_leases(
                    slot_key, owner, status, started_at, lease_until, completed_at, exit_code, message
                ) VALUES (?, ?, 'running', ?, ?, '', NULL, '')
                ON CONFLICT(slot_key) DO UPDATE SET
                    owner = excluded.owner,
                    status = excluded.status,
                    started_at = excluded.started_at,
                    lease_until = excluded.lease_until,
                    completed_at = '',
                    exit_code = NULL,
                    message = ''
                """,
                (slot_key, owner, now.isoformat(), (now + timedelta(seconds=lease_seconds)).isoformat()),
            )
            conn.commit()
            return True

    def _execute(self, slot_key: str) -> None:
        timeout = max(60, int(getattr(config, "DEEPSEEK_SCHEDULER_JOB_TIMEOUT_SECONDS", 900)))
        try:
            result = subprocess.run(
                [sys.executable, "-m", "stock_analyzer.jobs", "deepseek-precompute"],
                cwd=str(_PROJECT_ROOT),
                env=os.environ.copy(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            status = "ok" if result.returncode == 0 else "failed"
            message = (result.stderr or result.stdout or "")[-1000:]
            exit_code = int(result.returncode)
        except Exception as exc:
            status = "failed"
            message = str(exc)[:1000]
            exit_code = -1
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE deepseek_scheduler_leases
                    SET status = ?, completed_at = ?, lease_until = '', exit_code = ?, message = ?
                    WHERE slot_key = ?
                    """,
                    (status, datetime.now().isoformat(), exit_code, message, slot_key),
                )

    def _last_run(self) -> DeepSeekLastRun:
        try:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    """
                    SELECT slot_key, status, started_at, completed_at, exit_code, message
                    FROM deepseek_scheduler_leases
                    ORDER BY slot_key DESC LIMIT 1
                    """
                ).fetchone()
            if row is None:
                return {}
            return {
                "slot": row[0],
                "status": row[1],
                "started_at": row[2],
                "completed_at": row[3],
                "exit_code": row[4],
                "message": row[5],
            }
        except Exception:
            return {}

    def _connect(self) -> sqlite3.Connection:
        path = str(getattr(config, "DEEPSEEK_SCHEDULER_DB_PATH", ".runtime/deepseek_scheduler.sqlite3"))
        target = Path(path)
        if not target.is_absolute():
            target = _PROJECT_ROOT / target
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(target), timeout=10.0)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deepseek_scheduler_leases(
                slot_key TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                lease_until TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT '',
                exit_code INTEGER,
                message TEXT NOT NULL DEFAULT ''
            )
            """
        )
        return conn

    @staticmethod
    def _enabled() -> bool:
        return bool(getattr(config, "ENABLE_DEEPSEEK_FEATURES", True)) and bool(
            getattr(config, "DEEPSEEK_INTERNAL_SCHEDULER_ENABLED", True)
        )

    @staticmethod
    def _schedule_times() -> tuple[str, ...]:
        return tuple(str(value)[:5] for value in getattr(config, "DEEPSEEK_PRECOMPUTE_TIMES", ()))


_SCHEDULER = DeepSeekPrecomputeScheduler()


def start_deepseek_scheduler() -> bool:
    return _SCHEDULER.start()


def stop_deepseek_scheduler(timeout_seconds: float = 5.0) -> None:
    _SCHEDULER.stop(timeout_seconds)


def deepseek_schedule_status(now: datetime | None = None) -> DeepSeekScheduleStatus:
    return _SCHEDULER.status(now)


__all__ = [
    "DeepSeekLastRun",
    "DeepSeekScheduleStatus",
    "deepseek_schedule_status",
    "start_deepseek_scheduler",
    "stop_deepseek_scheduler",
]
