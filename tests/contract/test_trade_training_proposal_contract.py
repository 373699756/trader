from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _compact(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_daily_close_training_proposal_preserves_authoritative_boundaries() -> None:
    proposal = _compact(ROOT / "docs/trade.md")

    for token in (
        "docs/recommendation-strategy.md",
        "daily_close_proxy",
        "不能作为第 15.1.32 节要求的 Tomorrow 14:50 点时一致性证据",
        "production_authority=false",
        "被硬过滤或硬过滤证据不完整的股票不生成训练样本",
        "DeepSeek 完全不参与训练、验证、模型选择或历史收益门禁",
        "最旧 60%",
        "随后 20%",
        "最新 20%",
        "5 折 expanding walk-forward",
        "Ridge",
        "LightGBM",
        "automatic_model_update=false",
    ):
        assert token in proposal


def test_daily_close_training_proposal_keeps_production_activation_manual_and_separate() -> None:
    proposal = _compact(ROOT / "docs/trade.md")

    for token in (
        "只替换 Tomorrow 本地 `base_score`",
        "本地风险仍只扣一次",
        "固定 68/32 融合",
        "另立高风险生产变更批次",
        "不得自动修改生产配置",
    ):
        assert token in proposal
