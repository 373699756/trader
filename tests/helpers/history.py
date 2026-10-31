"""Explicit reference publication before isolated history synchronization fixtures."""

from tests.helpers.configuration import SqlitePolicyFixture

from dataclasses import dataclass, replace
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.prepared_reference import PreparedReference
from trader.download.infra.history_supplier_router import HistorySupplierRouter
from trader.download.infra.history_sync_runner import RunHistorySync
from trader.download.infra.reference_index import SQLiteReferenceIndex


@dataclass
class _PublishedSupplier:
    supplier: object
    reference_path: Path

    def load_context(self, as_of, sessions):
        context = self.supplier.load_context(as_of, sessions)
        if isinstance(self.supplier, HistorySupplierRouter):
            reference = self.supplier._reference
        else:
            population = {item.code: item for item in context.universe}
            for evidence in context.st_evidence:
                if evidence.code not in population:
                    population[evidence.code] = BaoStockSecurity(
                        evidence.code, "fixture excluded", "main", date(1990, 1, 1), None, "fixture"
                    )
            checked_on = max((item.checked_on for item in context.st_evidence), default=as_of)
            reference = PreparedReference(
                tuple(sorted(population.values(), key=lambda item: item.code)),
                context.st_evidence,
                datetime.combine(checked_on, time(20), ZoneInfo("Asia/Shanghai")),
                tuple(sorted({*context.calendar.open_dates, checked_on})),
            )
        SQLiteReferenceIndex(self.reference_path, sqlite_policy=SqlitePolicyFixture()).publish(reference, reference.st_evidence)
        return replace(context, reference_id=reference.identity)

    def fetch_tencent_window(self, security, dates):
        return self.supplier.fetch_tencent_window(security, dates)

    def fetch_baostock_window(self, security, dates):
        return self.supplier.fetch_baostock_window(security, dates)


def RunHistoryFixture(configuration, supplier, **options):
    return RunHistorySync(
        configuration,
        _PublishedSupplier(supplier, configuration.reference_root / "company_st.sqlite3"),
        **options, sqlite_policy=SqlitePolicyFixture(),
    )
