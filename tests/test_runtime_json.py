import json
import os
import tempfile

import pytest

from stock_analyzer.runtime_json import atomic_write_json, atomic_write_text


def test_atomic_write_json_replaces_complete_document():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "nested", "state.json")
        atomic_write_json(path, {"version": 1}, ensure_ascii=False)
        atomic_write_json(path, {"version": 2, "状态": "完成"}, ensure_ascii=False)

        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)

        assert payload == {"version": 2, "状态": "完成"}
        assert os.listdir(os.path.dirname(path)) == ["state.json"]


def test_atomic_write_text_accepts_path_objects():
    with tempfile.TemporaryDirectory() as tmpdir:
        from pathlib import Path

        path = Path(tmpdir) / "quotes.json"
        atomic_write_text(path, "[]")

        assert path.read_text(encoding="utf-8") == "[]"


def test_serialization_failure_preserves_existing_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "state.json")
        atomic_write_json(path, {"version": 1})

        with pytest.raises(TypeError):
            atomic_write_json(path, {"invalid": {1, 2, 3}})

        with open(path, encoding="utf-8") as handle:
            assert json.load(handle) == {"version": 1}
