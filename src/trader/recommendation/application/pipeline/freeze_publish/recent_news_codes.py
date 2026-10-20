"""Read the bounded published population authorized for company news."""

from datetime import datetime, timedelta

from trader.recommendation.application.ports.decision_records import DecisionRecordPort, DecisionRecordUnavailableError
from trader.recommendation.domain.publication.decision_identity import DisplayQuoteCodes
from trader.recommendation.domain.publication.models import Strategy


def RecentPublishedNewsCodes(records: tuple[DecisionRecordPort, ...], observed_at: datetime) -> tuple[str, ...]:
    if observed_at.tzinfo is None:
        raise ValueError("news population observation requires a timezone")
    first_date = observed_at.date() - timedelta(days=6)
    codes: set[str] = set()
    for source in records:
        for strategy in (Strategy.TOMORROW, Strategy.D25):
            try:
                dates = source.list_dates(strategy, limit=7)
            except DecisionRecordUnavailableError:
                continue
            for day in dates:
                if not first_date <= day <= observed_at.date():
                    continue
                try:
                    record = source.load(strategy, day)
                except DecisionRecordUnavailableError:
                    continue
                if record is not None and record.committed_at <= observed_at:
                    codes.update(DisplayQuoteCodes(record.decision))
    return tuple(sorted(codes))


__all__ = ["RecentPublishedNewsCodes"]
