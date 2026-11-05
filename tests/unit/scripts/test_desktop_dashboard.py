from __future__ import annotations

from pathlib import Path
from runpy import run_path
from itertools import count

import pytest

_RUNNER = Path(__file__).resolve().parents[2] / "performance" / "run_desktop_dashboard.py"
_cleanup_browser_profile = run_path(str(_RUNNER))["_cleanup_browser_profile"]


class _FlakyProfile:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def cleanup(self) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise OSError("profile still in use")


def test_chrome_profile_cleanup_retries_a_bounded_filesystem_race() -> None:
    profile = _FlakyProfile(failures=2)

    _cleanup_browser_profile(profile, attempts=3, delay_seconds=0.0)

    assert profile.calls == 3


@pytest.mark.parametrize("state", ("ready", "not_ready", "wrong_view", "long_quote_missing", "codes_mismatch"))
def test_live_probe_checks_every_enabled_view_and_rejects_unready_empty_results(monkeypatch, state):
    from scripts.recommendation.web import browser_refresh
    from tests.performance import run_desktop_dashboard

    class Chrome:
        closed = False
        profile = "v1"
        strategy = "tomorrow"

        def __init__(self, *args):
            pass

        def set_viewport(self, *args):
            pass

        def navigate(self, url):
            pass

        def execute(self, script):
            if "Array.from(document.querySelectorAll('.profile-tab')" in script:
                return ["v1", "v5"]
            if "data-profile=" in script:
                self.profile = script.split("data-profile=")[1].split("]")[0]
            elif "data-strategy=" in script:
                self.strategy = script.split("data-strategy=")[1].split("]")[0]
            elif "return (async" in script:
                return {"profile": "v3" if state == "wrong_view" else self.profile, "strategy": self.strategy,
                        "matched": state != "codes_mismatch", "stream": "实时", "status": state if state == "not_ready" else "ready",
                        "rows": 1 if self.strategy == "long" else 0,
                        "expected_rows": 1 if self.strategy == "long" else 0,
                        "quotes_complete": not (self.strategy == "long" and state == "long_quote_missing"),
                        "browser_errors": 0}
            return True

        def close(self):
            Chrome.closed = True

    clock = count()
    monkeypatch.setattr(run_desktop_dashboard, "_ChromeSession", Chrome)
    monkeypatch.setattr(browser_refresh.shutil, "which", lambda _: "chrome")
    monkeypatch.setattr(browser_refresh, "_free_port", lambda: 9222)
    monkeypatch.setattr(browser_refresh.time, "monotonic", lambda: float(next(clock)))
    monkeypatch.setattr(browser_refresh.time, "sleep", lambda _: None)
    report = browser_refresh._live_browser("http://localhost")
    assert report["passed"] is (state == "ready")
    views = report["summary"]["strategies"]
    assert [(view["profile"], view["strategy"]) for view in views] == [
        ("v3" if state == "wrong_view" else profile, strategy)
        for profile, strategy in (("v1", "tomorrow"), ("v1", "d25"), ("v5", "tomorrow"), ("v5", "d25"), ("v1", "long"))
    ]
    assert Chrome.closed
