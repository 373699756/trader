"""Bounded AKShare-compatible research evidence adapter."""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Protocol, cast
from zoneinfo import ZoneInfo

import requests

from trader.domain.models import Evidence


class HttpResponse(Protocol):
    text: str

    def raise_for_status(self) -> None: ...


GetFunction = Callable[..., HttpResponse]
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class AkshareResearchClient:
    def __init__(
        self,
        module_loader: Callable[[], object] | None = None,
        *,
        timeout_seconds: float = 8.0,
        get: GetFunction | None = None,
    ) -> None:
        self._module_loader = module_loader or _load_akshare
        self._timeout_seconds = max(0.1, timeout_seconds)
        self._get = get if get is not None else cast(GetFunction, requests.get)

    def fetch_news(self, code: str, *, observed_at: datetime, limit: int = 5) -> tuple[Evidence, ...]:
        if limit <= 0:
            return ()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("news observation time must be timezone-aware")
        point_in_time = _as_shanghai_time(observed_at)
        callback = "jQuery35101792940631092459_1764599530165"
        inner_param = {
            "uid": "",
            "keyword": code,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": 10,
                    "preTag": "<em>",
                    "postTag": "</em>",
                }
            },
        }
        response = self._get(
            "https://search-api-web.eastmoney.com/search/jsonp",
            params={
                "cb": callback,
                "param": json.dumps(inner_param, ensure_ascii=False),
                "_": "1764599530176",
            },
            headers={"Referer": f"https://so.eastmoney.com/news/s?keyword={code}"},
            timeout=self._timeout_seconds,
            proxies={"http": "", "https": "", "all": ""},
        )
        response.raise_for_status()
        rows = _news_rows(response.text, callback)
        evidence: list[Evidence] = []
        for row in rows:
            if len(evidence) >= limit:
                break
            title = _clean_text(_first_text(row, ("title", "新闻标题", "标题")))
            if not title:
                continue
            published = _parse_datetime(_first_text(row, ("date", "发布时间", "时间", "publish_time")))
            if published is None or published > point_in_time:
                continue
            source = _first_text(row, ("mediaName", "文章来源", "来源", "source")) or "akshare"
            identity = hashlib.sha256(f"{code}|{published.isoformat()}|{source}|{title}".encode()).hexdigest()[:32]
            evidence.append(
                Evidence(
                    evidence_id=f"akshare-news:{code}:{identity}",
                    evidence_type="news",
                    title=title[:240],
                    source=source[:60],
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


def _news_rows(content: str, callback: str) -> list[Mapping[str, object]]:
    content = content.strip()
    if content.endswith(";"):
        content = content[:-1]
    prefix = f"{callback}("
    if not content.startswith(prefix) or not content.endswith(")"):
        raise RuntimeError("AKShare news response is not valid JSONP")
    try:
        payload = json.loads(content[len(prefix) : -1])
    except json.JSONDecodeError as exc:
        raise RuntimeError("AKShare news response contains invalid JSON") from exc
    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    rows = result.get("cmsArticleWebOld") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _clean_text(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _parse_datetime(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("/", "-").replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_shanghai_time(parsed)


def _as_shanghai_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI_TZ)
    return value.astimezone(SHANGHAI_TZ)


__all__ = ["AkshareResearchClient"]
