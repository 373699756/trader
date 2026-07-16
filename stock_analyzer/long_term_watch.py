from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple

import pandas as pd

from .normalization import coerce_number, normalize_code
from .deepseek.production_merge import attach_and_merge_rows, select_top_rows
from .scoring_core.theme_scores import CHOKEPOINT_INDUSTRY_LEADERS, _chain_segment, _chokepoint_score


STRATEGIC_WORDS = (
    "卡脖子",
    "国产替代",
    "自主",
    "国产化",
    "政策",
    "扶持",
    "专项",
    "战略",
    "产业链安全",
    "供应链安全",
    "信创",
    "工业母机",
    "半导体",
    "高端装备",
    "机器人",
    "算力",
    "航天",
    "卫星",
)


@dataclass(frozen=True)
class LongTermScore:
    valuation_score: float
    leader_score: float
    strategic_score: float
    growth_quality_score: float
    composite_score: float
    strategic_hits: Tuple[str, ...]
    strategic_segment: str
    leader_reason: str
    risk_score: float
    eligible: bool
    fallback_eligible: bool
    blockers: Tuple[str, ...]


class LongTermCandidateSource:
    """Build a deduplicated long-term watch universe from strategy rows and broad candidates."""

    def rows(self, recommendations: Dict[str, object], candidates) -> List[Dict[str, object]]:
        rows: List[Dict[str, object]] = []
        for strategy in ("today_term", "tomorrow_picks", "swing_picks"):
            strategy_rows = recommendations.get(strategy) if isinstance(recommendations, dict) else []
            if isinstance(strategy_rows, list):
                rows.extend(dict(row, _long_term_source=strategy) for row in strategy_rows if isinstance(row, dict))
        if candidates is not None and not getattr(candidates, "empty", True):
            for row in candidates.to_dict(orient="records"):
                rows.append(dict(row, _long_term_source="candidate_pool"))
        return self._dedupe(rows)

    def _dedupe(self, rows: Iterable[Dict[str, object]]) -> List[Dict[str, object]]:
        by_code: Dict[str, Dict[str, object]] = {}
        for row in rows or []:
            code = normalize_code(row.get("code"))
            if not code:
                continue
            item = dict(row)
            item["code"] = code
            current = by_code.get(code)
            if current is None:
                by_code[code] = item
            elif self._source_rank(item) > self._source_rank(current):
                by_code[code] = self._merge_rows(item, current)
            else:
                by_code[code] = self._merge_rows(current, item)
        return list(by_code.values())

    @staticmethod
    def _source_rank(row: Dict[str, object]) -> Tuple[float, float, float]:
        source = str(row.get("_long_term_source") or "")
        source_bonus = {"today_term": 3, "tomorrow_picks": 3, "swing_picks": 3}.get(source, 1)
        return (
            source_bonus,
            coerce_number(row.get("score")),
            coerce_number(row.get("fundamental_value_score")),
        )

    @staticmethod
    def _merge_rows(primary: Dict[str, object], fallback: Dict[str, object]) -> Dict[str, object]:
        merged = dict(primary)
        for key, value in (fallback or {}).items():
            if key not in merged or merged.get(key) in (None, "", 0, 0.0):
                merged[key] = value
        return merged


