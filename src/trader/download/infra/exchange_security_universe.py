"""Current supported A-share identities shared by history and qfq downloads."""

from __future__ import annotations

from typing import cast
from collections.abc import Callable
from trader.download.application.progress_message import ProgressField, ProgressLevel, ProgressMessage, ProgressTask

from trader.download.domain.baostock_daily import BaoStockBoard, BaoStockSecurity
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.domain.download_failure import DownloadValidationFailure
from trader.download.domain.security_eligibility import IsStSecurityName
from trader.download.infra.history_sync_failure import HistoryFailureDetail
from trader.infra.market_data.observations import ExchangeSecurityMasterListing, ListingFetcher


def LoadCurrentAShareUniverse(
    sse_fetcher: ListingFetcher, szse_fetcher: ListingFetcher, timeout_seconds: float,
    *, source_label: str, report: Callable[[ProgressMessage], None] | None = None,
) -> tuple[BaoStockSecurity, ...]:
    if not source_label.strip():
        raise ValueError("security_universe_source_invalid")
    task = ProgressTask("参考", "universe", "股票总体")
    if report is not None:
        report(ProgressMessage((ProgressField(f"来源 {source_label}"),), task=task, state="开始"))
    listings: tuple[ExchangeSecurityMasterListing, ...] = ()
    for exchange, fetcher in (("SSE", sse_fetcher), ("SZSE", szse_fetcher)):
        if report is not None:
            report(ProgressMessage((ProgressField(f"正在获取 {exchange}"), ProgressField(f"来源 {source_label}")), task=task))
        try:
            fetched = tuple(fetcher(timeout_seconds))
        except (OSError, RuntimeError, ValueError) as exc:
            failure = DownloadValidationFailure(f"reference_{exchange.lower()}_listings_invalid", "response", "valid_listings", type(exc).__name__) if isinstance(exc, ValueError) else exc
            _report_failure(report, task, f"{source_label}/{exchange}", failure, cause=exc)
            if failure is exc:
                raise
            raise failure from exc
        listings += fetched
        if report is not None:
            report(ProgressMessage((ProgressField(f"已获取 {exchange} {len(fetched)} 条"), ProgressField(f"来源 {source_label}")), task=task))
    try:
        unique_count = len({item.code for item in listings})
        if len(listings) < 4000:
            raise DownloadValidationFailure("reference_universe_incomplete", "population", ">=4000", len(listings))
        if unique_count != len(listings):
            raise DownloadValidationFailure("reference_universe_duplicate", "unique_codes", len(listings), unique_count)
        if {item.exchange for item in listings} != {"SSE", "SZSE"}:
            raise DownloadValidationFailure("reference_universe_exchanges_invalid", "exchanges", "SSE,SZSE", "unexpected_set")
        if any(item.board not in ("main", "chinext", "star") for item in listings):
            raise DownloadValidationFailure("reference_universe_board_invalid", "board", "main,chinext,star", "unsupported")
        universe = tuple(
            BaoStockSecurity(
                item.code, item.name, cast(BaoStockBoard, item.board), item.listing_date, None, "exchange_security_master"
            )
            for item in sorted(listings, key=lambda item: item.code)
        )
    except ValueError as exc:
        failure = exc if isinstance(exc, DownloadValidationFailure) else DownloadValidationFailure(
            "reference_security_identity_invalid", "security", "valid_identity", type(exc).__name__,
        )
        _report_failure(report, task, source_label, failure, cause=exc)
        if failure is exc:
            raise
        raise failure from exc
    if report is not None:
        report(ProgressMessage((ProgressField(f"总体 {len(listings)} 股"), ProgressField(f"来源 {source_label}")),
                              task=task, state="完成", finished=True))
    return universe


def _report_failure(
    report: Callable[[ProgressMessage], None] | None,
    task: ProgressTask,
    source: str,
    failure: BaseException,
    *,
    cause: BaseException,
) -> None:
    if report is None:
        return
    differences = (
        ProgressField(f"字段 {failure.field}，要求 {failure.expected}，实际 {failure.actual}", ProgressLevel.ERROR),
    ) if isinstance(failure, DownloadValidationFailure) else ()
    reason = str(failure) if isinstance(failure, DownloadValidationFailure) else type(failure).__name__
    report(ProgressMessage(
        (ProgressField(f"原因 {reason}", ProgressLevel.ERROR), *differences,
         ProgressField(f"诊断 {HistoryFailureDetail(cause)}", ProgressLevel.ERROR), ProgressField(f"来源 {source}")),
        task=task, state="失败", finished=True,
    ))


def SelectStEligibleUniverse(
    universe: tuple[BaoStockSecurity, ...], evidence: tuple[HistoryStEvidence, ...]
) -> tuple[BaoStockSecurity, ...]:
    """Apply the current and historical ST rule to every download population."""

    official_codes = {security.code for security in universe}
    current = {item.code: item for item in evidence if item.code in official_codes}
    if not official_codes or set(current) != official_codes:
        raise RuntimeError("historical ST evidence is unavailable or conflicts with the official universe")
    eligible = frozenset(
        security.code
        for security in universe
        if current[security.code].status == "clear" and not IsStSecurityName(security.name)
    )
    if not eligible:
        raise RuntimeError("historical ST eligible universe is empty")
    return tuple(security for security in universe if security.code in eligible)


__all__ = ["LoadCurrentAShareUniverse", "SelectStEligibleUniverse"]
