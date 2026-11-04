"""Isolation and explicit invocation of the all-profile alignment probe."""

from tests.helpers.configuration import SqlitePolicyFixture

from pathlib import Path
import sys
import subprocess
import json
from types import SimpleNamespace

import pytest

from scripts.diagnose_runtime import _parser, _validate, build_commands
from scripts.runtime_diagnostics.training_alignment import _probe


@pytest.mark.parametrize("reported_profile", ("v1", "v2"))
def test_training_probe_decodes_pretty_profile_and_execution_reports(tmp_path, monkeypatch, reported_profile):
    from scripts.runtime_diagnostics import training_alignment
    from trader import bootstrap
    from trader.recommendation.infra.scoring import profile_factory

    profile = {"schema_version": "profile_training_result", "profile": reported_profile, "status": "engineering_ready", "message": "训练完成"}
    execution = {
        "schema_version": "selected_training_result",
        "status": "completed",
        "history_rows_read": 0,
        "timing": {"source_read_seconds": 0.0, "profiles": [{"profile": reported_profile, "queue_send_seconds": 1.25}]},
    }

    def train(*args, **kwargs):
        print(json.dumps(profile, ensure_ascii=False, indent=2))
        print(json.dumps(execution, ensure_ascii=False, indent=2))
        return 0

    monkeypatch.setattr(bootstrap, "ExecuteSelectedTraining", train)
    monkeypatch.setattr(profile_factory, "LoadScoringProfile", lambda *args, **kwargs: SimpleNamespace(heads=(1, 2)))
    monkeypatch.setattr(training_alignment, "_numeric_fingerprints", lambda *args: ())
    result = training_alignment._train_once(None, tmp_path / "history", tmp_path / "work", ("v1",))
    assert result["complete"] is (reported_profile == "v1")
    assert result["profiles"] == [profile]
    assert result["execution"] == execution


def test_alignment_import_does_not_preload_the_product_before_checkout_selection():
    completed = subprocess.run(
        [sys.executable, "-c", "import sys; import scripts.runtime_diagnostics.training_alignment; assert 'trader' not in sys.modules"],
        check=False, capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr


def test_all_profile_probe_requires_external_isolated_history_and_new_output(tmp_path: Path):
    with pytest.raises(ValueError, match="requires --history-root"):
        _validate(_parser().parse_args(["--profile", "training-alignment"]))
    args = [
        "--profile",
        "training-alignment",
        "--history-root",
        str(tmp_path / "history"),
        "--training-probe-directory",
        str(tmp_path / "probe"),
    ]
    options, _ = _validate(_parser().parse_args(args))
    commands = build_commands(options)
    assert commands[0].name == "training_alignment"
    assert "scripts.runtime_diagnostics.training_alignment" in commands[0].argv
    with pytest.raises(ValueError, match="external_history_copy"):
        _probe(Path(__file__).resolve().parents[3] / "data/market/history", tmp_path / "probe", sqlite_policy=SqlitePolicyFixture())
    with pytest.raises(ValueError, match="new_separate_output"):
        _probe(tmp_path / "history", tmp_path, sqlite_policy=SqlitePolicyFixture())


@pytest.mark.parametrize("inputs_only", (True, False))
@pytest.mark.parametrize("explicit_config", (True, False))
@pytest.mark.parametrize("benchmark_mode", (None, "baseline_before", "baseline_after", "candidate"))
def test_entry_selects_checkout_before_loading_its_sqlite_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inputs_only: bool, explicit_config: bool, benchmark_mode: str | None,
):
    from scripts.runtime_diagnostics import training_alignment as module
    import trader.bootstrap as bootstrap
    import trader.infra.settings as settings

    product = tmp_path / "product"
    policy_module = product / "src/trader/infra/sqlite_policy.py"
    policy_module.parent.mkdir(parents=True)
    policy_module.touch()
    config = tmp_path / "custom.json" if explicit_config else product / "config/runtime.json"
    policy = SqlitePolicyFixture()
    calls = []

    def select(checkout):
        assert checkout == product
        calls.append("selected")
        return product

    def load(path):
        assert calls == ["selected"]
        assert path == config
        calls.append("loaded")
        return policy

    def inputs(history, runtime_config, *, sqlite_policy):
        assert calls == ["selected", "loaded"]
        assert runtime_config == config
        assert sqlite_policy is policy
        return {"status": "passed"}

    def probe(history, work, profiles, *, runtime_config, sqlite_policy, require_reuse):
        assert require_reuse is (benchmark_mode not in {"baseline_before", "baseline_after"})
        return inputs(history, runtime_config, sqlite_policy=sqlite_policy)

    argv = ["probe", "--history-root", str(tmp_path / "history"), "--product-checkout", str(product)]
    argv += ["--inputs-only"] if inputs_only else ["--work-root", str(tmp_path / "output")]
    if explicit_config:
        argv += ["--runtime-config", str(config)]
    if benchmark_mode is not None:
        argv += ["--benchmark-mode", benchmark_mode]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(module, "_select_product", select)
    monkeypatch.setattr(settings, "LoadRuntimeSettings", load)
    monkeypatch.setattr(bootstrap, "BuildSqlitePolicy", lambda loaded: loaded)
    monkeypatch.setattr(module, "_inputs", inputs)
    monkeypatch.setattr(module, "_probe", probe)
    monkeypatch.setattr(module, "emit_report", lambda report: None)
    assert module.main() == 0
