"""Same-source Tencent updates retain real raw facts without fabricating history."""

from dataclasses import replace
import pytest

from tests.helpers.download.history.qfq_windows import DAYS, Supplier, _updater
from trader.download.application.update_qfq import _DownloadJob
from trader.download.domain.history_price_qualification import RetainTencentRawFacts
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.recommendation.domain.outcomes.models import OutcomeTradingStatus
from trader.recommendation.infra.market_data.published_history_bars import OutcomeBars


class _OnePriceSupplier(Supplier):
    sdk = "tencent-newfqkline"
    rebased = False

    def load_qfq_context(self, as_of, sessions):
        context = super().load_qfq_context(as_of, sessions)
        return replace(context, source_versions=replace(context.source_versions, sdk_version=self.sdk))

    def fetch_window(self, security, dates, **kwargs):
        window = super().fetch_window(security, dates, **kwargs)
        cells = []
        for cell in window.cells:
            if cell.trade_date == DAYS[-2]:
                cell = replace(
                    cell,
                    unadjusted=replace(
                        cell.unadjusted, open_price=9.0, high_price=9.0, low_price=9.0, close_price=9.0,
                        preclose=None, pct_change=None,
                    ),
                    qfq=replace(cell.qfq, open_price=9.0, high_price=9.0, low_price=9.0, close_price=9.0),
                )
            if self.rebased:
                cell = replace(
                    cell,
                    qfq=replace(
                        cell.qfq, open_price=cell.qfq.open_price / 2, high_price=cell.qfq.high_price / 2,
                        low_price=cell.qfq.low_price / 2, close_price=cell.qfq.close_price / 2,
                    ),
                )
            cells.append(cell)
        return replace(window, cells=tuple(cells))


def _known_window(supplier):
    context = supplier.load_qfq_context(DAYS[-1], 251)
    window = supplier.fetch_window(context.universe[0], DAYS[:-1])
    cells = tuple(
        replace(cell, unadjusted=replace(cell.unadjusted, preclose=10.0, pct_change=-10.0))
        if cell.trade_date == DAYS[-2] else cell for cell in window.cells
    )
    return replace(window, cells=cells), context


@pytest.mark.parametrize("rebased", (False, True))
def test_market_increment_and_whole_qfq_refresh_retain_proven_tencent_raw_facts(tmp_path, rebased):
    supplier = _OnePriceSupplier()
    previous, context = _known_window(supplier)
    updater = _updater(tmp_path, supplier)
    source = f"{supplier.sdk}:{context.source_versions.content_hash}"
    supplier.calls.clear()
    supplier.rebased = rebased

    downloaded = updater._download(
        _DownloadJob(context.universe[0], DAYS[-251:], previous, source, observed_on=DAYS[-1])
    )

    assert supplier.calls[0][1] == DAYS[-4:]
    assert len(supplier.calls) == (2 if rebased else 1)
    window = downloaded.window
    cell = next(cell for cell in window.cells if cell.trade_date == DAYS[-2])
    assert cell.unadjusted.preclose == 10.0 and cell.unadjusted.pct_change == -10.0
    assert cell.qfq.close_price == (4.5 if rebased else 9.0)
    assert OutcomeBars((cell,))[0].trading_status is OutcomeTradingStatus.ONE_PRICE_LIMIT_DOWN
    assert previous.cells[-1].qfq.close_price == 9.0


@pytest.mark.parametrize("sdk", ("fixture", "tencent_baostock", "baostock"))
def test_market_unknown_or_other_source_does_not_inherit_tencent_raw_facts(tmp_path, sdk):
    supplier = _OnePriceSupplier()
    supplier.sdk = sdk
    previous, context = _known_window(supplier)
    updater = _updater(tmp_path, supplier)
    supplier.calls.clear()

    downloaded = updater._download(_DownloadJob(
        context.universe[0], DAYS[-251:], previous, f"{sdk}:{context.source_versions.content_hash}",
        observed_on=DAYS[-1],
    ))

    assert len(supplier.calls) == 2
    cell = next(cell for cell in downloaded.window.cells if cell.trade_date == DAYS[-2])
    assert cell.unadjusted.preclose is None and cell.unadjusted.pct_change is None
    assert OutcomeBars((cell,))[0].trading_status is OutcomeTradingStatus.UNKNOWN


@pytest.mark.parametrize("field", ("open_price", "high_price", "low_price", "close_price", "volume", "amount"))
def test_changed_raw_observation_cannot_inherit_previous_auxiliary_facts(field):
    supplier = _OnePriceSupplier()
    previous, _ = _known_window(supplier)
    old = previous.cells[-1]
    raw = replace(old.unadjusted, preclose=None, pct_change=None)
    incoming = replace(old, unadjusted=replace(raw, **{field: getattr(raw, field) + 1.0}))

    result = RetainTencentRawFacts(previous, PublishedHistoryWindow(previous.code, (incoming,)))

    assert result.cells[0] == incoming
    assert result.cells[0].unadjusted.preclose is None
    assert result.cells[0].unadjusted.pct_change is None


def test_new_known_auxiliary_facts_and_nontrading_states_are_preserved_as_received():
    supplier = _OnePriceSupplier()
    previous, _ = _known_window(supplier)
    old = previous.cells[-1]
    corrected = replace(old, unadjusted=replace(old.unadjusted, preclose=11.0, pct_change=-18.18))
    supplied = PublishedHistoryWindow(previous.code, (corrected,))
    assert RetainTencentRawFacts(previous, supplied) == supplied
    for status in ("tencent_inferred_suspended", "unknown_missing"):
        incoming = PublishedHistoryWindow(previous.code, (replace(old, status=status, unadjusted=None, qfq=None),))
        assert RetainTencentRawFacts(previous, incoming) == incoming
    suspended = replace(
        old,
        status="supplier_marked_suspended",
        unadjusted=replace(
            old.unadjusted, trading_status="suspended", volume=0.0, amount=0.0,
            preclose=None, pct_change=None, turnover=None,
        ),
        qfq=replace(old.qfq, trading_status="suspended", volume=0.0, amount=0.0),
    )
    incoming = PublishedHistoryWindow(previous.code, (suspended,))
    assert RetainTencentRawFacts(previous, incoming) == incoming
