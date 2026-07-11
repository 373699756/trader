from __future__ import annotations

import json
from datetime import datetime
from typing import Dict

from . import config
from .runtime_json import atomic_write_json


class TomorrowIterationService:
    """Builds and persists tomorrow strategy weight iteration payloads."""

    def path(self) -> str:
        return getattr(config, "TOMORROW_ITERATION_PATH", ".runtime/tomorrow_iteration.json")

    def can_apply(self, result: Dict[str, object]) -> bool:
        return bool(result.get("ok")) and result.get("status") == "dry_run_improved"

    def reason(self, result: Dict[str, object]) -> str:
        status = str(result.get("status") or "")
        mode = str(result.get("objective_mode") or "default")
        mode_text = "方向先行" if mode == "direction_focused" else "平衡口径"
        if status == "dry_run_improved":
            return f"样本外验证改善（{mode_text}），允许人工应用。"
        if status == "insufficient_samples":
            return "有效样本不足，暂不允许自动修正。"
        if status == "insufficient_factor_coverage":
            return "因子覆盖不足，暂不允许自动修正。"
        if status == "no_oos_improvement":
            return f"样本外没有稳定改善（{mode_text}），保持当前权重。"
        if status == "insufficient_oos_folds":
            return "样本外折数不足，继续积累样本。"
        if status == "written":
            return "建议权重已写入并生效。"
        if status == "dry_run":
            return "当前权重未找到更优替代。"
        return result.get("error") or "暂无可应用建议。"

    def current_weights(self) -> Dict[str, float]:
        from .calibrate import _current_strategy_weights

        return _current_strategy_weights("tomorrow_picks")

    def payload(self, result: Dict[str, object], applied: bool = False, days: int = 120) -> Dict[str, object]:
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "strategy": "tomorrow_picks",
            "days": int(days),
            "objective_mode": result.get("objective_mode") or (
                "direction_focused" if getattr(config, "CALIBRATE_TOMORROW_DIRECTION_FOCUSED", False) else "default"
            ),
            "current_weights": (result.get("weights") or {}) if applied else self.current_weights(),
            "suggested_weights": result.get("weights") or {},
            "can_apply": self.can_apply(result),
            "applied": applied,
            "reason": self.reason(result),
            "result": result,
        }

    def save(self, payload: Dict[str, object]) -> None:
        atomic_write_json(self.path(), payload, ensure_ascii=False, indent=2)

    def load(self) -> Dict[str, object]:
        try:
            with open(self.path(), "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def refresh_scoring_weights(self, weights: Dict[str, object]) -> None:
        if not weights:
            return
        from . import scoring as scoring_module

        scoring_module.WEIGHTS.setdefault("tomorrow_picks", {}).update(weights)
