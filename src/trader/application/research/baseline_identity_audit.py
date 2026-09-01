"""Application service for the read-only current baseline identity audit."""

from __future__ import annotations

from typing import Protocol

from trader.domain.research.baseline_identity import BaselineIdentityAudit, BaselineIdentityClaim


class BaselineIdentityEvidence(Protocol):
    def baseline_identity_claims(self) -> tuple[BaselineIdentityClaim, ...]: ...

    @property
    def live_identity_available(self) -> bool: ...


class BaselineIdentityAuditService:
    """Build an audit without networking, scoring, training, or persistence."""

    def __init__(self, evidence: BaselineIdentityEvidence) -> None:
        self._evidence = evidence

    def execute(self) -> BaselineIdentityAudit:
        return BaselineIdentityAudit(
            self._evidence.baseline_identity_claims(),
            live_identity_available=self._evidence.live_identity_available,
        )


__all__ = ["BaselineIdentityAuditService", "BaselineIdentityEvidence"]
