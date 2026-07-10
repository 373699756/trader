from __future__ import annotations

import json
import os
import tempfile
from typing import Any


def atomic_write_json(path: str, payload: Any, **dump_kwargs) -> None:
    text = json.dumps(payload, **dump_kwargs)
    atomic_write_text(path, text)


def atomic_write_text(path: str, text: str) -> None:
    target = os.fspath(path)
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".{}-".format(os.path.basename(target)),
        suffix=".tmp",
        dir=directory,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
