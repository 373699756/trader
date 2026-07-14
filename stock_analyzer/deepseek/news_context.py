from __future__ import annotations

from typing import Dict, List, Tuple

from ..normalization import coerce_number, normalize_code
from .cache import DeepSeekCache


_NEWS_CACHE = DeepSeekCache()


class NewsContextProvider:
    """Attaches cached news context to candidates before DeepSeek review."""

    ANNOUNCEMENT_KEYWORDS = (
        "业绩预告",
        "业绩预增",
        "业绩预亏",
        "减持",
        "增持",
        "解禁",
        "问询函",
        "监管函",
        "质押",
        "立案",
        "处罚",
        "诉讼",
        "并购",
        "重组",
        "中标",
        "订单",
    )

    def __init__(self, config_module, time_module, provider_factory, score_news_items) -> None:
        self.config = config_module
        self.time = time_module
        self.provider_factory = provider_factory
        self.score_news_items = score_news_items

    def enabled(self) -> bool:
        return bool(getattr(self.config, "ENABLE_DEEPSEEK_NEWS_CONTEXT", False))

    def attach(self, rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
        if not rows or not self.enabled():
            return rows
        try:
            provider = self.provider_factory()
            cache = self.read_cache()
            limit = max(1, int(getattr(self.config, "DEEPSEEK_NEWS_CONTEXT_LIMIT", 6)))
            enriched: List[Dict[str, object]] = []
            cache, changed = self.prune_cache(cache)
            for row in rows:
                item = dict(row)
                code = normalize_code(item.get("code"))
                if not code:
                    enriched.append(item)
                    continue
                cached = self.cached_context(cache.get(code), limit)
                if cached is None:
                    try:
                        news_items = provider.get_stock_news(code, name=str(item.get("name") or ""), limit=limit)
                    except Exception as exc:
                        news_items = []
                        item["news_context_status"] = "error"
                        item["news_context_error"] = str(exc)[:120]
                    scored = self.score_news_items(news_items)
                    cached = {
                        "fetched_at": self.time.time(),
                        "recent_news": self.compact_news_items(scored.get("items") or news_items, limit),
                        "news_sentiment": self.compact_news_sentiment(scored),
                    }
                    cache[code] = cached
                    changed = True
                item["recent_news"] = cached.get("recent_news", [])
                item["news_sentiment"] = cached.get("news_sentiment", {})
                item["announcement_flags"] = self.announcement_flags(
                    item,
                    item.get("recent_news") or [],
                    item.get("news_sentiment") or {},
                )
                item["news_context_status"] = item.get("news_context_status") or "ok"
                enriched.append(item)
            if changed:
                self.write_cache(cache)
            return enriched
        except Exception:
            return rows

    def cached_context(self, entry: object, limit: int):
        if not isinstance(entry, dict):
            return None
        fetched_at = coerce_number(entry.get("fetched_at"), 0.0)
        max_age = max(0, int(getattr(self.config, "NEWS_CACHE_HOURS", 6))) * 3600
        if max_age > 0 and (self.time.time() - fetched_at) > max_age:
            return None
        return {
            "fetched_at": fetched_at,
            "recent_news": list(entry.get("recent_news") or [])[:limit],
            "news_sentiment": dict(entry.get("news_sentiment") or {}),
        }

    def prune_cache(self, cache: Dict[str, object]) -> Tuple[Dict[str, object], bool]:
        if not isinstance(cache, dict) or not cache:
            return {}, False
        now = self.time.time()
        max_age = max(0, int(getattr(self.config, "NEWS_CACHE_HOURS", 6))) * 3600
        max_entries = max(0, int(getattr(self.config, "DEEPSEEK_NEWS_CACHE_MAX_ENTRIES", 1000)))
        items = []
        changed = False
        for code, entry in cache.items():
            if not isinstance(entry, dict):
                changed = True
                continue
            fetched_at = coerce_number(entry.get("fetched_at"), 0.0)
            if max_age > 0 and fetched_at > 0 and now - fetched_at > max_age:
                changed = True
                continue
            items.append((str(code), entry, fetched_at))
        if max_entries > 0 and len(items) > max_entries:
            items.sort(key=lambda item: item[2], reverse=True)
            items = items[:max_entries]
            changed = True
        return {code: entry for code, entry, _ in items}, changed

    def compact_news_items(self, items: List[Dict[str, object]], limit: int) -> List[Dict[str, object]]:
        compact = []
        for item in items[:limit]:
            title = str(item.get("title") or item.get("content") or "").strip()
            if not title:
                continue
            compact.append(
                {
                    "title": title[:60],
                    "source": str(item.get("source") or "")[:20],
                    "publish_time": str(item.get("publish_time") or item.get("time") or "")[:32],
                    "trigger_words": list(item.get("trigger_words") or [])[:6],
                }
            )
        return compact

    def compact_news_sentiment(self, scored: Dict[str, object]) -> Dict[str, object]:
        return {
            "score": coerce_number(scored.get("score"), 50.0),
            "summary": str(scored.get("summary") or "")[:120],
            "trigger_words": list(scored.get("trigger_words") or [])[:8],
            "risk_words": list(scored.get("risk_words") or [])[:8],
        }

    def announcement_flags(
        self,
        row: Dict[str, object],
        news_items: List[Dict[str, object]],
        sentiment: Dict[str, object],
    ) -> List[str]:
        flags: List[object] = []
        raw_event_flags = row.get("event_risk_flags") or []
        if isinstance(raw_event_flags, list):
            for flag in raw_event_flags:
                if isinstance(flag, dict):
                    flags.append(flag.get("label"))
                else:
                    flags.append(flag)
        flags.extend(sentiment.get("risk_words") or [])
        for news in news_items or []:
            title = str(news.get("title") or "")
            for keyword in self.ANNOUNCEMENT_KEYWORDS:
                if keyword in title:
                    flags.append(keyword)
        return self.unique_strings(flags)[:10]

    def read_cache(self) -> Dict[str, object]:
        path = str(getattr(self.config, "DEEPSEEK_NEWS_CACHE_PATH", ".runtime/deepseek_news_context.json") or "")
        return _NEWS_CACHE.read(path)

    def write_cache(self, cache: Dict[str, object]) -> None:
        path = str(getattr(self.config, "DEEPSEEK_NEWS_CACHE_PATH", ".runtime/deepseek_news_context.json") or "")
        if not path:
            return
        _NEWS_CACHE.merge(path, cache)

    @staticmethod
    def unique_strings(values: List[object]) -> List[str]:
        result: List[str] = []
        for value in values:
            text = str(value).strip()
            if text and text not in result:
                result.append(text)
        return result
