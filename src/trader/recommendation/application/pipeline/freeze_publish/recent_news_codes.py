"""Read the bounded published population authorized for company news."""

from datetime import datetime, timedelta

from trader.recommendation.application.ports.freeze_records import FreezeRecordPort, FreezeRecordUnavailableError
from trader.recommendation.domain.publication.freeze_identity import DisplayQuoteCodes
from trader.recommendation.domain.publication.models import Strategy


def RecentPublishedNewsCodes(records: tuple[FreezeRecordPort, ...], observed_at: datetime) -> tuple[str, ...]:
    if observed_at.tzinfo is None:
        raise ValueError("news population observation requires a timezone")
    first_date = observed_at.date() - timedelta(days=6)
    codes: set[str] = set()
    for source in records:
        for strategy in (Strategy.TOMORROW, Strategy.D25):
            try:
                dates = source.list_dates(strategy, limit=7)
            except FreezeRecordUnavailableError:
                continue
            for day in dates:
                if not first_date <= day <= observed_at.date():
                    continue
                try:
                    record = source.load(strategy, day)
                except FreezeRecordUnavailableError:
                    continue
                if record is not None and record.committed_at <= observed_at:
                    codes.update(DisplayQuoteCodes(record.freeze))
    return tuple(sorted(codes))


__all__ = ["RecentPublishedNewsCodes"]
