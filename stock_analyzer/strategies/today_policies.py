from __future__ import annotations

from datetime import datetime
from typing import Callable, Dict, List, Tuple

from .. import config
from ..normalization import coerce_number


def _to_lower(value: object) -> str:
    return str(value or "").strip().lower()


def normalize_regime_aliases(regime: object) -> List[str]:
    if not isinstance(regime, str):
        return []
    normalized = _to_lower(regime)
    return [item for item in dict.fromkeys([normalized, normalized.replace("_", "-"), normalized.replace("-", "_")]) if item]


def collect_regime_aliases(value: object) -> List[str]:
    if isinstance(value, str):
        return normalize_regime_aliases(value)
    if isinstance(value, (list, tuple, set)):
        aliases: List[str] = []
        for item in value:
            aliases.extend(normalize_regime_aliases(item))
        return list(dict.fromkeys(aliases))
    return []


class TodayExecutionWindowPolicy:
    """Classify whether now is inside the today-term executable window."""

    def __init__(self, start: str = None, end: str = None) -> None:
        self.start_label = str(start or getattr(config, "TODAY_TERM_RECOMMENDATION_BUY_WINDOW_START", "09:30"))
        self.end_label = str(end or getattr(config, "TODAY_TERM_RECOMMENDATION_BUY_WINDOW_END", "14:00"))
        self.start = self.parse_hhmm(self.start_label)
        self.end = self.parse_hhmm(self.end_label)

    @staticmethod
    def parse_hhmm(value: object) -> Tuple[int, int] | None:
        if not isinstance(value, str):
            return None
        parts = value.strip().split(":")
        if len(parts) < 2:
            return None
        try:
            hour = int(parts[0])
            minute = int(parts[1])
        except ValueError:
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour, minute

    def state(self, now: datetime) -> Tuple[bool, str, str, str]:
        if self.start is None or self.end is None:
            return True, "immediate", "immediate", "执行窗口配置异常，按可执行口径兜底。"
        now_time = now.time().replace(second=0, microsecond=0)
        start_time = now.replace(hour=self.start[0], minute=self.start[1], second=0, microsecond=0).time()
        end_time = now.replace(hour=self.end[0], minute=self.end[1], second=0, microsecond=0).time()
        if now_time < start_time:
            return (
                False,
                "backup_only",
                "backup_only",
                f"{self.start_label}前先观察，{self.start_label}-{self.end_label}期间可执行。",
            )
        if now_time <= end_time:
            return True, "immediate", "immediate", f"{self.start_label}-{self.end_label}窗口内可执行。"
        return False, "backup_only", "backup_only", f"{self.end_label}后仅观察。"


