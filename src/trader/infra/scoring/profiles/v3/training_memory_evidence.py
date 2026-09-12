"""Typed evidence emitted by the Tomorrow V3 training memory gate."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class TomorrowTrainingMemoryEvidence:
    training_status: str
    repeat_training_status: str
    training_input_hash: str
    model_hash: str
    report_hash: str
    peak_rss_bytes: int
    max_rss_bytes: int

    def __post_init__(self) -> None:
        if (
            self.training_status != "engineering_ready"
            or self.repeat_training_status != "already_current"
            or any(
                _SHA256.fullmatch(value) is None
                for value in (self.training_input_hash, self.model_hash, self.report_hash)
            )
            or self.peak_rss_bytes < 1
            or self.max_rss_bytes < 1
            or self.peak_rss_bytes > self.max_rss_bytes
            or self.max_rss_bytes > 2048 * 1024 * 1024
        ):
            raise ValueError("Tomorrow training memory evidence is invalid")


__all__ = ["TomorrowTrainingMemoryEvidence"]
