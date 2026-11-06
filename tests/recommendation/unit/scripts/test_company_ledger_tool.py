"""The ledger tool uses the selected policy without starting a browser in tests."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import url2pathname

import pytest

from scripts.core.catalog import SelectionArguments
from scripts.recommendation.web import company_ledger_browser
from tests.helpers.shared.configuration import CompanyScoringPolicyFixture
from tests.helpers.shared.tool_cli import BuildToolCommands, ParseBusinessOptions, ParseToolArguments


def test_ledger_public_command_passes_the_selected_runtime(tmp_path: Path) -> None:
    configuration = tmp_path / "runtime.json"
    options = ParseBusinessOptions(ParseToolArguments([
        *SelectionArguments("company-ledger"), "--runtime-config", str(configuration),
    ]))
    command, = BuildToolCommands(options)
    assert command.argv[-2:] == ("--runtime-config", str(configuration))


@pytest.mark.parametrize("bonus_cap", (0, 0.5, 7))
def test_ledger_fixture_builds_with_the_complete_configured_policy(tmp_path: Path, monkeypatch, capsys, bonus_cap) -> None:
    configuration = tmp_path / "runtime.json"
    policy = CompanyScoringPolicyFixture(d25_cap=bonus_cap)
    selected = []
    original = company_ledger_browser.BuildScoreLedger

    def load(path):
        assert path == configuration
        return SimpleNamespace(strategy_config_path=configuration.parent / "strategy.json")

    def strategy(path):
        assert path == configuration.parent / "strategy.json"
        return SimpleNamespace(company_scoring=policy)

    def ledger(*args, **kwargs):
        selected.append(kwargs["policy"])
        return original(*args, **kwargs)

    def browser(argv):
        page = Path(url2pathname(urlsplit(argv[-1]).path)).read_text(encoding="utf-8")
        match = re.search(r"const item = (.*);\n", page)
        assert match is not None
        scores = json.loads(match[1])["scores"]
        assert scores["local_score"] == scores["final_score"] == scores["ledger"]["local_score"] == 91 + min(1, bonus_cap)
        assert "expected.applied_bonus.toFixed(2)" in page
        return subprocess.CompletedProcess(argv, 0, '<pre id="company-probe">' + json.dumps(rows) + "</pre>", "")

    rows = [{
        "profile": profile, "columns": 11,
        **{name: True for name in ("ledgerVisible", "drawerVisible", "evidenceButton", "finiteLayout", "modelDistinct")},
    } for profile in ("v2", "v1", "legacy")]
    monkeypatch.setenv("TRADER_DIAGNOSTIC_CHROME", "fixture-browser")
    monkeypatch.setattr(company_ledger_browser, "LoadRuntimeSettings", load)
    monkeypatch.setattr(company_ledger_browser, "LoadStrategySettings", strategy)
    monkeypatch.setattr(company_ledger_browser, "BuildScoreLedger", ledger)
    monkeypatch.setattr(company_ledger_browser, "_run_browser", browser)
    monkeypatch.setattr(company_ledger_browser.subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, "fixture-browser", "",
    ))
    assert company_ledger_browser.Main(["--runtime-config", str(configuration)]) == 0
    assert selected == [policy]
    assert json.loads(capsys.readouterr().out)["status"] == "passed"
