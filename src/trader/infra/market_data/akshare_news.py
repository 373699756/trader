"""Bounded Eastmoney news request and normalization."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from trader.domain.market.models import Evidence
from trader.infra.market_data.akshare_parsing import (
    _clean_text,
    _content_version,
    _first_text,
    _news_rows,
    _parse_datetime,
    _point_in_time,
    _validate_code,
)


def fetch_news(client: Any, code: str, *, observed_at: datetime, limit: int = 5) -> tuple[Evidence, ...]:
    _validate_code(code)
    if limit <= 0:
        return ()
    point_in_time = _point_in_time(observed_at)
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
    response = client._request(
        "https://search-api-web.eastmoney.com/search/jsonp",
        params={
            "cb": callback,
            "param": json.dumps(inner_param, ensure_ascii=False),
            "_": "1764599530176",
        },
        headers={"Referer": f"https://so.eastmoney.com/news/s?keyword={code}"},
    )
    client._cache_payload("news", code, point_in_time, response.text)
    rows = _news_rows(response.text, callback)
    response_version = _content_version("eastmoney-news", response.text)
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
        source = _first_text(row, ("mediaName", "文章来源", "来源", "source")) or "eastmoney_news"
        identity = hashlib.sha256(f"{code}|{published.isoformat()}|{source}|{title}".encode()).hexdigest()[:32]
        evidence.append(
            Evidence(
                evidence_id=f"akshare-news:{code}:{identity}",
                evidence_type="news",
                title=title[:240],
                source=source[:60],
                published_at=published,
                received_at=point_in_time,
                data_version=response_version,
            )
        )
    return tuple(evidence)
