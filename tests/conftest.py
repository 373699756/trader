from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import pytest
from xdist import is_xdist_worker

if TYPE_CHECKING:
    from _pytest.terminal import TerminalReporter
    from xdist.workermanage import WorkerController

    from trader.recommendation.application.pipeline.policy import RecommendationPolicy
    from trader.recommendation.domain.market.models import FeatureSnapshot

pytest_plugins = ("tests.helpers.recommendation.domain_fixtures",)

_TEST_DIRECTORY_MARKERS = frozenset({"unit", "component", "integration", "contract", "performance", "js"})
_BUSINESS_DIRECTORY_MARKERS = {
    "download": "history",
    "training": "train",
    "recommendation": "recommendation",
    "http_api": "recommendation",
    "web": "recommendation",
    "infra": "crosscut",
    "system": "crosscut",
    "tooling": "crosscut",
}
_SLOW_CATEGORIES = frozenset(
    {"slow_history", "slow_migration", "slow_runtime", "slow_supplier", "slow_static", "slow_training"}
)
_FORMAT_TOOLS_DESELECTED = pytest.StashKey[set[str]]()
_FORMAT_TOOLS_OUTPUT = "trader_format_tools_deselected"


class _XdistWorkerConfig(Protocol):
    workeroutput: dict[str, object]


class _ApplicationFeatureFactory(Protocol):
    def __call__(self, code: str, observed_at: datetime, *, industry: str = "工业") -> FeatureSnapshot: ...


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("Trader test authorization")
    group.addoption(
        "--run-format-tools",
        action="store_true",
        default=False,
        help="Run format_tools tests only after explicit authorization to execute formatting tools.",
    )


def _business_owner(relative_path: str) -> str:
    root = Path(relative_path).parts[0]
    owner = _BUSINESS_DIRECTORY_MARKERS.get(root)
    if owner is None:
        raise pytest.UsageError(f"Test has no registered business owner: tests/{relative_path}")
    return owner


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    excluded: list[pytest.Item] = []
    selected: list[pytest.Item] = []
    for item in items:
        relative = item.path.relative_to(Path(__file__).parent)
        category = relative.parts[1] if len(relative.parts) > 1 else ""
        if category not in _TEST_DIRECTORY_MARKERS:
            raise pytest.UsageError(f"Test has no registered test category: tests/{relative}")
        item.add_marker(category)
        item.add_marker(_business_owner(relative.as_posix()))
        if category == "performance" or any(item.get_closest_marker(marker) for marker in _SLOW_CATEGORIES):
            item.add_marker("slow")
        if item.get_closest_marker("format_tools") is not None and not config.getoption("run_format_tools"):
            excluded.append(item)
        else:
            selected.append(item)
    if excluded:
        config.stash.setdefault(_FORMAT_TOOLS_DESELECTED, set()).update(item.nodeid for item in excluded)
        config.hook.pytest_deselected(items=excluded)
        items[:] = selected


def pytest_sessionfinish(session: pytest.Session) -> None:
    if is_xdist_worker(session):
        worker_config = cast(_XdistWorkerConfig, session.config)
        excluded_nodeids: set[str] = session.config.stash.get(_FORMAT_TOOLS_DESELECTED, set())
        worker_config.workeroutput[_FORMAT_TOOLS_OUTPUT] = sorted(excluded_nodeids)


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: WorkerController, error: object) -> None:
    if error is not None:
        return
    excluded = node.workeroutput.get(_FORMAT_TOOLS_OUTPUT, ())
    if not isinstance(excluded, (list, tuple)) or not all(isinstance(nodeid, str) for nodeid in excluded):
        raise pytest.UsageError("Invalid format_tools deselection report from pytest worker")
    node.config.stash.setdefault(_FORMAT_TOOLS_DESELECTED, set()).update(excluded)


def pytest_terminal_summary(terminalreporter: TerminalReporter, config: pytest.Config) -> None:
    count = len(config.stash.get(_FORMAT_TOOLS_DESELECTED, set()))
    if count:
        terminalreporter.write_line(
            f"{count} format_tools deselected: explicit --run-format-tools required; "
            "format-tool release checks were not run."
        )


