from datetime import date
from tests.helpers.recommendation.market_data import (
    FEATURE_WEIGHT_POLICY,
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    NOW,
    TAIL_POLICY,
    CountingHistoryClient,
    FeatureBuilder,
    StaticGateway,
    _history_bars,
    _quote,
    _service,
)
from tests.helpers.shared.reference_fixtures import PreparedFixture
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.recommendation.infra.market_data.published_references import PublishedReferences


def _population():
    return (
        BaoStockSecurity("300001", "证券300001", "chinext", date(2020, 1, 2), None, "fixture"),
        BaoStockSecurity("600001", "证券600001", "main", date(2020, 1, 2), None, "fixture"),
    )


def _reader(state):
    return PublishedReferences(
        lambda: state["reference"],
        lambda: state["reference"].identity if state["reference"] else None,
        lambda _: (),
        max_age_seconds=86400,
    )


def _fixture(*, quotes=None, missing=False, population=None, **options):
    state = {"reference": None if missing else PreparedFixture(NOW, population or _population())}
    gateway = StaticGateway((_quote(),) if quotes is None else quotes)
    history = CountingHistoryClient(_history_bars())
    service = _service(
        gateway,
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        published_references=options.pop("published_references", _reader(state)),
        **options,
    )
    return state, gateway, history, service
