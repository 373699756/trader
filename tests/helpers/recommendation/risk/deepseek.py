from __future__ import annotations
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from trader.infra.http_contracts import HttpStatusError
from trader.infra.settings import DeepSeekSettings
from trader.infra.process_lock import ProcessLock
from trader.recommendation.domain.market.models import (
    Evidence,
    FeatureSnapshot,
    MarketQuote,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.deepseek.budget import DeepSeekBudgetLedger
from trader.recommendation.infra.deepseek.budget_file import BudgetStateFile
from trader.recommendation.infra.deepseek.schema import (
    SCHEMA_VERSION as REVIEW_SCHEMA_VERSION,
)

NOW = datetime(2026, 7, 16, 6, 30, tzinfo=timezone.utc)


def _candidate_with_evidence() -> FeatureSnapshot:
    return FeatureSnapshot(
        quote=MarketQuote(
            code="600001",
            name="测试股份",
            price=12.0,
            previous_close=11.65,
            open_price=11.8,
            high=12.2,
            low=11.7,
            pct_change=3.0,
            change_5m=1.0,
            speed=0.8,
            volume_ratio=2.0,
            turnover_rate=3.0,
            amount=300_000_000.0,
            amplitude=4.0,
            market_cap=30_000_000_000.0,
            industry="工业",
            source="fixture",
            source_time=NOW,
            received_time=NOW,
            data_version="fixture-base",
        ),
        values={"relative_strength_5d": 65.0, "industry_strength": 60.0},
        observed_at=NOW,
        history_days=60,
        evidence=(
            Evidence(
                "e-1",
                "announcement",
                "监管公告",
                "exchange",
                NOW - timedelta(hours=1),
                NOW,
                "announcement-source",
            ),
        ),
    )


def _candidate(code: str, evidence_id: str) -> FeatureSnapshot:
    original = _candidate_with_evidence()
    return replace(
        original,
        quote=replace(original.quote, code=code),
        evidence=(replace(original.evidence[0], evidence_id=evidence_id),),
    )


def _valid_payload(code: str) -> dict[str, object]:
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "results": [
            {
                "code": code,
                "abstain": False,
                "catalyst": {
                    "direction": "positive",
                    "importance": "high",
                    "confirmation": "confirmed",
                    "cycle": "short",
                    "evidence_ids": ["e-1"],
                },
                "price_reaction": {"bucket": "not_reflected", "evidence_ids": ["e-1"]},
                "fundamental": {"direction": "improving", "evidence_ids": ["e-1"]},
                "industry_policy": {"direction": "positive", "evidence_ids": ["e-1"]},
                "risks": {
                    "regulatory": {
                        "present": True,
                        "severity": "high",
                        "confidence": 0.9,
                        "evidence_ids": ["e-1"],
                        "assessment": "high risk",
                    },
                    **{
                        name: {
                            "present": False,
                            "severity": "low",
                            "confidence": 0.0,
                            "evidence_ids": [],
                            "assessment": "not present",
                        }
                        for name in ("shareholder_reduction", "unlock", "pledge", "litigation", "earnings")
                    },
                },
                "conflicts": [],
                "coverage": 0.8,
            }
        ],
    }


def _unknown_payload(code: str) -> dict[str, object]:
    payload = _valid_payload(code)
    result = payload["results"][0]
    result["abstain"] = True
    for field in ("catalyst", "price_reaction", "fundamental", "industry_policy"):
        result[field]["evidence_ids"] = []
    result["risks"]["regulatory"].update({"present": False, "confidence": 0.0, "evidence_ids": []})
    return payload


class FakeHttpResponse:
    def __init__(self, status_code: int, payload: object, *, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HttpStatusError(self.status_code)

    def json(self) -> object:
        return self._payload


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        return self._value

    def set(self, value: datetime) -> None:
        self._value = value


def _state_file(path):
    return BudgetStateFile(path, ProcessLock(path.parent / ".deepseek.lock", label="DeepSeek"))


def _budget(database_path) -> DeepSeekBudgetLedger:
    ledger = DeepSeekBudgetLedger(
        _state_file(database_path),
        daily_hard_limit=2,
        strategy_limits={"tomorrow": 2, "d25": 0, "shared_preheat": 0, "emergency": 0},
        stage_targets={"tomorrow_morning": 0},
        stage_limits={"tomorrow_morning": 2},
    )
    ledger.initialize(NOW - timedelta(days=1))
    ledger.initialize(NOW)
    return ledger


def _settings() -> DeepSeekSettings:
    return DeepSeekSettings(
        enabled=True,
        base_url="https://api.deepseek.example/v1",
        model="deepseek-v4-flash",
        timeout_seconds=1.0,
        batch_size=8,
        max_tokens=256,
        daily_hard_limit=2,
        strategy_limits={"tomorrow": 2, "d25": 0, "shared_preheat": 0, "emergency": 0},
        stage_targets={"tomorrow_morning": 0},
        stage_limits={"tomorrow_morning": 2},
        api_key="secret",
    )


def _reviewer_policy() -> dict[str, object]:
    return {
        "dimension_weights": {
            Strategy.TOMORROW: {
                "value_quality": 0.10,
                "financial_health": 0.10,
                "market_flow": 0.40,
                "industry_policy": 0.15,
                "risk_quality": 0.25,
            },
            Strategy.TOMORROW: {
                "value_quality": 0.15,
                "financial_health": 0.20,
                "market_flow": 0.25,
                "industry_policy": 0.20,
                "risk_quality": 0.20,
            },
            Strategy.D25: {
                "value_quality": 0.20,
                "financial_health": 0.25,
                "market_flow": 0.20,
                "industry_policy": 0.20,
                "risk_quality": 0.15,
            },
            Strategy.LONG: {
                "value_quality": 0.30,
                "financial_health": 0.30,
                "market_flow": 0.10,
                "industry_policy": 0.20,
                "risk_quality": 0.10,
            },
        },
        "strategy_version": "strategy-test-fixture",
        "confidence_coverage_min": 0.5,
        "minimum_known_dimensions": 2,
    }
