"""Chrome ownership is tested with controlled HTTP, CDP and process-family doubles."""

import json

import pytest

from scripts.recommendation.web import browser_session
from trader.infra.http_contracts import HttpCancelled, HttpConnectionError, HttpResponse, HttpTimeout


class _Profile:
    name = "/isolated-chrome-profile"

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def cleanup(self) -> None:
        self.events.append("profile_closed")


class _Family:
    def __init__(self, events: list[str], *, exited: bool = False) -> None:
        self.events = events
        self.exited = exited

    def poll(self) -> int | None:
        self.events.append("family_observed")
        return 0 if self.exited else None

    def close(self) -> None:
        self.events.append("family_closed")

    def close_streams(self) -> None:
        self.events.append("streams_closed")


class _Transport:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __enter__(self):
        self.events.append("http_open")
        return self

    def __exit__(self, *args):
        self.events.append("http_closed")

    def get(self, url, **kwargs):
        assert url == "http://127.0.0.1:9222/json/list"
        assert kwargs == {"timeout": 1, "max_response_bytes": 1024 * 1024}
        return HttpResponse(200, b'[{"type":"page","webSocketDebuggerUrl":"ws://localhost/controlled"}]')


class _Socket:
    def __init__(self, events: list[str], *, command_error: bool = False) -> None:
        self.events = events
        self.command_error = command_error
        self.close_error = False
        self.only_events = False
        self.commands: list[dict[str, object]] = []
        self.timeouts: list[float] = []

    def send(self, payload: str) -> None:
        self.commands.append(json.loads(payload))

    def recv(self) -> str:
        command = self.commands[-1]
        if self.only_events:
            return '{"method":"Runtime.consoleAPICalled","params":{}}'
        if self.command_error:
            return json.dumps({"id": command["id"], "error": {"message": "controlled command failure"}})
        result = {"result": {"value": 42}} if command["method"] == "Runtime.evaluate" else {"data": "image"}
        return json.dumps({"id": command["id"], "result": result})

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def shutdown(self) -> None:
        self.events.append("socket_closed")
        if self.close_error:
            raise OSError("controlled socket close failure")


def _resources(monkeypatch, *, failure=None):
    events = []
    profile = _Profile(events)
    family = _Family(events, exited=failure == "exited")
    connection = _Socket(events, command_error=failure == "command")
    launches = []
    monkeypatch.setattr(browser_session.tempfile, "TemporaryDirectory", lambda **kwargs: profile)

    def launch(argv, **kwargs):
        launches.append((argv, kwargs))
        if failure == "launch":
            raise OSError("controlled launch failure")
        return family

    def connect(url, *, timeout):
        assert url == "ws://localhost/controlled" and timeout == 15
        if failure == "connect":
            raise OSError("controlled connection failure")
        return connection

    def open_http():
        if failure == "cancel":
            raise KeyboardInterrupt
        return _Transport(events)

    monkeypatch.setattr(browser_session, "ProcessFamily", launch)
    monkeypatch.setattr(browser_session.websocket, "create_connection", connect)
    return events, family, connection, launches, open_http


def test_chrome_commands_use_injected_http_and_one_owned_process_family(monkeypatch) -> None:
    events, _, connection, launches, open_http = _resources(monkeypatch)

    with browser_session.ChromeSession("controlled-chrome", 9222, open_http=open_http) as chrome:
        chrome.navigate("http://localhost/dashboard")
        chrome.set_viewport(1440, 900)
        assert chrome.execute("return 42;") == 42
        assert chrome.screenshot() == "image"

    chrome.close()
    argv, options = launches[0]
    assert argv[0] == "controlled-chrome" and "--user-data-dir=/isolated-chrome-profile" in argv
    assert options == {"stdout": browser_session.subprocess.DEVNULL, "stderr": browser_session.subprocess.DEVNULL}
    assert [command["method"] for command in connection.commands] == [
        "Runtime.enable", "Page.enable", "Page.navigate", "Emulation.setDeviceMetricsOverride",
        "Runtime.evaluate", "Page.captureScreenshot",
    ]
    assert events[-4:] == ["socket_closed", "family_closed", "streams_closed", "profile_closed"]
    assert all(events.count(name) == 1 for name in ("http_closed", "socket_closed", "family_closed", "profile_closed"))
    with pytest.raises(RuntimeError, match="closed"):
        chrome.execute("return 1;")


