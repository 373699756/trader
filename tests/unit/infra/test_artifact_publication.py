from pathlib import Path

from trader.infra.artifacts.sealing import publish_immutable, publish_immutable_file, replace_file


def test_publish_immutable_creates_then_refuses_to_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "artifact.json"

    assert publish_immutable(target, '{"a":1}') is True
    assert target.read_text(encoding="utf-8") == '{"a":1}'

    assert publish_immutable(target, '{"a":2}') is False
    assert target.read_text(encoding="utf-8") == '{"a":1}'


def test_publish_immutable_leaves_no_temporary_files(tmp_path: Path) -> None:
    target = tmp_path / "artifact.json"

    publish_immutable(target, "first")
    publish_immutable(target, "second")

    assert [entry.name for entry in tmp_path.iterdir()] == ["artifact.json"]


def test_publish_immutable_file_copies_once(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    target = tmp_path / "copies" / "artifact.bin"

    assert publish_immutable_file(target, source) is True
    assert target.read_bytes() == b"payload"

    assert publish_immutable_file(target, source) is False
    assert target.read_bytes() == b"payload"


def test_replace_file_overwrites_and_keeps_directory_clean(tmp_path: Path) -> None:
    target = tmp_path / "runtime.json"

    replace_file(target, "first")
    replace_file(target, "second")

    assert target.read_text(encoding="utf-8") == "second"
    assert [entry.name for entry in tmp_path.iterdir()] == ["runtime.json"]
