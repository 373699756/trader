"""Cancellation of retained shares cannot renew a historical repurchase reward."""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from trader.download.domain.company_event_extraction import ExtractCompanyEvents, ExtractCompanyFieldEvidence
from trader.download.domain.company_evidence import CompanyDocument
from trader.download.domain.company_events import CompanyPositiveStatus


_BODY = (
    "证券代码：600001\n本公司于2022年审议回购方案。截至2023年8月17日，本公司累计回购股份9,522,792股，"
    "占公司总股本的比例为0.82%。回购股份用于注销并减少注册资本。"
    "本公司于本月办理上述已回购股份的注销手续。"
)
_TITLES = (
    "关于注销回购股份、减少注册资本暨修订《公司章程》的公告",
    "关于注销回购股份、减少注册资本暨通知债权人的公告",
    "关于回购股份注销完成暨股份变动的公告",
    "关于注销回购股份的公告",
    "关于回购股份注销完成的公告",
)


@pytest.mark.parametrize("title", _TITLES)
def test_cancellation_only_notice_never_certifies_or_rewards_an_old_repurchase(title):
    # CNInfo 1225441609 / 1225485548 quote the 2023 buyback in 2026 cancellation notices.
    document = CompanyDocument(fixtures._disclosure("cancel-shares", title), _BODY, True)
    event, = ExtractCompanyEvents((document,), fixtures.NOW, fixtures.CompanyEventPolicy()).positives
    assert event.measurements.actual_ratio_pct is None
    assert event.status is CompanyPositiveStatus.PLANNED
    assert event.positive_level == 0 and event.level_milestones == () and event.not_eligible_reason is not None
    assert not any(field.field == "actual_pct" for field in
                   ExtractCompanyFieldEvidence(document, fixtures.NOW, fixtures.CompanyEventPolicy()))


@pytest.mark.parametrize("title", (
    "关于首次回购股份暨注销部分股份的公告",
    "关于回购股份进展暨股份注销的公告",
    "关于注销回购股份暨实施新回购的公告",
))
def test_combined_new_action_requires_its_own_normal_measurement_verification(title):
    document = CompanyDocument(fixtures._disclosure("new-action", title),
                               "本公司首次回购股份100万股，占公司总股本比例为0.3%，回购股份用于注销。", True)
    fields = ExtractCompanyFieldEvidence(document, fixtures.NOW, fixtures.CompanyEventPolicy())
    assert next(item for item in fields if item.field == "actual_pct").value == 0.3


@pytest.mark.parametrize("independent", (False, True))
def test_exact_single_input_correction_clears_its_own_milestones_and_preserves_independent_history(independent):
    first = fixtures._disclosure("cancel-shares", "关于回购股份的公告")
    previous, = ExtractCompanyEvents((CompanyDocument(first, _BODY, True),), fixtures.NOW, fixtures.CompanyEventPolicy()).positives
    assert previous.positive_level == 3 and len(previous.level_milestones) == 3
    if independent:
        previous = replace(previous, event_id="independent-plan", evidence_ids=("independent-plan", first.identity),
                           first_disclosed_at=first.published_at - timedelta(days=2))
    document = CompanyDocument(replace(first, title=_TITLES[0]), _BODY, True)
    result = ExtractCompanyEvents((document,), fixtures.NOW, fixtures.CompanyEventPolicy(), previous_positives=(previous,))
    event, = result.positives
    if independent:
        assert event.measurements == previous.measurements and event.level_milestones == previous.level_milestones
    else:
        assert event.event_id == previous.event_id and event.evidence_ids == previous.evidence_ids
        assert event.measurements.actual_ratio_pct is None and event.level_milestones == ()
    assert event.positive_level == 0 and event.not_eligible_reason is not None