class LongTermProfileBuilder:
    """Score low-valuation strategic leaders for a non-trading long-term watch list."""

    weights = {
        "valuation": 0.35,
        "leader": 0.25,
        "strategic": 0.25,
        "growth_quality": 0.15,
    }

    def __init__(self, universe_rows: Iterable[Dict[str, object]]) -> None:
        rows = list(universe_rows or [])
        self._market_caps = sorted(
            value for value in (coerce_number(row.get("market_cap") or row.get("float_market_cap"), None) for row in rows)
            if value is not None and value > 0
        )
        self._leader_codes = {
            normalize_code(item.get("code")): segment
            for segment, items in CHOKEPOINT_INDUSTRY_LEADERS.items()
            for item in items
            if normalize_code(item.get("code"))
        }

    def build(self, row: Dict[str, object]) -> LongTermScore:
        valuation = self._valuation_score(row)
        leader, leader_reason = self._leader_score(row)
        strategic, strategic_hits, segment = self._strategic_score(row)
        growth = self._growth_quality_score(row)
        overheated_penalty = self._overheated_penalty(row)
        composite = max(
            0.0,
            valuation * self.weights["valuation"]
            + leader * self.weights["leader"]
            + strategic * self.weights["strategic"]
            + growth * self.weights["growth_quality"]
            - overheated_penalty,
        )
        risk_score = self._risk_score(row)
        blockers = self._blockers(row, valuation, leader, strategic, growth, risk_score)
        return LongTermScore(
            valuation_score=round(valuation, 4),
            leader_score=round(leader, 4),
            strategic_score=round(strategic, 4),
            growth_quality_score=round(growth, 4),
            composite_score=round(composite, 4),
            strategic_hits=tuple(strategic_hits),
            strategic_segment=segment,
            leader_reason=leader_reason,
            risk_score=round(risk_score, 4),
            eligible=not blockers and composite >= 0.58,
            fallback_eligible=not self._hard_risk_blockers(row, risk_score)
            and valuation >= 0.55
            and strategic >= 0.45
            and composite >= 0.42,
            blockers=tuple(blockers),
        )

    def _valuation_score(self, row: Dict[str, object]) -> float:
        direct = coerce_number(row.get("fundamental_value_score"), None)
        if direct is not None and direct > 0:
            return max(0.0, min(1.0, direct / 100.0))
        pe = coerce_number(row.get("pe_dynamic") or row.get("pe"), None)
        pb = coerce_number(row.get("pb"), None)
        pe_score = 0.5 if pe is None or pe <= 0 else max(0.0, min(1.0, (60.0 - pe) / 45.0))
        pb_score = 0.5 if pb is None or pb <= 0 else max(0.0, min(1.0, (8.0 - pb) / 6.8))
        return pe_score * 0.55 + pb_score * 0.45

    def _leader_score(self, row: Dict[str, object]) -> Tuple[float, str]:
        code = normalize_code(row.get("code"))
        if code in self._leader_codes:
            return 1.0, "内置战略产业龙头名单"
        cap = coerce_number(row.get("market_cap") or row.get("float_market_cap"), None)
        cap_score = self._percentile(cap, self._market_caps) if cap is not None else 0.45
        quality = coerce_number(row.get("fundamental_quality_score"), None)
        quality_score = max(0.0, min(1.0, quality / 100.0)) if quality is not None and quality > 0 else 0.5
        roe = coerce_number(row.get("roe"), None)
        roe_score = max(0.0, min(1.0, roe / 25.0)) if roe is not None else 0.5
        score = cap_score * 0.45 + quality_score * 0.35 + roe_score * 0.20
        return score, "市值/质量/ROE龙头代理"

    def _strategic_score(self, row: Dict[str, object]) -> Tuple[float, List[str], str]:
        code = normalize_code(row.get("code"))
        leader_segment = self._leader_codes.get(code)
        if leader_segment:
            return 1.0, [leader_segment], leader_segment
        series = pd.Series(row)
        chokepoint_score, hits = _chokepoint_score(series)
        text = self._text_bag(row)
        word_hits = [word for word in STRATEGIC_WORDS if word.lower() in text]
        all_hits = list(dict.fromkeys([*hits, *word_hits]))
        if not all_hits:
            return 0.2, [], ""
        text_bonus = min(0.25, len(word_hits) * 0.05)
        score = max(chokepoint_score / 100.0, 0.55) + text_bonus
        return max(0.0, min(1.0, score)), all_hits[:5], _chain_segment(all_hits)

    def _growth_quality_score(self, row: Dict[str, object]) -> float:
        revenue = coerce_number(row.get("revenue_yoy"), None)
        profit = coerce_number(row.get("net_profit_yoy"), None)
        quality = coerce_number(row.get("fundamental_quality_score"), None)
        cashflow = coerce_number(row.get("operating_cashflow_yoy"), None)
        parts = []
        if revenue is not None:
            parts.append(max(0.0, min(1.0, (revenue + 10.0) / 60.0)))
        if profit is not None:
            parts.append(max(0.0, min(1.0, (profit + 10.0) / 80.0)))
        if cashflow is not None:
            parts.append(max(0.0, min(1.0, (cashflow + 10.0) / 80.0)))
        if quality is not None and quality > 0:
            parts.append(max(0.0, min(1.0, quality / 100.0)))
        return sum(parts) / len(parts) if parts else 0.5

    def _risk_score(self, row: Dict[str, object]) -> float:
        for path in (
            row.get("sell_risk", {}).get("score") if isinstance(row.get("sell_risk"), dict) else None,
            row.get("serenity_profile", {}).get("risk_score") if isinstance(row.get("serenity_profile"), dict) else None,
            row.get("avg_risk"),
        ):
            value = coerce_number(path, None)
            if value is not None:
                return value
        return 50.0

    def _overheated_penalty(self, row: Dict[str, object]) -> float:
        sixty = coerce_number(row.get("sixty_day_pct"), None)
        ytd = coerce_number(row.get("ytd_pct"), None)
        penalty = 0.0
        if sixty is not None and sixty > 80:
            penalty += 0.15
        elif sixty is not None and sixty > 55:
            penalty += 0.08
        if ytd is not None and ytd > 160:
            penalty += 0.15
        elif ytd is not None and ytd > 100:
            penalty += 0.08
        return penalty

    def _blockers(
        self,
        row: Dict[str, object],
        valuation: float,
        leader: float,
        strategic: float,
        growth: float,
        risk_score: float,
    ) -> List[str]:
        blockers = self._hard_risk_blockers(row, risk_score)
        if valuation < 0.55:
            blockers.append("估值不够低或不够合理")
        if strategic < 0.45:
            blockers.append("缺少卡脖子/国产替代/政策扶持线索")
        if leader < 0.50:
            blockers.append("龙头或准龙头证据不足")
        if growth < 0.35:
            blockers.append("成长质量线索偏弱")
        return blockers

    def _hard_risk_blockers(self, row: Dict[str, object], risk_score: float) -> List[str]:
        blockers = []
        if risk_score > 90:
            blockers.append("风险评分过高")
        pe = coerce_number(row.get("pe_dynamic") or row.get("pe"), None)
        pb = coerce_number(row.get("pb"), None)
        if pe is not None and pe > 120:
            blockers.append("PE极端高估")
        if pb is not None and pb > 18:
            blockers.append("PB极端高估")
        if coerce_number(row.get("fundamental_quality_score"), 50.0) < 20:
            blockers.append("基本面质量过低")
        return blockers

    @staticmethod
    def _text_bag(row: Dict[str, object]) -> str:
        values = [
            row.get("name"),
            row.get("industry"),
            row.get("theme"),
            row.get("sub_theme"),
            row.get("reason"),
            row.get("summary"),
            row.get("note"),
        ]
        deepseek = row.get("deepseek_features") if isinstance(row.get("deepseek_features"), dict) else {}
        values.extend([deepseek.get("event_type"), deepseek.get("reason"), deepseek.get("evidence_summary")])
        values.extend(row.get("reasons") or [] if isinstance(row.get("reasons"), list) else [])
        return " ".join(str(value).lower() for value in values if value not in (None, ""))

    @staticmethod
    def _percentile(value: float, values: List[float]) -> float:
        if value is None or not values:
            return 0.5
        below = sum(1 for item in values if item <= value)
        return max(0.0, min(1.0, below / len(values)))


