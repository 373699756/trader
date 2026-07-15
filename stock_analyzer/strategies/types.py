from __future__ import annotations


TODAY_PICKS = "today_picks"
TOMORROW_PICKS = "tomorrow_picks"
SWING_2_5D_PICKS = "swing_2_5d_picks"

CANONICAL_STRATEGIES = (TODAY_PICKS, TOMORROW_PICKS, SWING_2_5D_PICKS)

STRATEGY_ALIASES = {
    "today_term": TODAY_PICKS,
    "today_picks": TODAY_PICKS,
    "tomorrow_picks": TOMORROW_PICKS,
    "swing_picks": SWING_2_5D_PICKS,
    "swing_2_5d_picks": SWING_2_5D_PICKS,
}

LEGACY_STRATEGY_NAMES = {
    TODAY_PICKS: "today_term",
    TOMORROW_PICKS: "tomorrow_picks",
    SWING_2_5D_PICKS: "swing_picks",
}

STRATEGY_LABELS = {
    TODAY_PICKS: "今早",
    TOMORROW_PICKS: "明日优先",
    SWING_2_5D_PICKS: "2-5日持有",
}


def canonical_strategy_name(strategy_name: str) -> str:
    return STRATEGY_ALIASES.get(str(strategy_name or "").strip(), str(strategy_name or "").strip())


def storage_strategy_name(strategy_name: str) -> str:
    canonical = canonical_strategy_name(strategy_name)
    return LEGACY_STRATEGY_NAMES.get(canonical, canonical)