@pytest.mark.parametrize("failure", ("launch", "exited", "connect", "command", "cancel"))
def test_chrome_startup_failure_and_cancellation_reclaim_every_created_resource(monkeypatch, failure) -> None:
    events, _, _, _, open_http = _resources(monkeypatch, failure=failure)
    error = KeyboardInterrupt if failure == "cancel" else OSError if failure in {"launch", "connect"} else RuntimeError

    with pytest.raises(error):
        browser_session.ChromeSession("controlled-chrome", 9222, open_http=open_http)

    assert events[-1] == "profile_closed"
    if failure == "launch":
        assert events == ["profile_closed"]
    else:
        assert events[-3:] == ["family_closed", "streams_closed", "profile_closed"]
    if failure == "command":
        assert "socket_closed" in events


def test_socket_cleanup_failure_still_reclaims_the_process_family_and_profile(monkeypatch) -> None:
    events, _, connection, _, open_http = _resources(monkeypatch)
    chrome = browser_session.ChromeSession("controlled-chrome", 9222, open_http=open_http)
    connection.close_error = True

    with pytest.raises(OSError, match="controlled socket close failure"):
        chrome.close()

    assert events[-4:] == ["socket_closed", "family_closed", "streams_closed", "profile_closed"]
    connection.close_error = False
    chrome.close()
    assert events.count("family_closed") == 1


def test_unrelated_devtools_events_cannot_extend_a_command_deadline(monkeypatch) -> None:
    _, _, connection, _, open_http = _resources(monkeypatch)
    with browser_session.ChromeSession("controlled-chrome", 9222, open_http=open_http) as chrome:
        connection.only_events = True
        elapsed = 0.0

        def monotonic() -> float:
            nonlocal elapsed
            elapsed += 7.0
            return elapsed

        monkeypatch.setattr(browser_session.time, "monotonic", monotonic)
        with pytest.raises(TimeoutError, match="command timed out"):
            chrome.navigate("http://localhost/dashboard")
        assert connection.timeouts[-2:] == [8.0, 1.0]


@pytest.mark.parametrize("error", (HttpConnectionError, HttpTimeout))
def test_devtools_readiness_retries_an_initial_http_failure_before_a_valid_endpoint(monkeypatch, error) -> None:
    events, _, _, _, open_http = _resources(monkeypatch)
    original_get = _Transport.get
    attempts = 0
    delays = []

    def get(transport, url, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error("controlled endpoint not ready")
        return original_get(transport, url, **kwargs)

    monkeypatch.setattr(_Transport, "get", get)
    monkeypatch.setattr(browser_session.time, "sleep", delays.append)

    with browser_session.ChromeSession("controlled-chrome", 9222, open_http=open_http):
        assert attempts == 2 and delays == [0.05]
        assert events.count("http_closed") == 2

    assert events[-4:] == ["socket_closed", "family_closed", "streams_closed", "profile_closed"]


@pytest.mark.parametrize("error", (HttpCancelled, InterruptedError))
def test_devtools_readiness_propagates_cancellation_without_retrying(monkeypatch, error) -> None:
    events, _, _, _, open_http = _resources(monkeypatch)
    attempts = 0

    def get(transport, url, **kwargs):
        nonlocal attempts
        attempts += 1
        raise error("controlled readiness cancellation")

    monkeypatch.setattr(_Transport, "get", get)
    monkeypatch.setattr(browser_session.time, "sleep", lambda _: pytest.fail("cancelled readiness must not retry"))

    with pytest.raises(error, match="controlled readiness cancellation"):
        browser_session.ChromeSession("controlled-chrome", 9222, open_http=open_http)

    assert attempts == 1 and events.count("http_closed") == 1
    assert events[-3:] == ["family_closed", "streams_closed", "profile_closed"]
