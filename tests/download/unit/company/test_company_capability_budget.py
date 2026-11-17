"""Capability discovery obeys the caller's deadline and preserves interruption identity."""

import subprocess

import pytest

from trader.download.domain.company_sync import CompanySyncInterrupted
from trader.download.infra import company_document_ocr, company_document_parser


@pytest.mark.parametrize("kind,count", (("ocr", 3), ("text", 2)))
def test_every_local_probe_receives_only_remaining_total_time(monkeypatch, kind, count):
    clock, timeouts = [100.0], []
    monkeypatch.setattr("shutil.which", lambda name: name)

    def run(command, **kwargs):
        timeouts.append(kwargs["timeout"])
        clock[0] += 0.2
        return subprocess.CompletedProcess(command, 0, b"languages\nchi_sim\neng\n", b"version")

    monkeypatch.setattr(subprocess, "run", run)
    options = {"deadline": 101.0, "monotonic": lambda: clock[0]}
    result = company_document_ocr.DetectCompanyOcr("chi_sim+eng", **options) if kind == "ocr" else company_document_parser.DetectCompanyText(**options)
    assert result.available and timeouts == pytest.approx([1 - 0.2 * index for index in range(count)])


@pytest.mark.parametrize("kind", ("ocr", "text"))
@pytest.mark.parametrize("error_type", (CompanySyncInterrupted, InterruptedError))
def test_probe_never_turns_an_interruption_into_an_unavailable_tool(monkeypatch, kind, error_type):
    error = error_type("local_probe_interrupted")
    monkeypatch.setattr("shutil.which", lambda name: name)

    def interrupted(*args, **kwargs):
        raise error

    monkeypatch.setattr(subprocess, "run", interrupted)
    with pytest.raises(error_type) as caught:
        if kind == "ocr":
            company_document_ocr.DetectCompanyOcr("chi_sim+eng")
        else:
            company_document_parser.DetectCompanyText()
    assert caught.value is error