@pytest.fixture
def recommendation_policy() -> RecommendationPolicy:
    from tests.helpers.recommendation.configuration import CompanyScoringPolicyFixture, HardFilterPolicyFixture
    from trader.recommendation.application.pipeline.policy import RecommendationPolicy, RecommendationSelectionSettings
    from trader.recommendation.domain.evidence.review import RiskRule
    from trader.recommendation.domain.publication.models import Strategy
    from trader.recommendation.domain.risk.fusion import DIMENSION_NAMES, FusionPolicy

    risk_rules = {
        "near_limit_crowding": RiskRule("near_limit_crowding", "medium", 5.0, 0.7, "market_crowding"),
        "price_volume_divergence": RiskRule("price_volume_divergence", "medium", 4.0, 0.7, "market_structure"),
        "high_volatility": RiskRule("high_volatility", "low", 3.0, 0.7, "market_structure"),
        "financial_deterioration": RiskRule("financial_deterioration", "high", 6.0, 0.7, "financial"),
    }
    return RecommendationPolicy(
        strategy_version="strategy-current",
        fusion_version="fusion-fixture",
        fusion=FusionPolicy(25.0),
        selection=RecommendationSelectionSettings(
            default_top_k=6,
            maximum_top_k=12,
            observation_margin=5.0,
            thresholds={"tomorrow": 72.0, "d25": 70.0},
            maximum_board_fraction=0.6,
            competition_group_limits={},
            minimum_board_reliability=0.85,
            review_candidate_limit=6,
        ),
        dimension_weights={
            strategy: {
                **{name: 0.25 for name in DIMENSION_NAMES if name != "industry_policy"},
                "industry_policy": 0.0,
            }
            for strategy in Strategy
        },
        risk_rules=risk_rules,
        board_policy_version="fixture",
        board_local_strategy_weights={},
        local_component_weights={},
        hard_filter=HardFilterPolicyFixture(),
        company_scoring=CompanyScoringPolicyFixture(),
    )


@pytest.fixture
def application_feature_factory() -> _ApplicationFeatureFactory:
    from trader.recommendation.domain.market.models import FeatureSnapshot, MarketQuote

    def build(code: str, observed_at: datetime, *, industry: str = "工业") -> FeatureSnapshot:
        quote = MarketQuote(
            code=code,
            name=f"测试{code}",
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
            industry=industry,
            source="fixture",
            source_time=observed_at,
            received_time=observed_at,
            data_version=f"fixture:{observed_at.isoformat()}",
        )
        values = {
            "amount_median_20d": 200_000_000.0,
            "amount_percentile_20d": 75.0,
            "speed_percentile": 75.0,
            "relative_strength_3d": 75.0,
            "relative_strength_5d": 75.0,
            "relative_strength_10d": 70.0,
            "relative_strength_20d": 70.0,
            "industry_strength": 70.0,
            "industry_breadth": 70.0,
            "industry_trend": 70.0,
            "news_sentiment": 60.0,
            "evidence_freshness": 70.0,
            "market_breadth": 60.0,
            "low_volatility_score": 70.0,
            "low_drawdown_score": 70.0,
            "low_crowding_score": 70.0,
            "volatility_20d": 2.0,
            "max_drawdown_20d": -8.0,
            "atr20_pct": 2.0,
            "ma5": 11.9,
            "ma10": 11.8,
            "ma20": 11.6,
            "ma20_slope_pct": 1.0,
            "volume_to_5d_average": 1.0,
            "prior_high_20d": 12.8,
            "breakout_deviation_pct": -6.25,
            "entry_quality": 70.0,
            "price_volume_confirmation": 70.0,
            "moderate_daily_return": 75.0,
            "ma20_60_position": 75.0,
            "ma20_60_structure": 75.0,
            "ma_slope": 70.0,
            "breakout_20d": 65.0,
            "risk_adjusted_return_20d": 70.0,
            "upward_consistency": 70.0,
            "capacity_score": 80.0,
            "moderate_amplitude": 75.0,
            "limit_distance_safety": 75.0,
            "tail_return_30m_pct": 0.8,
            "tail_return_30m": 70.0,
            "tail_volume_ratio_raw": 1.4,
            "tail_volume_ratio": 70.0,
            "close_location": 75.0,
            "price_executability": 75.0,
            "ma20_deviation_inverse": 70.0,
            "return_20d": 10.0,
            "trend_score": 70.0,
            "value_score": 70.0,
            "growth_score": 70.0,
            "quality_score": 70.0,
            "industry_policy_score": 70.0,
            "risk_protection_score": 70.0,
            "limit_proximity": 0.3,
            "price_volume_divergence": 0.0,
            "latest_disclosed_report_loss": 0.0,
            "financial_deterioration": 0.0,
            "reduction_or_unlock": 0.0,
            "shareholder_reduction_level": 0.0,
            "unlock_risk": 0.0,
            "pledge_risk": 0.0,
            "negative_announcement_level": 0.0,
            "major_shareholder_reduction": 0.0,
            "financial_fraud_history": 0.0,
            "official_investigation_history": 0.0,
            "major_illegal_history": 0.0,
            "fund_occupation_history": 0.0,
            "illegal_guarantee_history": 0.0,
            "forced_delisting_risk": 0.0,
            "corporate_risk_window_unavailable": 0.0,
            "trend_breakdown": 0.0,
            "short_term_overheat": 0.0,
            "intraday_reversal": 0.0,
        }
        return FeatureSnapshot(quote=quote, values=values, observed_at=observed_at, history_days=60)

    return build
