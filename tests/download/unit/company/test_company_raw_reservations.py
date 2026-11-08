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
