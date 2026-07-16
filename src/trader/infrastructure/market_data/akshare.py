"""Optional AKShare research evidence adapter."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from trader.domain.models import Evidence


class AkshareResearchClient:
    def __init__(self, module_loader: Callable[[], object] | None = None) -> None:
        self._module_loader = module_loader or _load_akshare

    def fetch_news(self, code: str, *, observed_at: datetime, limit: int = 5) -> tuple[Evidence, ...]:
        module = self._module_loader()
        function = getattr(module, "stock_news_em", None)
        if not callable(function):
            return ()
        frame = function(symbol=code)
        rows = frame.to_dict(orient="records") if hasattr(frame, "to_dict") else []
        evidence: list[Evidence] = []
        for index, row in enumerate(rows[: max(0, limit)]):
            if not isinstance(row, dict):
                continue
            title = _first_text(row, ("新闻标题", "标题", "title"))
            if not title:
                continue
            published = _parse_datetime(_first_text(row, ("发布时间", "时间", "publish_time")), observed_at)
            evidence.append(
                Evidence(
                    evidence_id=f"akshare-news:{code}:{published.isoformat()}:{index}"[:80],
                    evidence_type="news",
                    title=title[:240],
                    source=_first_text(row, ("文章来源", "来源", "source")) or "akshare",
                    published_at=published,
                )
            )
        return tuple(evidence)

    def fetch_financial_snapshot(self, code: str) -> Mapping[str, object]:
        module = self._module_loader()
        function = getattr(module, "stock_financial_analysis_indicator", None)
        if not callable(function):
            return {}
        frame = function(symbol=code)
        if not hasattr(frame, "to_dict") or getattr(frame, "empty", True):
            return {}
        rows = frame.to_dict(orient="records")
        return dict(rows[0]) if rows and isinstance(rows[0], dict) else {}


def _load_akshare() -> object:
    import akshare

    return akshare


def _first_text(row: Mapping[str, object], keys: Sequence[str]) -> str:
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _parse_datetime(raw: str, fallback: datetime) -> datetime:
    if not raw:
        return fallback
    try:
        parsed = datetime.fromisoformat(raw.replace("/", "-"))
    except ValueError:
        return fallback
    return parsed.replace(tzinfo=fallback.tzinfo) if parsed.tzinfo is None else parsed


__all__ = ["AkshareResearchClient"]
