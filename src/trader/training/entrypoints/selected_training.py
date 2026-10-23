"""Execute the selected profile commands after one shared history preparation."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable

from trader.infra.settings import RuntimeSettings
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.entrypoints.commands import ConfigureTrainingResources
from trader.training.entrypoints.profile_training import RunModelTrainingCommand
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE


def RunSelectedTraining(
    runtime: RuntimeSettings,
    profiles: tuple[ScoringProfileId, ...],
    *,
    run_v1: Callable[[], int],
    run_joint: Callable[[], int],
) -> int:
    """Continue independent failures, stop on cancellation, and retain the shared V2/V3 scan."""
    completed: list[tuple[ScoringProfileId, int]] = []
    joint = "v2" in profiles and "v3" in profiles
    for profile in profiles:
        if any(name == profile for name, _code in completed):
            continue
        batch: tuple[ScoringProfileId, ...] = ("v2", "v3") if joint and profile in {"v2", "v3"} else (profile,)
        print(f"train: 开始处理 {', '.join(name.upper() for name in batch)}。", file=sys.stderr)
        try:
            ConfigureTrainingResources(scoring_profile=profile)
            if len(batch) == 2:
                code = run_joint()
            elif profile == "v1":
                code = run_v1()
            else:
                contract = (
                    V2_TRAINING_PROFILE
                    if profile == "v2"
                    else V3_TRAINING_PROFILE
                    if profile == "v3"
                    else V5_TRAINING_PROFILE
                )
                code = RunModelTrainingCommand(runtime, contract)
        except (KeyboardInterrupt, InterruptedError):
            print(json.dumps({"schema_version": "profile_training_result", "profiles": batch, "status": "cancelled"}))
            code = 130
        except (OSError, RuntimeError, ValueError, ImportError) as exc:
            dependency_missing = isinstance(exc, ModuleNotFoundError) and profile == "v5" and exc.name == "torch"
            if dependency_missing:
                print("V5需要PyTorch CPU依赖，请按下载训练文档安装 .[v5] 后重试。", file=sys.stderr)
            print(
                json.dumps(
                    {
                        "schema_version": "profile_training_result",
                        "profiles": batch,
                        "status": "failed",
                        "category": "dependency_missing" if dependency_missing else type(exc).__name__,
                    }
                )
            )
            code = 1
        completed.extend((name, code) for name in batch)
        if code == 130:
            break
    exit_code = (
        130 if any(code == 130 for _name, code in completed) else int(any(code != 0 for _name, code in completed))
    )
    if len(profiles) > 1:
        print(
            json.dumps(
                {
                    "schema_version": "selected_training_result",
                    "status": "cancelled" if exit_code == 130 else "failed" if exit_code else "completed",
                    "profiles": profiles,
                    "results": [{"profile": name, "exit_code": code} for name, code in completed],
                    "not_started": [name for name in profiles if not any(done == name for done, _code in completed)],
                }
            )
        )
    return exit_code


__all__ = ["RunSelectedTraining"]
