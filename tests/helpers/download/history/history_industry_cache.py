from datetime import date, datetime
from zoneinfo import ZoneInfo
from tests.helpers.download.history.baostock_gateway import _Result, _Sdk
from trader.download.domain.baostock_daily import BaoStockCalendar, BaoStockDailySpec, BaoStockSecurity
from trader.download.infra.baostock_gateway import BaoStockRowGateway
from trader.download.domain.industry_evidence import IndustryCategoryMapping, OfficialIndustryEvidence

DATES = (date(2020, 2, 3), date(2020, 7, 1), date(2021, 1, 4), date(2026, 10, 9))

UNIVERSE = (BaoStockSecurity("600001", "A", "main", date(2000, 1, 1), None, "test"),)


class _IndustrySdk(_Sdk):
    def __init__(self):
        self.calls = []
        self.fail_on = None
        self.empty = False
        self.future = False

    def query_stock_industry(self, *, code="", date=""):
        self.calls.append(date)
        if date == self.fail_on:
            raise RuntimeError("supplier_industry_timeout")
        return _Result(
            ("updateDate", "code", "industry", "industryClassification"),
            ()
            if self.empty
            else tuple(
                ("2099-01-01" if self.future else date, code, "bank", "test") for code in ("sh.600001", "sh.600002")
            ),
        )


def _fetch(sdk, cache, dates=DATES, universe=UNIVERSE, progress=None):
    return BaoStockRowGateway(sdk).fetch_industry_intervals(
        BaoStockDailySpec(sessions=len(dates)), BaoStockCalendar(dates), universe, cache=cache, progress=progress
    )


def _official_evidence():
    zone = ZoneInfo("Asia/Shanghai")
    return OfficialIndustryEvidence(
        "600003",
        "https://disc.static.szse.cn/fixture.pdf",
        "a" * 64,
        datetime(2026, 11, 2, 12, tzinfo=zone),
        date(2026, 11, 3),
        date(2026, 11, 6),
        datetime(2026, 11, 2, 18, tzinfo=zone),
        1,
        "CAPCO2023 C29 Plastics",
        IndustryCategoryMapping(
            "CAPCO2023", "C29", "Plastics", "C29Plastics", "https://www.csrc.gov.cn/fixture.pdf",
            "b" * 64, 1, "CSRC2012 C29Plastics", "Reviewed category scope in both standards.",
        ),
    )
