import app as server_entry


def test_server_without_reloader_owns_port_and_runtime():
    plan = server_entry._server_process_plan(False, {})

    assert plan.acquire_port is True
    assert plan.start_runtime is True


def test_reloader_parent_owns_port_without_starting_runtime():
    plan = server_entry._server_process_plan(True, {})

    assert plan.acquire_port is True
    assert plan.start_runtime is False


def test_reloader_child_starts_runtime_without_cleaning_parent_port_lock():
    plan = server_entry._server_process_plan(True, {"WERKZEUG_RUN_MAIN": "true"})

    assert plan.acquire_port is False
    assert plan.start_runtime is True
