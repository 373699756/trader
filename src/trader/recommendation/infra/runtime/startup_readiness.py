"""Bounded startup notices for absent local research inputs; no readiness writes."""

from __future__ import annotations

import logging
from pathlib import Path

from trader.download.infra.qfq_sqlite import QFQ_SHARD_NAMES
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId

_LOGGER = logging.getLogger(__name__)


def WarnMissingLocalData(project_root: Path, profile_id: ScoringProfileId, *, model_missing: bool) -> None:
    """Warn about absent files without claiming existing files are validated."""

    if model_missing:
        _LOGGER.warning(
            "警告：%s 双头模型工件缺失，看板继续启动，模型评分待就绪；请完成 ./run.sh download 后运行 "
            "./run.sh train-%s，完成后重启以加载模型。",
            profile_id.upper(),
            profile_id,
        )
    if not (project_root / "data/history/control.sqlite3").is_file():
        _LOGGER.warning(
            "警告：缺少 history 历史数据，看板继续启动；离线训练与回溯待就绪。"
            "需要训练时先运行 ./run.sh download，再运行 ./run.sh train-%s。",
            profile_id,
        )
    if not any((project_root / "data/qfq" / profile_id / name).is_file() for name in QFQ_SHARD_NAMES):
        _LOGGER.warning(
            "警告：%s 缺少 qfq 短窗口，看板继续启动；相关推荐输入待就绪。可运行 ./run.sh qfq_download 补齐。",
            profile_id.upper(),
        )


__all__ = ["WarnMissingLocalData"]
