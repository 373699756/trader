from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import requests

from trader import bootstrap
from trader.entrypoints.cli import main as cli_main
from trader.training.infra.research.capability_http import BoundedCapabilitySession
from trader.training.infra.research.command_evidence import CapabilityEvidencePublisher

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class _Response:
    def __init__(self, chunks=(b"{}",), *, status=200, advance=lambda: None):
        self.chunks = chunks
        self.status_code = status
        self.advance = advance
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("private supplier payload")

    def iter_content(self, *, chunk_size):
        assert chunk_size == 65536
        self.advance()
        yield from self.chunks


class _Session:
    def __init__(self, response=None, *, failure=None):
        self.response = response or _Response()
        self.failure = failure
        self.calls = []
        self.headers = {}
        self.trust_env = True
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.failure is not None:
            raise self.failure
        return self.response


def _get(transport, *, timeout=8):
    return transport.get("https://gu.qq.com/", params={"param": ("sample",)}, timeout=timeout)


def test_transport_budget_starts_after_offline_scan_and_caps_physical_requests():
    clock = [0.0]
    session = _Session()
    transport = BoundedCapabilitySession(session, timeout_seconds=8, monotonic=lambda: clock[0])
    clock[0] = 400.0
    assert _get(transport, timeout=60).json() == {}
    assert _get(transport).json() == {}
    with pytest.raises(requests.Timeout):
        _get(transport)
    assert len(session.calls) == 2
    assert session.calls[0][1]["timeout"] == 8
    assert all(call[1]["allow_redirects"] is False for call in session.calls)
    assert session.response.closed


@pytest.mark.parametrize("failure", [requests.Timeout("private timeout"), requests.ConnectionError("private error")])
def test_transport_failure_is_one_attempt_without_fallback(failure):
    session = _Session(failure=failure)
    transport = BoundedCapabilitySession(session, timeout_seconds=8, monotonic=lambda: 0)
    with pytest.raises(type(failure)):
        _get(transport)
    assert len(session.calls) == 1


@pytest.mark.parametrize("status", [302, 500])
def test_transport_rejects_redirect_and_http_error_and_closes_response(status):
    response = _Response(status=status)
    transport = BoundedCapabilitySession(_Session(response), timeout_seconds=8, monotonic=lambda: 0)
    with pytest.raises(requests.RequestException):
        _get(transport)
    assert response.closed


def test_transport_rejects_oversized_response_and_closes_it():
    response = _Response((b"x" * BoundedCapabilitySession.MAX_RESPONSE_BYTES, b"x"))
    transport = BoundedCapabilitySession(_Session(response), timeout_seconds=8, monotonic=lambda: 0)
    with pytest.raises(ValueError, match="byte budget"):
        _get(transport)
    assert response.closed


@pytest.mark.parametrize("chunks", [(b"{}",), ()])
def test_transport_deadline_closes_stream_including_empty_response(chunks):
    clock = [0.0]
    response = _Response(chunks, advance=lambda: clock.__setitem__(0, 16.0))
    session = _Session(response)
    transport = BoundedCapabilitySession(session, timeout_seconds=8, monotonic=lambda: clock[0])
    with pytest.raises(requests.Timeout):
        _get(transport)
    assert response.closed
    with pytest.raises(requests.Timeout):
        _get(transport)
    assert len(session.calls) == 1


@pytest.mark.parametrize("timeout", [0, -1, 61, float("nan"), float("inf")])
def test_transport_rejects_invalid_timeout_before_io(timeout):
    session = _Session()
    with pytest.raises(ValueError):
        BoundedCapabilitySession(session, timeout_seconds=timeout, monotonic=lambda: 0)
    assert not session.calls


def _cli(command, argv):
    return cli_main(["--config", str(PROJECT_ROOT / "config/runtime.json"), command, *argv])


