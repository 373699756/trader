"""Dependency-free elapsed-time prefix shared by explicit local commands."""

import re
import unicodedata


def ProgressDuration(seconds: float) -> str:
    hours, remainder = divmod(int(max(0, seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def CleanProgressText(text: str) -> str:
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(" " if char == "|" or unicodedata.category(char).startswith("C") else char for char in text)


def DisplayWidth(text: str) -> int:
    return sum(
        0 if unicodedata.category(char) in {"Mn", "Me"}
        else 2 if unicodedata.east_asian_width(char) in {"W", "F"}
        else 1 for char in text
    )


def PadProgressText(text: str, width: int, *, right: bool = False) -> str:
    value = CleanProgressText(text)
    padding = " " * max(0, width - DisplayWidth(value))
    return padding + value if right else value + padding


def ShortProgressText(text: str, width: int) -> str:
    value = CleanProgressText(text)
    if DisplayWidth(value) <= width:
        return value
    if width < 1:
        return ""
    result = ""
    for char in value:
        if DisplayWidth(result + char) > width - 1:
            break
        result += char
    return result + "…"


def ProgressPrefix(
    total_seconds: float,
    task: str,
    stage_seconds: float,
    state: str,
    *,
    stage_width: int = 52,
) -> tuple[str, str, str]:
    fields = (ProgressDuration(total_seconds), f"{CleanProgressText(task)} {ProgressDuration(stage_seconds)}", CleanProgressText(state))
    padded = tuple(PadProgressText(value, width) for value, width in zip(fields, (8, stage_width, 6), strict=True))
    return padded[0], padded[1], padded[2]


def _startup_progress() -> None:
    import os
    import sys
    import time
    from threading import Event, Lock, Thread

    started = float(os.environ["TRADER_COMMAND_STARTED_MONOTONIC"])
    lock = Lock()

    def emit(state: str, text: str) -> None:
        elapsed = time.monotonic() - started
        fields = (*ProgressPrefix(elapsed, "启动·运行环境检查", elapsed, state), CleanProgressText(text))
        with lock:
            print(" | ".join(fields), file=sys.stderr, flush=True)

    if sys.argv[1] != "--pipe":
        if sys.argv[1] not in {"开始", "处理中", "等待", "重试", "完成", "失败", "取消"}:
            raise ValueError("command_progress_state_invalid")
        emit(sys.argv[1], sys.argv[2])
        return
    stopped = Event()

    def heartbeat() -> None:
        while not stopped.wait(5):
            emit("等待", "环境依赖准备仍在进行")

    thread = Thread(target=heartbeat, name="command-environment-progress", daemon=False)
    thread.start()
    try:
        for line in sys.stdin:
            emit("处理中", line.rstrip())
    finally:
        stopped.set()
        thread.join()


__all__ = ["ProgressDuration", "ProgressPrefix", "CleanProgressText", "DisplayWidth", "PadProgressText", "ShortProgressText"]


if __name__ == "__main__":
    _startup_progress()
