"""Field diagnostics distinguish sample schemas, usable fields and missing proof."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.download.company.company_structured_field_audit import _typed_fields
from scripts.download.company.company_field_audit import ProjectCompanyFieldAudit


def test_typed_holder_observation_keeps_missing_revision_explicit():
    now = datetime(2026, 11, 5, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    payload = {"success": True, "code": 0, "result": {"count": 1, "pages": 1, "data": [{
        "SECURITY_CODE": "600001", "NOTICE_DATE": "2026-11-04", "HOLDER_NAME": "示例股东",
        "DIRECTION": "增持", "CHANGE_NUM_SYMBOL": 1, "AFTER_CHANGE_RATE": 0.1,
        "START_DATE": "2026-11-01", "END_DATE": "2026-11-04", "PRIVATE": "hidden-provider-payload",
    }]}}
    result = _typed_fields("holder_trade", payload, now, now.date(), now.date(), 5, "a" * 64)
    assert result["status"] == "rejected"
    result = _typed_fields("holder_trade", payload, now, now.date().replace(day=1), now.date(), 5, "a" * 64)
    assert result["status"] == "decoded" and result["observations"] == 1
    assert result["reasons"] == {"point_in_time_unverified": 1}
    assert not result["production_adopted"] and "hidden-provider-payload" not in json.dumps(result)


def test_field_projection_does_not_promote_legacy_values_or_expose_unlisted_fields():
    source = {"field_results": {
        "derived_from_current_facts": True, "field_correctness_verified": False,
        "field_evidence_locator_verified": False, "applicable": 1,
        "states": {"present_unverified": 1}, "truncated": False,
        "items": [{"code": "600001", "field": "contract_amount", "state": "present_unverified", "PRIVATE": "hidden"}],
    }, "material_dependencies": {"ledger_available": False, "required_reviews": None, "items": []}}
    result = ProjectCompanyFieldAudit(source)
    assert not result["field_results"]["field_correctness_verified"]
    assert result["field_results"]["states"] == {"present_unverified": 1}
    assert result["material_dependencies"]["required_reviews"] is None
    assert "hidden" not in json.dumps(result)
