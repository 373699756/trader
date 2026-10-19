"""Bounded startup notices for absent local research inputs; no readiness writes."""

from __future__ import annotations

import logging

from trader.infra.settings.data_paths import DataPaths
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId

_LOGGER = logging.getLogger(__name__)


def WarnMissingLocalData(paths: DataPaths, profile_id: ScoringProfileId, *, model_missing: bool) -> None:
    """Warn about absent files without claiming existing files are validated."""

    if model_missing:
        _LOGGER.warning(
            "警告：%s 双头模型工件缺失，看板继续启动，模型评分待就绪；请运行 ./run.sh train，完成后重启以加载模型。",
            profile_id.upper(),
        )
    if not (paths.history / "catalog.sqlite3").is_file():
        _LOGGER.warning(
            "警告：缺少历史数据，看板继续启动；离线训练与回溯待就绪。需要训练时运行 ./run.sh train。",
        )
    if not paths.daily_window.is_file():
        _LOGGER.warning(
            "警告：%s 缺少行情短窗口，看板继续启动；相关推荐输入待就绪。可运行 ./run.sh sync market 补齐。",
            profile_id.upper(),
        )


__all__ = ["WarnMissingLocalData"]
