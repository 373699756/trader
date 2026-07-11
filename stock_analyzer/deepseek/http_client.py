from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, Dict, Optional

import requests


@dataclass
class DeepSeekHttpResult:
    parsed: object = None
    usage: Dict[str, object] = None
    error: str = ""
    attempts: int = 0
    timed_out: bool = False
    raw: object = None

    def __post_init__(self) -> None:
        if self.usage is None:
            self.usage = {}


class DeepSeekHttpClient:
    """HTTP boundary for DeepSeek chat calls."""

    RETRY_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(self, requests_module=requests, sleep_func: Callable[[float], None] = time.sleep) -> None:
        self._requests = requests_module
        self._sleep = sleep_func

    def post_json(
        self,
        url: str,
        *,
        headers: Dict[str, str],
        payload: Dict[str, object],
        timeout: float,
        retry_count: int = 0,
        retry_base_delay: float = 0.0,
        parse_content: Optional[Callable[[str], object]] = None,
    ) -> DeepSeekHttpResult:
        parsed = None
        raw = None
        usage: Dict[str, object] = {}
        last_error = ""
        timed_out = False
        attempt = 0
        retry_count = max(0, int(retry_count or 0))
        retry_base_delay = max(0.0, float(retry_base_delay or 0.0))

        while attempt <= retry_count:
            attempt += 1
            try:
                response = self._requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=float(timeout),
                )
                status_code = getattr(response, "status_code", None)
                if isinstance(status_code, int) and status_code in self.RETRY_STATUS_CODES and attempt <= retry_count:
                    last_error = "可重试响应码: {}".format(status_code)
                    self._sleep((2 ** (attempt - 1)) * retry_base_delay)
                    continue
                response.raise_for_status()
                raw = response.json()
                usage = raw.get("usage", {}) if isinstance(raw, dict) else {}
                if parse_content is None:
                    parsed = raw
                else:
                    content = ((raw.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "") if isinstance(raw, dict) else ""
                    parsed = parse_content(str(content))
                if parsed is not None:
                    break
                last_error = "响应无法解析 JSON"
            except Exception as exc:
                last_error = str(exc)
                timeout_type = getattr(getattr(self._requests, "exceptions", object), "Timeout", None)
                timed_out = bool(timeout_type and isinstance(exc, timeout_type)) or "timed out" in last_error.lower()
                if attempt <= retry_count:
                    self._sleep((2 ** (attempt - 1)) * retry_base_delay)
                else:
                    break

        return DeepSeekHttpResult(
            parsed=parsed,
            usage=usage,
            error=last_error,
            attempts=attempt,
            timed_out=timed_out,
            raw=raw,
        )
