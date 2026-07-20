"""Render FeatureSnapshot into a deterministic "ground truth" block for DeepSeek prompts.

The renderer produces an authoritative numeric snapshot that DeepSeek sees.
It is intentionally separate from the free-text evidence block so that the
model cannot hallucinate alternative values for fields that are already
computed locally.
"""

from __future__ import annotations

from collections.abc import Sequence

from trader.domain.models import FeatureSnapshot


def render_ground_truth(snapshot: FeatureSnapshot, *, stock_index: int = 0) -> str:
    """Produce a compact, deterministic numeric ground-truth block for one candidate.

    The output is a stable string suitable for embedding in a prompt.  It only
    contains fields whose values are known locally — DeepSeek must not invent or
    override them.

    Args:
        snapshot: The candidate feature snapshot.
        stock_index: 0-based index for human readability in multi-stock batches.

    Returns:
        A markdown-style text block.
    """
    lines: list[str] = []
    quote = snapshot.quote
    lines.append(f"## Stock #{stock_index + 1}: {quote.code} {quote.name}")
    lines.append("")
    lines.append("### Quote Snapshot (authoritative)")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|-------|-------|")
    _add_row(lines, "price", quote.price)
    _add_row(lines, "pct_change", quote.pct_change, suffix="%")
    _add_row(lines, "change_5m", quote.change_5m, suffix="%")
    _add_row(lines, "volume_ratio", quote.volume_ratio)
    _add_row(lines, "turnover_rate", quote.turnover_rate, suffix="%")
    _add_row(lines, "amount", _fmt_large_number(quote.amount))
    _add_row(lines, "amplitude", quote.amplitude, suffix="%")
    _add_row(lines, "market_cap", _fmt_large_number(quote.market_cap))
    lines.append("")

    lines.append("### Computed Features (authoritative)")
    lines.append("")
    lines.append("| Feature | Value |")
    lines.append("|---------|-------|")
    for name in sorted(snapshot.values.keys()):
        raw = snapshot.values[name]
        if raw is not None:
            lines.append(f"| {name} | {round(raw, 4)} |")
        else:
            lines.append(f"| {name} | null |")
    lines.append("")

    evidence_count = len(snapshot.evidence)
    risk_fact_count = len(snapshot.external_risk_facts)
    missing = ", ".join(snapshot.missing_fields) if snapshot.missing_fields else "none"
    lines.append(f"evidence_count={evidence_count}, risk_fact_count={risk_fact_count}, missing_fields={missing}")

    return "\n".join(lines)


def render_batch_ground_truth(candidates: Sequence[FeatureSnapshot]) -> str:
    """Render ground-truth blocks for a batch of candidates.

    Used as a shared preamble in the DeepSeek prompt so the model sees
    authoritative values once per batch.
    """
    blocks = [render_ground_truth(c, stock_index=i) for i, c in enumerate(candidates)]
    return "\n\n---\n\n".join(blocks)


def _add_row(lines: list[str], label: str, value: float | str | None, *, suffix: str = "") -> None:
    if value is None:
        lines.append(f"| {label} | null |")
    elif isinstance(value, str):
        lines.append(f"| {label} | {value} |")
    else:
        lines.append(f"| {label} | {round(value, 4)}{suffix} |")


def _fmt_large_number(value: float | None) -> str:
    if value is None:
        return "null"
    abs_v = abs(value)
    if abs_v >= 1e8:
        return f"{value / 1e8:.4f}亿"
    if abs_v >= 1e4:
        return f"{value / 1e4:.4f}万"
    return f"{round(value, 4)}"


__all__ = ["render_batch_ground_truth", "render_ground_truth"]
