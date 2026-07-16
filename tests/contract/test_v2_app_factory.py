from __future__ import annotations

import threading

from trader.web import create_app


def test_create_app_has_no_thread_or_filesystem_side_effects(tmp_path, monkeypatch) -> None:
    started: list[str] = []
    original_start = threading.Thread.start

    def record_start(thread: threading.Thread) -> None:
        started.append(thread.name)
        original_start(thread)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(threading.Thread, "start", record_start)

    app = create_app()

    assert app is not None
    assert started == []
    assert list(tmp_path.iterdir()) == []
    response = app.test_client().get("/api/status")
    assert response.status_code == 200
    assert response.get_json()["status"] == "not_ready"


def test_dashboard_uses_packaged_v2_assets() -> None:
    app = create_app()

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert "A股策略看板" in response.get_data(as_text=True)
    assert "策略验证" not in response.get_data(as_text=True)
    assert app.test_client().get("/static/dashboard.css").status_code == 200
    assert app.test_client().get("/static/dashboard.js").status_code == 200
