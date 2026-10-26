"""Readonly document references exposed with recommendation explanations."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True, slots=True)
class CompanyDocumentReference:
    evidence_id: str
    title: str
    url: str | None
    disclosed_at: datetime
    text_status: Literal["extracted", "pending"]
