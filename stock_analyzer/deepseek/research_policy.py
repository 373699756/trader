"""Cost and evidence policies for shared DeepSeek research."""

from __future__ import annotations

import hashlib
import json
from typing import Dict, List


QUALITATIVE_MARKERS = {
    "announcement",
    "cashflow",
    "financial",
    "fundamental",
    "industry",
    "news",
    "policy",
    "report",
    "risk",
    "公告",
    "现金流",
    "财务",
    "基本面",
    "行业",
    "新闻",
    "政策",
    "研报",
    "风险",
}
MARKET_ONLY_MARKERS = {
    "market",
    "quote",
    "technical",
    "turnover",
    "volume",
    "行情",
    "技术",
    "换手",
    "量价",
}


def has_qualitative_evidence(evidence: object) -> bool:
    """Return whether evidence contains facts worth sending to an LLM."""
    if not isinstance(evidence, list):
        return False
    for item in evidence:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or item.get("source_type") or "").strip().lower()
        evidence_id = str(item.get("evidence_id") or "").strip().lower()
        if source in {"point_in_time_market_data", "point_in_time_fundamentals"} or evidence_id.startswith(
            ("m_", "f_")
        ):
            return True
        evidence_type = (
            str(item.get("evidence_type") or item.get("type") or item.get("source_type") or item.get("category") or "")
            .strip()
            .lower()
        )
        if any(marker in evidence_type for marker in QUALITATIVE_MARKERS):
            return True
        if any(marker in evidence_type for marker in MARKET_ONLY_MARKERS):
            continue
        searchable = " ".join(str(item.get(key) or "").lower() for key in ("evidence_id", "title", "source", "field"))
        if any(marker in searchable for marker in QUALITATIVE_MARKERS):
            return True
    return False


def qualitative_evidence_hash(evidence: object) -> str:
    qualitative = []
    for item in evidence or []:
        if not isinstance(item, dict) or not has_qualitative_evidence([item]):
            continue
        normalized = dict(item)
        if str(normalized.get("source") or "") in {
            "point_in_time_fundamentals",
            "point_in_time_market_data",
        }:
            # The evidence id already fingerprints the structured values.  The
            # observation timestamp is provenance, not a material cache input.
            normalized.pop("published_at", None)
        qualitative.append(normalized)
    return hashlib.sha256(
        json.dumps(qualitative, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def neutralize_shared_research_messages(messages: object) -> List[Dict[str, object]]:
    """Remove first-strategy bias from research shared by all horizons."""
    normalized = [dict(item) for item in messages or [] if isinstance(item, dict)]
    instruction = (
        "本次分析是今日、明日、波段和长期观察策略共享的中性事实研究。"
        "提示词中出现的具体策略名称仅用于记录，不得改变事实判断、风险判断或证据权重；"
        "不要给出买卖建议，不要为了匹配某个持有周期调整结论。"
    )
    if normalized:
        normalized[0]["content"] = "{}\n{}".format(instruction, normalized[0].get("content") or "")
    else:
        normalized.append({"role": "system", "content": instruction})
    return normalized
