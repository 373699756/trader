"""Pure construction of the application recommendation policy."""

from __future__ import annotations

from trader.application.long_groups import LongGroupDefinition, LongGroupSectionDefinition, LongWatchItemDefinition
from trader.application.policy import RecommendationPolicy, SelectionPolicy
from trader.domain.market.models import Board
from trader.domain.recommendation.filtering.filters import HardFilterPolicy
from trader.domain.recommendation.models import Strategy
from trader.domain.recommendation.risk_fusion.fusion import FusionPolicy
from trader.domain.review.models import RiskRule
from trader.infra.settings import LongWatchlist, StrategySettings


def _recommendation_policy(settings: StrategySettings) -> RecommendationPolicy:
    return RecommendationPolicy(
        strategy_version=settings.strategy_version,
        fusion_version=settings.fusion.version,
        fusion=FusionPolicy(
            local_weight=settings.fusion.local_weight,
            deepseek_weight=settings.fusion.deepseek_weight,
            confidence_coverage_min=settings.fusion.confidence_coverage_min,
            minimum_known_dimensions=settings.fusion.minimum_known_dimensions,
            local_risk_cap=settings.fusion.local_risk_cap,
            deepseek_risk_cap=settings.fusion.deepseek_risk_cap,
        ),
        selection=SelectionPolicy(
            default_top_k=settings.selection.default_top_k,
            maximum_top_k=settings.selection.maximum_top_k,
            maximum_per_industry=settings.selection.maximum_per_industry,
            observation_margin=settings.selection.observation_margin,
            thresholds=settings.selection.thresholds,
            maximum_board_fraction=settings.selection.maximum_board_fraction,
            competition_group_limits={
                Board(name): limit for name, limit in settings.selection.competition_group_limits.items()
            },
            candidate_min_score=settings.selection.candidate_min_score,
            minimum_board_reliability=settings.selection.minimum_board_reliability,
            review_candidate_limit=settings.selection.review_candidate_limit,
        ),
        candidate_weights=settings.candidate_weights,
        dimension_weights={Strategy(name): weights for name, weights in settings.dimension_weights.items()},
        board_policy_version=settings.board_policy_version,
        board_candidate_weights={
            Strategy(strategy): {Board(board): weights for board, weights in boards.items()}
            for strategy, boards in settings.board_candidate_weights.items()
        },
        board_local_strategy_weights={
            Strategy(strategy): {Board(board): weights for board, weights in boards.items()}
            for strategy, boards in settings.board_local_strategy_weights.items()
        },
        risk_rules={
            rule.risk_code: RiskRule(
                risk_code=rule.risk_code,
                severity=rule.severity,
                penalty=rule.penalty,
                minimum_confidence=rule.minimum_confidence,
                group=rule.group,
                evidence_ttl_hours=rule.evidence_ttl_hours,
                veto=rule.veto,
                allowed_evidence_types=rule.allowed_evidence_types,
                strategies=rule.strategies,
                trigger_factor=rule.trigger_factor,
                trigger_operator=rule.trigger_operator,
                trigger_thresholds=rule.trigger_thresholds,
                combination_mode=rule.combination_mode,
                risk_fact_id_fields=rule.risk_fact_id_fields,
                local_trigger_enabled=rule.local_trigger_enabled,
            )
            for rule in settings.risk_rules
        },
        hard_filter=HardFilterPolicy(
            blacklist_codes=frozenset(settings.hard_filters.blacklist_codes),
            structured_risk_thresholds=settings.hard_filters.structured_risk_thresholds,
        ),
    )


def _long_item_definitions(watchlist: LongWatchlist) -> tuple[LongWatchItemDefinition, ...]:
    return tuple(LongWatchItemDefinition(item.code, item.name, item.industry) for item in watchlist.items)


def _long_group_definitions(watchlist: LongWatchlist) -> tuple[LongGroupDefinition, ...]:
    return tuple(
        LongGroupDefinition(
            name=group.name,
            category=group.category,
            codes=group.codes,
            source=group.source,
            source_section=group.source_section,
            sections=tuple(
                LongGroupSectionDefinition(section.source_section, section.codes) for section in group.sections
            ),
        )
        for group in watchlist.groups
    )


__all__ = ["_long_group_definitions", "_long_item_definitions", "_recommendation_policy"]
