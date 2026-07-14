from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional

from . import config


def realtime_refresh_profile(now: Optional[datetime] = None) -> Dict[str, object]:
    current = now or datetime.now()
    minutes = current.hour * 60 + current.minute
    weekday = current.weekday() < 5
    morning = 565 <= minutes <= 690
    afternoon = 780 <= minutes <= 910
    active = weekday and (morning or afternoon)
    profile: Dict[str, object] = {
        "active": active,
        "phase": "closed",
        "full_market_seconds": None,
        "candidate_seconds": None,
        "recommendation_seconds": None,
        "force_final_full": False,
    }
    if not active:
        return profile
    if minutes < 860:
        profile.update(
            phase="normal",
            full_market_seconds=config.FULL_MARKET_REFRESH_NORMAL_SECONDS,
            candidate_seconds=config.CANDIDATE_POOL_REFRESH_NORMAL_SECONDS,
            recommendation_seconds=config.RECOMMENDATION_POOL_REFRESH_NORMAL_SECONDS,
        )
    elif minutes < 870:
        profile.update(
            phase="warmup",
            full_market_seconds=config.FULL_MARKET_REFRESH_WARMUP_SECONDS,
            candidate_seconds=config.CANDIDATE_POOL_REFRESH_WARMUP_SECONDS,
            recommendation_seconds=config.RECOMMENDATION_POOL_REFRESH_WARMUP_SECONDS,
        )
    elif minutes < 888:
        profile.update(
            phase="decision",
            full_market_seconds=config.FULL_MARKET_REFRESH_DECISION_SECONDS,
            candidate_seconds=config.CANDIDATE_POOL_REFRESH_DECISION_SECONDS,
            recommendation_seconds=config.RECOMMENDATION_POOL_REFRESH_DECISION_SECONDS,
        )
    elif minutes < 890:
        profile.update(
            phase="finalizing",
            full_market_seconds=config.FULL_MARKET_REFRESH_DECISION_SECONDS,
            candidate_seconds=config.CANDIDATE_POOL_REFRESH_DECISION_SECONDS,
            recommendation_seconds=config.RECOMMENDATION_POOL_REFRESH_DECISION_SECONDS,
            force_final_full=True,
        )
    else:
        profile.update(
            phase="frozen",
            recommendation_seconds=config.RECOMMENDATION_POOL_REFRESH_FROZEN_SECONDS,
        )
    return profile