class LongTermWatchScorer:
    """Create the long-term observation list without changing executable strategies."""

    def __init__(self, source: LongTermCandidateSource | None = None) -> None:
        self.source = source or LongTermCandidateSource()

    def score(
        self,
        recommendations: Dict[str, object],
        candidates,
        top_n: int,
        validation_store=None,
    ) -> List[Dict[str, object]]:
        universe = self.source.rows(recommendations, candidates)
        builder = LongTermProfileBuilder(universe)
        scored = []
        for row in universe:
            score = builder.build(row)
            if score.eligible or score.fallback_eligible:
                enriched = self._enrich(row, score, eligible=score.eligible)
                scored.append(enriched)
        scored.sort(key=self._sort_key)
        if validation_store is None:
            return scored[: max(0, int(top_n or 0))]
        merged = attach_and_merge_rows(scored, "long_term_watch", validation_store)
        return select_top_rows(merged, "long_term_watch", top_n, include_veto_observations=False)

    def _enrich(self, row: Dict[str, object], score: LongTermScore, *, eligible: bool) -> Dict[str, object]:
        profile = {
            "valuation_score": score.valuation_score,
            "leader_score": score.leader_score,
            "strategic_score": score.strategic_score,
            "growth_quality_score": score.growth_quality_score,
            "long_term_potential": score.composite_score,
            "strategic_hits": list(score.strategic_hits),
            "strategic_segment": score.strategic_segment,
            "leader_reason": score.leader_reason,
            "risk_score": score.risk_score,
            "eligible": eligible,
            "blockers": list(score.blockers),
        }
        reasons = [
            "估值低估/合理" if score.valuation_score >= 0.55 else "",
            score.leader_reason,
            score.strategic_segment or "战略产业线索",
        ]
        item = dict(row)
        item["strategy_name"] = "long_term_watch"
        item["strategy_label"] = "长期"
        item["tier"] = "watch_pool" if eligible else "fallback_watch"
        item["tier_label"] = "长期观察" if eligible else "长期回退观察"
        item["execution_allowed"] = False
        item["trade_action"] = {"action": "watch", "position_size": 0.0, "reason": "长期观察池不产生交易动作"}
        item["long_term_profile"] = profile
        item["longTermProfile"] = {
            "valueScore": score.valuation_score,
            "leaderScore": score.leader_score,
            "strategicScore": score.strategic_score,
            "growthQualityScore": score.growth_quality_score,
            "longTermPotential": score.composite_score,
            "strategicHits": list(score.strategic_hits),
            "strategicSegment": score.strategic_segment,
            "riskScore": score.risk_score,
        }
        item["reasons"] = [reason for reason in [*reasons, *(item.get("reasons") or [])] if reason]
        item["long_term_blockers"] = list(score.blockers)
        item["expected_return_net"] = None
        item["predicted_net_return"] = None
        item["ranking_source"] = "long_term_composite_score"
        item["score"] = round(score.composite_score * 100.0, 2)
        item["local_score"] = item["score"]
        return item

    @staticmethod
    def _sort_key(row: Dict[str, object]) -> Tuple[float, float, float, float]:
        profile = row.get("long_term_profile") if isinstance(row.get("long_term_profile"), dict) else {}
        return (
            -coerce_number(profile.get("long_term_potential")),
            -coerce_number(profile.get("valuation_score")),
            -coerce_number(profile.get("leader_score")),
            coerce_number(profile.get("risk_score"), 100.0),
        )
