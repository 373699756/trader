"""Real publisher process exits at the fixed-directory swap boundaries."""

import os
import shutil
from pathlib import Path

from trader.download.infra.history_control_index import HistoryMaintenanceLock
from trader.training.infra.model_bundles.bundle_snapshot import MakeBundleStagingDirectory
from trader.training.infra.model_bundles.profile_publication import PublishProfileBundles
from trader.training.infra.profile.v1.models import PublishLearnedRankPair, ReadLearnedRankPair
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE


def CrashProfilePublication(model_root: Path, profile_id: str, point: str) -> None:
    with HistoryMaintenanceLock(model_root / f".{profile_id}.training.lock"):
        _publish_until_exit(model_root, profile_id, point)


def _publish_until_exit(model_root: Path, profile_id: str, point: str) -> None:
    profile_root = model_root / profile_id
    original_replace = os.replace

    def crash_replace(source: str | Path, destination: str | Path) -> None:
        original_replace(source, destination)
        if point == "old_moved" and Path(source) == profile_root:
            os._exit(86)
        if point == "new_moved" and Path(destination) == profile_root:
            os._exit(86)

    if profile_id == "v1":
        pair = ReadLearnedRankPair(model_root)
        os.replace = crash_replace
        PublishLearnedRankPair(model_root, pair)
    else:
        profile = next(
            item for item in (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE, V5_TRAINING_PROFILE)
            if item.profile_id == profile_id
        )
        staged = tuple(
            (head.strategy, MakeBundleStagingDirectory(profile_root))
            for head in profile.heads
        )
        for strategy, destination in staged:
            shutil.copytree(profile_root / strategy.value, destination, dirs_exist_ok=True)
        os.replace = crash_replace
        PublishProfileBundles(profile_root, profile, staged)


__all__ = ["CrashProfilePublication"]
