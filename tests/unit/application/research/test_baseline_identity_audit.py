from __future__ import annotations

from dataclasses import dataclass

from trader.application.research.baseline_identity_audit import BaselineIdentityAuditService
from trader.domain.research.baseline_identity import BaselineIdentityClaim, source_hash


@dataclass(frozen=True)
class _Evidence:
    live_identity_available: bool = True

    def baseline_identity_claims(self) -> tuple[BaselineIdentityClaim, ...]:
        return (BaselineIdentityClaim("model", "v1", "v1", "fixture", source_hash("fixture")),)


def test_audit_service_only_projects_typed_evidence() -> None:
    result = BaselineIdentityAuditService(_Evidence()).execute()
    assert result.status == "baseline_identity_consistent"
    assert result.production_authority is False