@pytest.mark.parametrize("publication_failure", [False, True])
def test_bootstrap_closes_the_only_session_on_success_and_publication_failure(
    tmp_path, monkeypatch, capsys, publication_failure
):
    session = _Session()
    created = []

    def create_session():
        created.append(session)
        return session

    monkeypatch.setattr(requests, "Session", create_session)
    monkeypatch.setattr(bootstrap, "build_system", lambda *_a, **_kw: pytest.fail("server must not start"))
    if publication_failure:

        def fail_publication(*_args):
            raise RuntimeError("private artifact detail")

        monkeypatch.setattr(CapabilityEvidencePublisher, "publish", fail_publication)
    result = _cli(
        "research-h1-capability",
        ["--h1-runtime-dir", str(tmp_path / "input"), "--artifact-dir", str(tmp_path / "artifacts")],
    )
    payload = json.loads(capsys.readouterr().out)
    assert result == (2 if publication_failure else 1)
    assert payload["production_authority"] is False
    assert "private" not in json.dumps(payload)
    assert created == [session]
    assert session.closed and session.response.closed
    assert session.trust_env is False
    assert len(session.calls) == 2


@pytest.mark.parametrize(
    "case",
    ["repo_output", "details_stdout", "archive_summary", "h1_overlap", "holdout_overlap", "bad_code", "nan_timeout"],
)
def test_cli_rejects_invalid_boundaries_before_evidence_io(tmp_path, monkeypatch, capsys, case):
    monkeypatch.setattr(bootstrap, "execute_research_evidence", lambda _c: pytest.fail("must reject before I/O"))
    command = "research-industry-audit"
    argv = ["--history-root", str(tmp_path / "input")]
    if case == "repo_output":
        argv += ["--output", str(PROJECT_ROOT / "report.json")]
    elif case == "details_stdout":
        argv += ["--include-details"]
    elif case == "archive_summary":
        argv += ["--output", str(tmp_path / "input" / "control.sqlite3")]
    elif case == "h1_overlap":
        command = "research-h1-capability"
        argv = ["--h1-runtime-dir", str(tmp_path / "input"), "--artifact-dir", str(tmp_path / "input" / "artifacts")]
    elif case == "holdout_overlap":
        command = "research-terminal-holdout"
        argv = ["--parent-artifact-dir", str(tmp_path / "input"), "--output-dir", str(tmp_path / "input")]
    else:
        command = "research-data-qualification"
        argv += ["--code", "private-input"] if case == "bad_code" else ["--timeout-seconds", "nan"]
    assert _cli(command, argv) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["production_authority"] is False
    assert "private-input" not in json.dumps(payload)
    assert not (tmp_path / "input").exists()


def test_report_write_failure_returns_bounded_stdout_without_retry(tmp_path, monkeypatch, capsys):
    from trader.training.entrypoints import research_evidence_projection

    monkeypatch.setattr(bootstrap, "execute_research_evidence", lambda _c: object())
    monkeypatch.setattr(
        research_evidence_projection, "project_evidence_result", lambda *_a, **_kw: ({"status": "qualified"}, 0)
    )
    blocker = tmp_path / "file"
    blocker.write_text("preserve", encoding="utf-8")
    assert (
        _cli(
            "research-industry-audit",
            ["--history-root", str(tmp_path / "input"), "--output", str(blocker / "report.json")],
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "artifact_io_failed"
    assert str(tmp_path) not in json.dumps(payload)
    assert blocker.read_text(encoding="utf-8") == "preserve"


def test_ordinary_cli_validation_does_not_load_new_research_implementations():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from trader.entrypoints.cli import main; "
            "assert main(sys.argv[1:]) == 0; "
            "assert not any(name in sys.modules for name in ("
            "'trader.training.infra.research.command_evidence',"
            "'trader.training.infra.research.capability_http',"
            "'trader.training.application.capability_completion',"
            "'trader.training.application.terminal_holdout_execution'))",
            "--config",
            str(PROJECT_ROOT / "config/runtime.json"),
            "validate-config",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
