from __future__ import annotations

from pathlib import Path
from itertools import count
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from scripts.recommendation.web.browser_session import _cleanup_browser_profile


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


@pytest.mark.parametrize("engine", ("chrome", "firefox"))
@pytest.mark.parametrize("state", ("ready", "not_ready", "wrong_view", "long_quote_missing", "codes_mismatch"))
def test_live_probe_checks_every_enabled_view_and_rejects_unready_empty_results(monkeypatch, state, engine):
    from scripts.recommendation.web import browser_refresh

    monkeypatch.setenv("MOZ_CRASHREPORTER_DISABLE", "user-value")
    class Chrome:
        closed = False
        profile = "v1"
        strategy = "tomorrow"

        def __init__(self, *args, **kwargs):
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
    monkeypatch.setattr(browser_refresh, "ChromeSession", Chrome)
    monkeypatch.setattr(browser_refresh, "_FirefoxBrowser", Chrome)
    monkeypatch.setattr(browser_refresh.shutil, "which", lambda name: name if (
        name == "google-chrome" and engine == "chrome" or name in {"firefox", "geckodriver"} and engine == "firefox"
    ) else None)
    driver = _Driver()
    requests = []
    launches = []
    monkeypatch.setattr(browser_refresh.subprocess, "Popen", lambda *args, **kwargs: launches.append(kwargs) or driver)
    monkeypatch.setattr(browser_refresh, "_wait_driver", lambda *args: None)
    monkeypatch.setattr(browser_refresh, "_request_json", lambda url, **kwargs: (
        requests.append((url, kwargs)) or {"value": {"sessionId": "session-1"}}
    ))
    monkeypatch.setattr(browser_refresh, "_free_port", lambda: 9222)
    monkeypatch.setattr(browser_refresh.time, "monotonic", lambda: float(next(clock)))
    monkeypatch.setattr(browser_refresh.time, "sleep", lambda _: None)
    report = browser_refresh._live_browser("http://localhost")
    assert report["passed"] is (state == "ready")
    assert report["summary"]["browser"] == engine
    views = report["summary"]["strategies"]
    assert [(view["profile"], view["strategy"]) for view in views] == [
        ("v3" if state == "wrong_view" else profile, strategy)
        for profile, strategy in (("v1", "tomorrow"), ("v1", "d25"), ("v5", "tomorrow"), ("v5", "d25"), ("v1", "long"))
    ]
    if engine == "chrome":
        assert Chrome.closed
        assert not requests
    else:
        assert driver.terminated and driver.waited
        assert launches[0]["start_new_session"] is False
        assert launches[0]["env"]["MOZ_CRASHREPORTER_DISABLE"] == "1"
        assert os.environ["MOZ_CRASHREPORTER_DISABLE"] == "user-value"
        assert any(options.get("method") == "DELETE" for _url, options in requests)


class _Driver:
    def __init__(self, slow_exit=False):
        self.terminated = False
        self.waited = False
        self.killed = False
        self.slow_exit = slow_exit

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, *, timeout):
        self.waited = True
        if self.slow_exit and not self.killed and timeout == 5 and self.terminated:
            raise subprocess.TimeoutExpired("geckodriver", timeout)
        return 0

    def kill(self):
        self.killed = True


@pytest.mark.parametrize("failure", ("readiness", "session", "timeouts", "inspection", "cancel", "delete"))
def test_firefox_live_session_reclaims_driver_on_startup_and_inspection_failures(monkeypatch, failure):
    from scripts.recommendation.web import browser_refresh

    driver = _Driver(slow_exit=True)
    requests = []
    monkeypatch.setattr(browser_refresh.shutil, "which", lambda name: name if name in {"firefox", "geckodriver"} else None)
    monkeypatch.setattr(browser_refresh.subprocess, "Popen", lambda *args, **kwargs: driver)
    monkeypatch.setattr(browser_refresh, "_free_port", lambda: 9222)

    def wait_driver(*args):
        if failure == "readiness":
            raise RuntimeError("driver not ready")

    def request(url, **kwargs):
        requests.append((url, kwargs))
        if failure == "session" and url.endswith("/session"):
            return {"value": {"sessionId": None}}
        if (failure == "timeouts" and url.endswith("/timeouts")) or (
            failure == "delete" and kwargs.get("method") == "DELETE"
        ):
            raise RuntimeError("driver connection closed")
        return {"value": {"sessionId": "session-1"}}

    monkeypatch.setattr(browser_refresh, "_wait_driver", wait_driver)
    monkeypatch.setattr(browser_refresh, "_request_json", request)

    def inspect():
        with browser_refresh._live_browser_session() as (engine, browser):
            assert engine == "firefox"
            browser.set_viewport(1440, 1100)
            browser.navigate("http://localhost")
            browser.execute("return true;")
            if failure == "inspection":
                raise RuntimeError("inspection failed")
            if failure == "cancel":
                raise KeyboardInterrupt

    if failure == "delete":
        inspect()
    elif failure == "cancel":
        with pytest.raises(KeyboardInterrupt):
            inspect()
    else:
        with pytest.raises(RuntimeError):
            inspect()
    assert driver.terminated and driver.killed and driver.waited
    assert all(0 < options["timeout"] <= 30 for _url, options in requests)
    if failure in {"timeouts", "inspection", "cancel", "delete"}:
        assert any(options.get("method") == "DELETE" for _url, options in requests)


def test_live_browser_without_supported_installation_fails_before_launch(monkeypatch):
    from scripts.recommendation.web import browser_refresh

    monkeypatch.setattr(browser_refresh.shutil, "which", lambda name: None)
    monkeypatch.setattr(browser_refresh.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("unexpected launch"))
    with pytest.raises(RuntimeError, match="Firefox with geckodriver"):
        browser_refresh._live_browser("http://localhost")


@pytest.mark.skipif(os.name != "posix" or not Path("/proc/self/stat").is_file(), reason="POSIX process group evidence")
@pytest.mark.parametrize("outcome", ("failed", "timed_out"))
def test_public_worker_group_reaps_children_after_failure_or_timeout(tmp_path, outcome):
    from scripts.core.process import RunBoundedCommand

    identity_path = tmp_path / "identity.json"
    invocation = (
        "import json,os,signal,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); time.sleep(30)']); "
        "identity={'worker':os.getpid(),'child':child.pid,'group':os.getpgid(child.pid)}; "
        "open(sys.argv[1],'w').write(json.dumps(identity)); "
        "time.sleep(30) if sys.argv[2]=='timed_out' else sys.exit(1)"
    )
    identity = None
    try:
        if outcome == "timed_out":
            with pytest.raises(subprocess.TimeoutExpired):
                RunBoundedCommand((sys.executable, "-c", invocation, str(identity_path), outcome), timeout=0.5, environment=os.environ)
        else:
            result = RunBoundedCommand(
                (sys.executable, "-c", invocation, str(identity_path), outcome), timeout=5, environment=os.environ,
            )
            assert result.returncode == 1
        identity = json.loads(identity_path.read_text())
        assert identity["worker"] == identity["group"] != os.getpgrp()
        deadline = time.monotonic() + 2
        while True:
            process_stat = Path(f"/proc/{identity['child']}/stat")
            try:
                state = process_stat.read_text().rsplit(")", 1)[1].split()[0]
            except FileNotFoundError:
                break
            if state in {"Z", "X"}:
                break
            assert time.monotonic() < deadline, "owned child survived public worker group cleanup"
            time.sleep(0.01)
    finally:
        if identity is not None:
            try:
                os.kill(identity["child"], signal.SIGKILL)
            except ProcessLookupError:
                pass
