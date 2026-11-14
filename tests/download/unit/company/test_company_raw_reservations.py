"""Default and diagnostic quota scans enforce the same bounded disk admissions."""

import pytest

from trader.download.infra.company_raw_files import CompanyRawFiles


@pytest.mark.parametrize("candidate", [False, True])
def test_reservation_growth_counts_owned_files_and_unused_reservations_once(tmp_path, candidate):
    raw = CompanyRawFiles(
        tmp_path,
        capacity=1000,
        analysis_reserve=100,
        free_reserve=1,
        maximum_document_bytes=64 * 1024**2,
        scan_reservations_once=candidate,
    )
    first = tmp_path / "documents" / "first"
    second = tmp_path / "documents" / "second"
    with raw.reserve(first, 400):
        retained = first / "input.pdf"
        retained.write_bytes(b"x" * 100)
        with raw.reserve(second, 450):
            with pytest.raises(RuntimeError, match="company_raw_capacity_exhausted"), raw.reserve(tmp_path / "text", 51):
                pytest.fail("unused reservations must remain charged")
        assert retained.is_file()
    with raw.reserve(second, 800):
        assert retained.is_file()
    with pytest.raises(RuntimeError, match="company_raw_capacity_exhausted"), raw.reserve(second, 801):
        pytest.fail("retained input bytes must remain charged")
    assert retained.read_bytes() == b"x" * 100


@pytest.mark.parametrize("candidate", [False, True])
def test_analysis_reserve_remains_available_without_releasing_existing_inputs(tmp_path, candidate):
    raw = CompanyRawFiles(
        tmp_path,
        capacity=1000,
        analysis_reserve=300,
        free_reserve=1,
        maximum_document_bytes=64 * 1024**2,
        scan_reservations_once=candidate,
    )
    directory = tmp_path / "documents" / "retained"
    directory.mkdir(parents=True)
    retained = directory / "input.pdf"
    retained.write_bytes(b"x" * 700)
    with pytest.raises(RuntimeError, match="company_raw_capacity_exhausted"), raw.reserve(directory, 1):
        pytest.fail("ordinary downloads cannot consume the analysis reserve")
    with raw.reserve(directory, 300, analysis=True):
        assert retained.stat().st_size == 700


@pytest.mark.parametrize("candidate", [False, True])
def test_analysis_reservations_do_not_consume_download_headroom_twice(tmp_path, candidate):
    raw = CompanyRawFiles(
        tmp_path,
        capacity=1000,
        analysis_reserve=300,
        free_reserve=1,
        maximum_document_bytes=64 * 1024**2,
        scan_reservations_once=candidate,
    )
    directory = tmp_path / "documents" / "retained"
    directory.mkdir(parents=True)
    retained = directory / "input.pdf"
    retained.write_bytes(b"x" * 600)
    with raw.reserve(tmp_path / "documents" / "ocr", 250, analysis=True):
        with raw.reserve(tmp_path / "documents" / "download", 100):
            with raw.reserve(tmp_path / "text", 50, analysis=True):
                assert retained.stat().st_size == 600
            with pytest.raises(RuntimeError, match="company_raw_capacity_exhausted"):
                with raw.reserve(tmp_path / "text", 51, analysis=True):
                    pytest.fail("all outstanding reservations must fit the total capacity")
        with pytest.raises(RuntimeError, match="company_raw_capacity_exhausted"):
            with raw.reserve(tmp_path / "documents" / "download", 101):
                pytest.fail("downloads must still leave the configured analysis headroom")
        with raw.reserve(tmp_path / "text", 150, analysis=True):
            assert retained.read_bytes() == b"x" * 600


@pytest.mark.parametrize("candidate", [False, True])
def test_materialized_analysis_bytes_remain_charged_to_capacity(tmp_path, candidate):
    raw = CompanyRawFiles(
        tmp_path,
        capacity=1000,
        analysis_reserve=300,
        free_reserve=1,
        maximum_document_bytes=64 * 1024**2,
        scan_reservations_once=candidate,
    )
    directory = tmp_path / "documents" / "ocr"
    directory.mkdir(parents=True)
    (directory / "input.pdf").write_bytes(b"x" * 600)
    with raw.reserve(directory, 250, analysis=True):
        scratch = directory / "page.png"
        scratch.write_bytes(b"x" * 50)
        with raw.reserve(tmp_path / "documents" / "download", 50):
            with raw.reserve(tmp_path / "text", 100, analysis=True):
                assert raw.usage() == 650
            with pytest.raises(RuntimeError, match="company_raw_capacity_exhausted"):
                with raw.reserve(tmp_path / "text", 101, analysis=True):
                    pytest.fail("materialized bytes and unused reservations exceed total capacity")
        with pytest.raises(RuntimeError, match="company_raw_capacity_exhausted"):
            with raw.reserve(tmp_path / "documents" / "download", 51):
                pytest.fail("materialized scratch bytes remain part of actual usage")
    assert scratch.read_bytes() == b"x" * 50


@pytest.mark.parametrize("candidate", [False, True])
def test_analysis_reservations_still_count_against_physical_free_space(tmp_path, monkeypatch, candidate):
    import shutil

    disk = shutil.disk_usage(tmp_path)._replace(free=350)
    monkeypatch.setattr("trader.download.infra.company_raw_files.shutil.disk_usage", lambda _path: disk)
    raw = CompanyRawFiles(
        tmp_path,
        capacity=1000,
        analysis_reserve=300,
        free_reserve=1,
        maximum_document_bytes=64 * 1024**2,
        scan_reservations_once=candidate,
    )
    with raw.reserve(tmp_path / "documents" / "ocr", 250, analysis=True):
        with pytest.raises(RuntimeError, match="company_raw_capacity_exhausted"):
            with raw.reserve(tmp_path / "documents" / "download", 100):
                pytest.fail("analysis reservations must also protect physical free space")
        with raw.reserve(tmp_path / "documents" / "download", 99):
            assert raw.usage() == 0
