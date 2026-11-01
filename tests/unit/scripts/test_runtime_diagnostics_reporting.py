from __future__ import annotations

import json
import pytest

from scripts.runtime_diagnostics.reporting import ReadJsonReports, emit_report, summarize_latency_ms


def test_summarize_latency_ms_uses_nearest_rank_p95() -> None:
    assert summarize_latency_ms([10.04, 40.06, 20.05]) == {
        "sample_count": 3,
        "p50_ms": 20.1,
        "p95_ms": 40.1,
        "maximum_ms": 40.1,
    }


def test_emit_report_writes_one_json_document_to_stdout(capsys) -> None:
    emit_report({"status": "passed", "count": 2, "message": "检查完成"})

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "passed", "count": 2, "message": "检查完成"}
    assert '\n  "message": "检查完成"\n' in captured.out
    assert captured.out.endswith("\n")
    assert captured.err == ""


@pytest.mark.parametrize("indent", (None, 2))
def test_read_complete_reports_with_nested_objects_and_text_progress(indent):
    expected = ({"schema_version": "profile_training_result", "heads": [{"message": '中文 {内容} 和 "引号"\n下一行'}]},
                {"schema_version": "selected_training_result", "history_rows_read": 0})
    output = "正在读取\n" + "\n进度完成\n".join(json.dumps(item, ensure_ascii=False, indent=indent) for item in expected) + "\n"
    assert ReadJsonReports(output) == expected


def test_truncated_multiline_report_is_not_silently_ignored():
    with pytest.raises(json.JSONDecodeError):
        ReadJsonReports('准备完成\n{\n  "schema_version": "profile_training_result",\n')


def test_readable_diagnostic_file_preserves_default_persistence_encoding(tmp_path):
    from trader.infra.atomic_files.json import AtomicReadJson, AtomicWriteJson

    payload = {"状态": "完成", "counts": [1, 2]}
    compact, pretty = tmp_path / "state.json", tmp_path / "report.json"
    AtomicWriteJson(compact, payload)
    AtomicWriteJson(pretty, payload, indent=2)
    assert compact.read_text() == json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert pretty.read_text() == json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    assert AtomicReadJson(compact) == AtomicReadJson(pretty) == payload
