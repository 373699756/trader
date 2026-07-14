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


def _is_our_app_process(pid: int) -> bool:
    if os.name != "posix":
        return False
    cmdline = Path(f"/proc/{pid}/cmdline")
    if not cmdline.exists():
        return False
    try:
        text = cmdline.read_bytes().decode("utf-8", errors="ignore")
        return "app.py" in text or "stock_analyzer.app" in text
    except Exception:
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


def _cleanup_stale_lock(port: int) -> None:
    lock = _load_lock_file()
    if not lock:
        return
    if int(lock.get("port") or 0) != port:
        return
    pid = int(lock.get("pid") or 0)
    if not pid or pid == _CURRENT_PID:
        return

    if not _is_process_alive(pid):
        try:
            _LOCK_FILE.unlink()
        except FileNotFoundError:
            pass
        return

    if not _is_our_app_process(pid):
        return

    print(f"检测到端口 {port} 残留锁文件，旧进程 {pid} 可能未退出，尝试退出该进程。")
    _terminate_process(pid)


def _tcp_socket_inodes_for_port(port: int) -> set[str]:
    target_port = int(port)
    inodes: set[str] = set()
    for table_path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table_path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()
        except FileNotFoundError:
            continue
        except Exception:
            continue
        for line in lines[1:]:
            parts = line.split()
            if len(parts) < 3:
                continue
            local_addr = parts[1]
            if ":" not in local_addr:
                continue
            try:
                _, port_hex = local_addr.rsplit(":", 1)
                if int(port_hex, 16) != target_port:
                    continue
            except Exception:
                continue
            inode = str(parts[-1]).strip()
            if inode:
                inodes.add(inode)
    return inodes


def _cleanup_stale_port_holders(port: int) -> None:
    inodes = _tcp_socket_inodes_for_port(port)
    if not inodes:
        return
    for pid_text in os.listdir("/proc"):
        if not pid_text.isdigit():
            continue
        pid = int(pid_text)
        if pid == _CURRENT_PID:
            continue
        if not _is_process_alive(pid):
            continue
        if not _is_our_app_process(pid):
            continue
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except Exception:
            continue
        for fd in fds:
            try:
                target = os.readlink(f"{fd_dir}/{fd}")
            except Exception:
                continue
            if not target.startswith("socket:["):
                continue
            inode = target[8:-1]
            if inode in inodes:
                print(f"检测到端口 {port} 被本项目旧进程 {pid} 持有，尝试退出该进程。")
                _terminate_process(pid)
                break


def _acquire_port(host: str, port: int) -> int:
    retries = max(0, int(os.getenv("PORT_CLEANUP_RETRIES", "20")))
    interval = float(os.getenv("PORT_CLEANUP_RETRY_INTERVAL_SECONDS", "0.5") or 0.5)

    if _is_port_free(host, port):
        _write_lock_file(host, port)
        return port

    for _ in range(retries + 1):
        _cleanup_stale_lock(port)
        _cleanup_stale_port_holders(port)
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
