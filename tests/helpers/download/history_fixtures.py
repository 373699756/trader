"""Explicit reference publication before isolated history synchronization fixtures."""

from tests.helpers.shared.configuration import SqlitePolicyFixture

from dataclasses import dataclass, replace
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.prepared_reference import PreparedReference
from trader.download.infra.history_supplier_router import HistorySupplierRouter
from trader.bootstrap import ExecuteHistorySync
from trader.download.infra.history_sync_io import HistorySyncResources
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
    resources = HistorySyncResources(
        worker_pool=options.pop("worker_pool", None),
        writer_pool=options.pop("writer_pool", None),
        write_tuning=options.pop("write_tuning", None),
        disk_kind=options.pop("disk_kind", "unknown"),
        repair_only=options.pop("repair_only", False),
        price_review_budget=options.pop("price_review_budget", None),
        batched_price_reads=options.pop("batched_price_reads", False),
        exact_month_ranges=options.pop("exact_month_ranges", False),
        fingerprint_before_statistics=options.pop("fingerprint_before_statistics", False),
        reuse_catalog_fingerprint=options.pop("reuse_catalog_fingerprint", False),
        repair_codes=options.pop("repair_codes", None),
    )
    return ExecuteHistorySync(
        configuration,
        _PublishedSupplier(supplier, configuration.reference_root / "company_st.sqlite3"),
        **options,
        resources=resources,
        sqlite_policy=SqlitePolicyFixture(),
    )
