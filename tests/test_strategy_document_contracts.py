from pathlib import Path

from stock_analyzer import config
from stock_analyzer.strategies.types import storage_strategy_name


ROOT = Path(__file__).resolve().parents[1]


def _strategy_doc() -> str:
    return (ROOT / "docs" / "strategy_and_prediction.md").read_text(encoding="utf-8")


def test_strategy_document_matches_runtime_strategy_names_and_snapshot_semantics():
    text = _strategy_doc()

    assert "不进入默认自动快照" not in text
    assert "可以进入自动快照作为辅助验证样本" in text
    assert "验证库、API 参数、指标缓存、`baseline_id`" in text
    assert "short_term" in config.AUTO_SNAPSHOT_STRATEGIES
    assert "short_term" not in config.ACTIVE_STRATEGIES
    assert storage_strategy_name("today_picks") == "short_term"
    assert storage_strategy_name("swing_2_5d_picks") == "swing_picks"


def test_strategy_document_preserves_oos_return_and_shadow_boundaries():
    text = _strategy_doc()

    assert "shadow_rank_key" in text
    assert "production_ranking_key = predicted_net_return" not in text
    assert "`DEEPSEEK_META_PRODUCTION_ENABLED` 硬编码为 false" in text
    assert "primary_return_net" in text
    assert "涨跌停未成交按现金零收益处理" in text
    assert "DeepSeek 不能单独否决" in text
