from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from trader.domain.market.models import Evidence
from trader.domain.market.news import NewsSignalPolicy, derive_news_signals

NOW = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
POLICY = NewsSignalPolicy(
    lookback_hours=72.0,
    freshness_full_score_hours=1.0,
    positive_score=75.0,
    neutral_score=50.0,
    negative_score=25.0,
    positive_keywords=("回购", "增持", "中标"),
    negative_keywords=("减持", "立案", "亏损"),
)


def test_news_signals_use_keyword_majority_and_newest_valid_evidence() -> None:
    positive = _evidence("positive", "公司拟回购并增持股份", NOW - timedelta(minutes=30))
    signals = derive_news_signals(
        (
            positive,
            positive,
            _evidence("negative", "股东拟减持股份", NOW - timedelta(hours=4)),
            _evidence("neutral", "公司召开股东大会", NOW - timedelta(hours=2)),
        ),
        observed_at=NOW,
        policy=POLICY,
    )

    assert signals.sentiment_score == pytest.approx(50.0)
    assert signals.freshness_score == pytest.approx(100.0)


def test_news_freshness_declines_linearly_between_one_and_seventy_two_hours() -> None:
    signals = derive_news_signals(
        (_evidence("midpoint", "公司中标项目", NOW - timedelta(hours=36.5)),),
        observed_at=NOW,
        policy=POLICY,
    )

    assert signals.sentiment_score == pytest.approx(75.0)
    assert signals.freshness_score == pytest.approx(50.0)

    boundary = derive_news_signals(
        (_evidence("boundary", "公司中标项目", NOW - timedelta(hours=72)),),
        observed_at=NOW,
        policy=POLICY,
    )
    assert boundary.sentiment_score == 75.0
    assert boundary.freshness_score == 0.0


def test_news_signals_ignore_non_news_invalid_future_and_expired_evidence() -> None:
    signals = derive_news_signals(
        (
            _evidence("announcement", "公司回购", NOW, evidence_type="announcement"),
            _evidence("empty", "", NOW),
            _evidence("future", "公司回购", NOW + timedelta(seconds=1)),
            _evidence("expired", "公司回购", NOW - timedelta(hours=72, seconds=1)),
            Evidence("naive", "news", "公司回购", "fixture", NOW.replace(tzinfo=None)),
        ),
        observed_at=NOW,
        policy=POLICY,
    )

    assert signals.sentiment_score is None
    assert signals.freshness_score is None


def test_news_signal_policy_rejects_overlapping_keywords_and_invalid_scores() -> None:
    with pytest.raises(ValueError, match="must not overlap"):
        NewsSignalPolicy(72, 1, 75, 50, 25, ("回购",), ("回购",))
    with pytest.raises(ValueError, match="negative < neutral < positive"):
        NewsSignalPolicy(72, 1, 50, 50, 25, ("回购",), ("减持",))


def _evidence(evidence_id: str, title: str, published_at: datetime, *, evidence_type: str = "news") -> Evidence:
    return Evidence(evidence_id, evidence_type, title, "fixture", published_at)