class TodayBackupThresholdPolicy:
    """Resolve the today-term backup observation floor by regime and row risk."""

    def __init__(self, raw=None, experiments=None) -> None:
        self.plan = self._load_plan(raw, experiments)

    def _load_plan(self, raw=None, experiments=None) -> Dict[str, object]:
        if raw is None:
            raw = getattr(config, "TODAY_BACKUP_MIN_SCORE", 45.0)
        base = coerce_number(raw if not isinstance(raw, dict) else raw.get("base"), 45.0)
        if not isinstance(raw, dict):
            return {
                "base": base,
                "by_regime": {},
                "experiments": [],
                "dynamic": {
                    "volatility_20d": {
                        "high_threshold": 12.0,
                        "high_delta": 3.0,
                        "low_threshold": 6.0,
                        "low_delta": -1.0,
                    },
                    "turnover_rate": {
                        "high_threshold": 18.0,
                        "high_delta": 2.0,
                        "mid_threshold": 10.0,
                        "mid_delta": 1.0,
                        "low_threshold": 5.0,
                        "low_delta": -0.5,
                    },
                    "order_imbalance": {
                        "high_threshold": 8.0,
                        "high_delta": 2.0,
                        "mid_threshold": 4.0,
                        "mid_delta": 1.0,
                    },
                },
            }
        profile = dict(raw or {})
        by_regime = profile.get("by_regime") if isinstance(profile.get("by_regime"), dict) else {}
        dynamic = profile.get("dynamic") if isinstance(profile.get("dynamic"), dict) else {}
        experiment_rows = profile.get("experiments")
        if not isinstance(experiment_rows, list):
            experiment_rows = experiments if isinstance(experiments, list) else []
            if not experiment_rows:
                fallback = getattr(config, "TODAY_BACKUP_MIN_SCORE_EXPERIMENTS", None)
                experiment_rows = fallback if isinstance(fallback, list) else []
        normalized_experiments: List[Dict[str, object]] = []
        for exp in experiment_rows:
            if not isinstance(exp, dict):
                continue
            exp_regimes = (
                exp.get("regime")
                if exp.get("regime") is not None
                else exp.get("regimes")
                if exp.get("regimes") is not None
                else exp.get("market_regime")
                if exp.get("market_regime") is not None
                else exp.get("market_regimes")
            )
            normalized_experiments.append(
                {
                    "name": str(exp.get("name") or exp.get("id") or "experiment"),
                    "base": coerce_number(exp.get("base"), base),
                    "dynamic": exp.get("dynamic") if isinstance(exp.get("dynamic"), dict) else dynamic,
                    "regime_aliases": collect_regime_aliases(exp_regimes),
                }
            )
        return {
            "base": coerce_number(profile.get("base"), base),
            "by_regime": by_regime,
            "experiments": normalized_experiments,
            "dynamic": dynamic,
        }

    def resolve(self, market_regime: Dict[str, object]) -> Dict[str, object]:
        plan = self.plan
        base = coerce_number(plan.get("base"), 45.0)
        dynamic = plan.get("dynamic") if isinstance(plan.get("dynamic"), dict) else {}
        source = "base"
        used_experiment = False
        key = _to_lower(
            (market_regime or {}).get("level")
            or (market_regime or {}).get("label")
            or (market_regime or {}).get("state")
            or ""
        )
        experiments = plan.get("experiments")
        if isinstance(experiments, list):
            aliases = set(normalize_regime_aliases(key))
            for experiment in experiments:
                exp_aliases = experiment.get("regime_aliases")
                if aliases and isinstance(exp_aliases, list) and aliases.intersection(exp_aliases):
                    base = coerce_number(experiment.get("base"), base)
                    exp_dynamic = experiment.get("dynamic")
                    if isinstance(exp_dynamic, dict):
                        dynamic = exp_dynamic
                    source = str(experiment.get("name") or "experiment")
                    used_experiment = True
                    break
        by_regime = plan.get("by_regime")
        if not used_experiment and isinstance(by_regime, dict) and key:
            found_regime_base = None
            for alias in normalize_regime_aliases(key):
                if alias in by_regime:
                    found_regime_base = coerce_number(by_regime.get(alias))
                    break
            if found_regime_base is None and "balanced" in by_regime:
                found_regime_base = coerce_number(by_regime.get("balanced"))
            if found_regime_base is not None:
                base = found_regime_base
                source = "by_regime"
            if base < 0:
                base = coerce_number(plan.get("base"), 45.0)
                source = "base"
        return {"base": base, "dynamic": dynamic, "source": source}

    def row_min_score(self, row: Dict[str, object], backup_profile: Dict[str, object], min_score: float) -> float:
        dynamic = backup_profile.get("dynamic") if isinstance(backup_profile.get("dynamic"), dict) else {}
        threshold = coerce_number(backup_profile.get("base"), 45.0)
        threshold += self._rule_adjustment(coerce_number(row.get("volatility_20d")), dynamic.get("volatility_20d"))
        threshold += self._rule_adjustment(coerce_number(row.get("turnover_rate")), dynamic.get("turnover_rate"))
        threshold += self._rule_adjustment(
            coerce_number(row.get("order_imbalance")),
            dynamic.get("order_imbalance"),
            use_abs=True,
        )
        threshold = max(0.0, min(100.0, threshold))
        if threshold >= min_score:
            threshold = max(0.0, min_score - 0.1)
        return round(threshold, 2)

    @staticmethod
    def _rule_adjustment(value: float, cfg: Dict[str, object], *, use_abs: bool = False) -> float:
        if use_abs:
            value = abs(value)
        if not isinstance(cfg, dict):
            return 0.0
        value = coerce_number(value)
        high_threshold = cfg.get("high_threshold")
        mid_threshold = cfg.get("mid_threshold")
        low_threshold = cfg.get("low_threshold")
        if high_threshold is not None and value >= coerce_number(high_threshold):
            return coerce_number(cfg.get("high_delta"), 0.0)
        if mid_threshold is not None and value >= coerce_number(mid_threshold):
            return coerce_number(cfg.get("mid_delta"), 0.0)
        if low_threshold is not None and value <= coerce_number(low_threshold):
            return coerce_number(cfg.get("low_delta"), 0.0)
        return 0.0


class IndustryDiversificationPolicy:
    """Apply a per-industry display cap while preserving incoming row order."""

    def __init__(self, key_fn: Callable[[Dict[str, object]], str] = None) -> None:
        self.key_fn = key_fn or self.default_key

    @staticmethod
    def default_key(row: Dict[str, object]) -> str:
        return _to_lower(row.get("industry") or "")

    def distribution(self, rows: List[Dict[str, object]]) -> Dict[str, int]:
        distribution: Dict[str, int] = {}
        for row in rows or []:
            key = self.key_fn(row)
            distribution[key] = distribution.get(key, 0) + 1
        return distribution

    def select(
        self,
        rows: List[Dict[str, object]],
        *,
        limit: int,
        cap: int,
    ) -> Tuple[List[Dict[str, object]], Dict[str, int], int]:
        display_limit = max(0, int(limit or 0))
        cap_value = int(cap or 0)
        if display_limit <= 0:
            return [], {}, 0
        if cap_value <= 0:
            selected = list(rows or [])[:display_limit]
            return selected, self.distribution(selected), 0

        selected: List[Dict[str, object]] = []
        industry_counts: Dict[str, int] = {}
        industry_limited_count = 0
        for row in rows or []:
            if len(selected) >= display_limit:
                break
            key = self.key_fn(row)
            if industry_counts.get(key, 0) >= cap_value:
                industry_limited_count += 1
                continue
            selected.append(row)
            industry_counts[key] = industry_counts.get(key, 0) + 1
        return selected, industry_counts, industry_limited_count
