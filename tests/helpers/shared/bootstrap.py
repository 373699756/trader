from __future__ import annotations
import json
import shutil
from pathlib import Path
from tests.helpers.training.profiles.v3.profile import _publish_profile
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _config(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    shutil.copytree(PROJECT_ROOT / "config", config_dir, dirs_exist_ok=True)
    strategy = json.loads((config_dir / "strategy.json").read_text(encoding="utf-8"))
    strategy["scoring_profile"] = "v2"
    strategy_path = config_dir / "strategy.json"
    strategy_path.write_text(json.dumps(strategy), encoding="utf-8")
    runtime = json.loads((config_dir / "runtime.json").read_text(encoding="utf-8"))
    runtime["runtime_dir"] = str(tmp_path / "runtime")
    runtime["strategy_config"] = str(strategy_path)
    runtime["long_watchlist"] = str(config_dir / "long_watchlist.json")
    reference_dir = tmp_path / "data/market/reference"
    reference_dir.mkdir(parents=True, exist_ok=True)
    for profile in (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE):
        _publish_profile(tmp_path / "data" / "training" / "models", profile)
    path = config_dir / "runtime.json"
    path.write_text(json.dumps(runtime), encoding="utf-8")
    return path


def _config_with_strategy_profile(tmp_path: Path, profile: str) -> Path:
    path = _config(tmp_path)
    strategy_path = path.parent / "strategy.json"
    strategy = json.loads(strategy_path.read_text(encoding="utf-8"))
    strategy["scoring_profile"] = profile
    strategy_path.write_text(json.dumps(strategy), encoding="utf-8")
    return path
