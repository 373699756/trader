"""Eastmoney spot protocol for diagnostics; never added to production routing."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from trader.infra.http_contracts import HttpTransport
from trader.recommendation.domain.market.models import MarketQuote
from trader.recommendation.infra.normalization.quote import BuildMarketQuote, MarketQuoteInput, ParseVolume, ToFloat


@dataclass(frozen=True)
class EastmoneySpotPage:
    quotes: tuple[MarketQuote, ...]
    total: int


class EastmoneySpotClient:
    PAGE_SIZE = 100

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def fetch_page(
        self, number: int, *, host: str, timeout: float, deadline: float, received_at: datetime
    ) -> EastmoneySpotPage:
        if host not in {"82.push2.eastmoney.com", "push2.eastmoney.com", "7.push2.eastmoney.com"}:
            raise ValueError("eastmoney_spot_host_invalid")
        response = self._transport.get(
            f"https://{host}/api/qt/clist/get",
            params={
                "pn": str(number),
                "pz": str(self.PAGE_SIZE),
                "po": "1",
                "np": "1",
                "fltt": "2",
                "invt": "2",
                "fid": "f12",
                "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23",
                "fields": "f2,f3,f5,f6,f7,f8,f10,f12,f14,f15,f16,f17,f18,f20,f100",
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            },
            timeout=timeout,
            deadline=deadline,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("diff"), list):
            raise ValueError("eastmoney malformed page")
        quotes = tuple(
            quote
            for row in data["diff"]
            if isinstance(row, dict) and (quote := ParseEastmoneySpotQuote(row, received_at)) is not None
        )
        return EastmoneySpotPage(quotes, int(data.get("total", 0)))


def ParseEastmoneySpotQuote(row: Mapping[str, object], received_at: datetime) -> MarketQuote | None:
    code = str(row.get("f12") or "")
    if len(code) != 6 or not code.isdigit():
        return None
    return BuildMarketQuote(
        MarketQuoteInput(
            code=code,
            name=str(row.get("f14") or ""),
            price=ToFloat(row.get("f2")),
            previous_close=ToFloat(row.get("f18")),
            open_price=ToFloat(row.get("f17")),
            high=ToFloat(row.get("f15")),
            low=ToFloat(row.get("f16")),
            pct_change=ToFloat(row.get("f3")),
            change_5m=ToFloat(row.get("f11")),
            speed=ToFloat(row.get("f22")),
            volume_ratio=ToFloat(row.get("f10")),
            turnover_rate=ToFloat(row.get("f8")),
            amount=ToFloat(row.get("f6")),
            volume=ParseVolume(row.get("f5"), shares_per_unit=100.0),
            amplitude=ToFloat(row.get("f7")),
            market_cap=ToFloat(row.get("f20")),
            industry=str(row.get("f100") or ""),
            source="eastmoney",
            source_time=received_at,
            received_time=received_at,
            data_version=f"eastmoney:{received_at.isoformat()}",
        )
    )


__all__ = ["EastmoneySpotClient", "EastmoneySpotPage", "ParseEastmoneySpotQuote"]
