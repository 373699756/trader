"""Current grouping identity excludes refresh evidence and never invents members."""

from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from trader.download.domain.current_industry import (
    CurrentIndustryGap,
    CurrentIndustryMember,
    CurrentIndustryReference,
    CurrentIndustrySnapshot,
)

_AT = datetime(2026, 11, 6, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
_STANDARD = "中国上市公司协会上市公司行业分类标准"


def _member(code="600001", name="货币金融服务"):
    return CurrentIndustryMember(code, CurrentIndustryReference(
        "capco:J66" + name, _STANDARD, date(2024, 9, 30), "cninfo_industry", "industry-query:" + "a" * 64,
    ), name)


def _snapshot():
    return CurrentIndustrySnapshot(
        members=(_member(), _member("600002")),
        gaps=(CurrentIndustryGap("600003", "current_standard_absent"),),
        observed_at=_AT,
        refresh_after=_AT + timedelta(days=7),
        source="cninfo_industry",
        classification=_STANDARD,
        publication_hash="a" * 64,
    )


def test_projection_retains_evidence_and_reports_absent_codes_without_fake_industry():
    original = _snapshot()
    selected = original.project(("600001", "600003", "600004"))
    assert selected.members == (original.members[0],)
    assert selected.gaps == (
        CurrentIndustryGap("600003", "current_standard_absent"),
        CurrentIndustryGap("600004", "current_industry_not_published"),
    )
    assert selected.member("600001") is original.members[0]
    assert selected.member("600003") is selected.member("600004") is None
    assert selected.publication_hash == original.publication_hash
    assert selected.observed_at == original.observed_at
    assert selected.refresh_after == original.refresh_after
    assert selected.project(("600001",)).content_hash == original.project(("600001",)).content_hash
    with pytest.raises(FrozenInstanceError):
        selected.source = "other"


def test_grouping_identity_ignores_receipt_time_changed_on_and_publication_digest():
    original = _snapshot()
    refreshed = replace(
        original,
        observed_at=_AT + timedelta(days=7),
        refresh_after=_AT + timedelta(days=14),
        publication_hash="b" * 64,
        members=tuple(replace(member, reference=replace(
            member.reference, effective_date=date(2026, 11, 1), data_version="industry-query:" + "b" * 64,
        )) for member in original.members),
    )
    assert refreshed.content_hash == original.content_hash
    assert refreshed.project(("600001",)).content_hash == original.project(("600001",)).content_hash
    changed = replace(original, members=(_member(), _member("600002", "资本市场服务")))
    assert changed.content_hash != original.content_hash
    assert changed.project(("600001",)).content_hash == original.project(("600001",)).content_hash
    renamed = replace(original, members=(_member(name="金融服务"), original.members[1]))
    assert renamed.content_hash != original.content_hash
    assert replace(original, gaps=(CurrentIndustryGap("600003", "industry_records_empty"),)).content_hash != original.content_hash


@pytest.mark.parametrize("changes", (
    {"members": (_member("600002"), _member())},
    {"members": (_member(), _member())},
    {"gaps": (CurrentIndustryGap("600001", "industry_records_empty"),)},
    {"gaps": (CurrentIndustryGap("600003", "industry_records_empty"),) * 2},
    {"observed_at": _AT.replace(tzinfo=None)},
    {"observed_at": _AT.astimezone(timezone.utc)},
    {"refresh_after": _AT},
    {"refresh_after": _AT + timedelta(days=8)},
    {"publication_hash": "not-a-digest"},
    {"classification": "旧标准"},
    {"source": "different_source"},
    {"members": [_member()]},
))
def test_snapshot_rejects_invalid_population_or_publication_metadata(changes):
    with pytest.raises(ValueError, match="current_industry_snapshot"):
        replace(_snapshot(), **changes)


@pytest.mark.parametrize("codes", (("600002", "600001"), ("600001", "600001"), ("invalid",), ["600001"]))
def test_projection_requires_canonical_price_codes(codes):
    with pytest.raises(ValueError, match="population_invalid"):
        _snapshot().project(codes)


def test_empty_projection_is_explicit_and_member_requires_a_chinese_native_name():
    selected = _snapshot().project(())
    assert selected.members == selected.gaps == ()
    with pytest.raises(ValueError, match="member_invalid"):
        replace(_member(), name="unknown")
    with pytest.raises(ValueError, match="member_invalid"):
        replace(_member(), reference=replace(_member().reference, industry_id="capco:unknown"))
