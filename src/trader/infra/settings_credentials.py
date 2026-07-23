"""Protected local credential-file parsing shared by runtime integrations."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from trader.infra.settings_parser import ConfigurationError

CREDENTIAL_FILE_NAME = ".token_key"
_CREDENTIAL_KEYS = frozenset({"DEEPSEEK_API_KEY", "TUSHARE_TOKEN"})


def read_credential_file(path: Path, *, label: str, raw_key: str) -> dict[str, str]:
    content = _read_credential_content(path, label)
    return _parse_credentials(content, label=label, raw_key=raw_key)


def _read_credential_content(path: Path, label: str) -> str:
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ConfigurationError(f"{label} file must be a regular file")
        if os.name == "posix" and stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ConfigurationError(f"{label} file must not be accessible by group or other users")
        if metadata.st_size > 4096:
            raise ConfigurationError(f"{label} file is too large")
        content = path.read_text(encoding="utf-8")
    except ConfigurationError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError(f"{label} file cannot be read") from exc

    return content


def _parse_credentials(content: str, *, label: str, raw_key: str) -> dict[str, str]:
    lines = [line.strip() for line in content.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) == 1 and "=" not in lines[0]:
        return {raw_key: _credential_value(lines[0], label=label)}
    if not lines:
        raise ConfigurationError(f"{label} file contains no credentials")

    values: dict[str, str] = {}
    for line in lines:
        assignment = line.removeprefix("export ").strip() if line.startswith("export ") else line
        key, separator, raw_value = assignment.partition("=")
        key = key.strip()
        if not separator or key not in _CREDENTIAL_KEYS:
            raise ConfigurationError(f"{label} file contains an unsupported assignment")
        if key in values:
            raise ConfigurationError(f"{label} file contains a duplicate credential")
        values[key] = _credential_value(raw_value, label=label)
    return values


def _credential_value(raw: str, *, label: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    if not value or "\x00" in value:
        raise ConfigurationError(f"{label} file contains an empty or invalid credential")
    return value
