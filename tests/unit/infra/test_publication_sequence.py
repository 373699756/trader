import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from trader.recommendation.infra.persistence.publication_sequence import PublicationSequence


def test_construction_is_inert_and_restart_skips_reserved_numbers(tmp_path):
    path = tmp_path / "sequence.sqlite3"
    first = PublicationSequence(path, block_size=8)
    assert not path.exists()
    with pytest.raises(RuntimeError, match="not_initialized"):
        first.allocate()
    first.initialize()
    local = first.allocate(width=2)
    assert first.allocate() == local + 2
    restarted = PublicationSequence(path, block_size=8)
    restarted.initialize()
    assert restarted.allocate() > local + 7
    assert first.allocate() < restarted.allocate()


def test_recovered_legacy_coordinate_advances_the_persistent_reservation(tmp_path):
    path = tmp_path / "sequence.sqlite3"
    first = PublicationSequence(path, block_size=8)
    first.initialize()
    legacy = 10_000
    new = first.allocate(width=2, minimum=legacy + 1)
    assert new > legacy
    restarted = PublicationSequence(path, block_size=8)
    restarted.initialize()
    assert restarted.allocate() > new + 1


def test_concurrent_instances_and_exhausted_blocks_never_reuse_coordinates(tmp_path):
    path = tmp_path / "sequence.sqlite3"
    instances = [PublicationSequence(path, block_size=8) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda sequence: sequence.initialize(), instances))
        values = list(pool.map(lambda index: instances[index % 4].allocate(width=2), range(200)))
    reserved = {number for start in values for number in (start, start + 1)}
    assert len(reserved) == 400
    restarted = PublicationSequence(path, block_size=8)
    restarted.initialize()
    assert restarted.allocate() > max(reserved)


@pytest.mark.parametrize("reserved_until", [-1, "corrupt", 2**63 - 1])
def test_invalid_persistent_coordinate_fails_closed(tmp_path, reserved_until):
    path = tmp_path / "sequence.sqlite3"
    first = PublicationSequence(path, block_size=8)
    first.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE publication_sequence SET reserved_until=?", (reserved_until,))
    with pytest.raises(RuntimeError, match="invalid"):
        PublicationSequence(path, block_size=8).initialize()


def test_deleted_reservation_cannot_reset_a_previous_sequence(tmp_path):
    path = tmp_path / "sequence.sqlite3"
    first = PublicationSequence(path, block_size=8)
    first.initialize()
    first.allocate()
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM publication_sequence")
    with pytest.raises(RuntimeError, match="invalid"):
        PublicationSequence(path, block_size=8).initialize()
