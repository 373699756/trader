import os
import errno
import json
import socket
import time
import signal
import atexit
from pathlib import Path
from typing import Any, Dict, Optional

from stock_analyzer.app import create_app
from stock_analyzer import config


_PROJECT_ROOT = Path(__file__).resolve().parent
_LOCK_FILE = _PROJECT_ROOT / ".runtime" / "app_server_port.lock"
_CURRENT_PID = os.getpid()


def _is_port_free(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            return True
    except OSError as exc:
        if exc.errno in {errno.EADDRINUSE, errno.EACCES}:
            return False
        raise


def _load_lock_file() -> Optional[Dict[str, Any]]:
    if not _LOCK_FILE.exists():
        return None
    try:
        data = json.loads(_LOCK_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def _write_lock_file(host: str, port: int) -> None:
    try:
        _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LOCK_FILE.write_text(
            json.dumps(
                {
                    "pid": _CURRENT_PID,
                    "host": host,
                    "port": port,
                    "process_start_ticks": _process_start_ticks(_CURRENT_PID),
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        # 锁文件是辅助手段，写失败不阻止服务启动
        pass


def _release_lock_file() -> None:
    try:
        current = _load_lock_file()
        if current and int(current.get("pid") or 0) == _CURRENT_PID:
            _LOCK_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_cmdline(pid: int) -> list[str]:
    if os.name != "posix":
        return []
    cmdline = Path(f"/proc/{pid}/cmdline")
    if not cmdline.exists():
        return []
    try:
        return [
            item.decode("utf-8", errors="ignore")
            for item in cmdline.read_bytes().split(b"\0")
            if item
        ]
    except Exception:
        return []


def _process_start_ticks(pid: int) -> str:
    if os.name != "posix":
        return ""
    try:
        # /proc/<pid>/stat field 22 is the kernel start time.  Split after the
        # final ')' because the process name itself may contain spaces.
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        return str(fields[19])
    except (IndexError, OSError, ValueError):
        return ""


def _entry_script_arg(args: list[str]) -> str:
    if not args:
        return ""
    if Path(args[0]).name.startswith("app.py"):
        return args[0]
    index = 1
    flags_with_value = {"-W", "-X"}
    while index < len(args) and args[index].startswith("-"):
        if args[index] in {"-c", "-m"}:
            return ""
        index += 2 if args[index] in flags_with_value else 1
    if index >= len(args):
        return ""
    return args[index]


def _is_our_app_process(pid: int) -> bool:
    """Accept only the exact project entry point recorded by our lock file."""
    args = _process_cmdline(pid)
    if not args:
        return False
    expected_entry = _PROJECT_ROOT / "app.py"
    entry_arg = _entry_script_arg(args)
    if not entry_arg:
        return False
    try:
        entry = Path(entry_arg)
        if not entry.is_absolute():
            process_cwd = Path(f"/proc/{pid}/cwd").resolve()
            entry = process_cwd / entry
        return entry.resolve() == expected_entry
    except (OSError, RuntimeError):
        return False


def _terminate_process(pid: int) -> None:
    if os.name == "nt":
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        return

    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not _is_process_alive(pid):
            return
        time.sleep(0.1)

    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


def _cleanup_stale_lock(port: int) -> str:
    lock = _load_lock_file()
    if not lock:
        return "missing"
    if int(lock.get("port") or 0) != port:
        return "other_port"
    pid = int(lock.get("pid") or 0)
    if not pid or pid == _CURRENT_PID:
        return "invalid"

    if not _is_process_alive(pid):
        try:
            _LOCK_FILE.unlink()
        except FileNotFoundError:
            pass
        return "removed"

    if not _is_our_app_process(pid):
        return "foreign"
    lock_start_ticks = str(lock.get("process_start_ticks") or "")
    if not lock_start_ticks or lock_start_ticks != _process_start_ticks(pid):
        return "foreign"

    print(f"检测到端口 {port} 残留锁文件，旧进程 {pid} 可能未退出，尝试退出该进程。")
    _terminate_process(pid)
    return "terminated"


def _acquire_port(host: str, port: int) -> int:
    retries = max(0, int(os.getenv("PORT_CLEANUP_RETRIES", "20")))
    interval = float(os.getenv("PORT_CLEANUP_RETRY_INTERVAL_SECONDS", "0.5") or 0.5)

    if _is_port_free(host, port):
        _write_lock_file(host, port)
        return port

    for _ in range(retries + 1):
        lock_status = _cleanup_stale_lock(port)
        if lock_status == "foreign":
            raise RuntimeError(
                f"端口 {port} 已被占用，锁文件 PID 不是本项目入口；拒绝终止其他进程。"
            )
        if lock_status in {"missing", "other_port", "invalid"}:
            raise RuntimeError(
                f"端口 {port} 已被其他进程占用，请先关闭该进程或改用其他端口。"
            )
        if _is_port_free(host, port):
            _write_lock_file(host, port)
            return port
        time.sleep(interval)

    raise RuntimeError(f"端口 {port} 仍被占用，请先关闭旧进程后重试。")


app = create_app()


if __name__ == "__main__":
    host = os.getenv("HOST") or os.getenv("FLASK_RUN_HOST") or str(config.SERVER_HOST)
    port_raw = os.getenv("PORT") or os.getenv("FLASK_RUN_PORT") or str(config.SERVER_PORT)
    port = int(port_raw)
    resolved_port = _acquire_port(host, port)
    atexit.register(_release_lock_file)

    app.run(
        host=host,
        port=resolved_port,
        debug=bool(config.SERVER_DEBUG),
        use_reloader=bool(config.SERVER_DEBUG) and bool(config.SERVER_USE_RELOADER),
    )
