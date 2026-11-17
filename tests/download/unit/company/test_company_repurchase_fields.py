"""Exact current buyback observations never turn plans, old tables or other actors into proof."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trader.download.domain.company_event_extraction import ExtractCompanyFieldEvidence, ExtractCompanyLinkCandidate
from trader.download.domain.company_events import CompanyEventPolicy, CompanyPositiveStatus
from trader.download.domain.company_evidence import CompanyDocument, Disclosure

_AT = datetime(2026, 11, 17, tzinfo=ZoneInfo("Asia/Shanghai"))
_POLICY = CompanyEventPolicy((0.1, 0.3, 0.5), (5.0, 10.0, 20.0), 365, 90, 180)
_TITLE = "关于以集中竞价交易方式首次回购公司股份的公告"


def _document(text, *, title=_TITLE):
    return CompanyDocument(Disclosure("cninfo", "current", ("600001",), title, _AT, "", "current-record"),
                           "证券代码：600001\n本公司保证信息真实。\n" + text, True)


def _table(quantity="1.00万", ratio="0.1"):
    return ("回购用途\n□减少注册资本\n√用于员工持股计划或股权激励\n□为维护公司价值及股东权益\n"
            f"累计已回购股数\n{quantity}股\n累计已回购股数占总股本比例\n{ratio}%\n累计已回购金额\n10万元。")


def _actual(document):
    return tuple(field for field in ExtractCompanyFieldEvidence(document, _AT, _POLICY) if field.field == "actual_pct")


def test_literal_cumulative_table_keeps_its_original_lines_and_does_not_reward_employee_plan():
    document = _document(_table())
    field, = _actual(document)
    assert field.value == 0.1 and field.evidence[0].quote in document.text
    assert field.evidence[0].quote == "累计已回购股数\n1.00万股\n累计已回购股数占总股本比例\n0.1%"
    event = ExtractCompanyLinkCandidate(document, _AT, _POLICY).positive
    assert event.measurements.actual_ratio_pct == 0.1 and event.status is CompanyPositiveStatus.IMPLEMENTING
    assert event.positive_level == 0 and not event.level_milestones


@pytest.mark.parametrize("introduction", (
    "根据法律规定，现将公司首次回购股份的情况披露如下：",
    "根据法律规定，现将本次回购股份情况公告如下：",
))
def test_current_disclosure_introduction_is_not_a_future_action(introduction):
    document = _document(introduction + "公司于2026年11月16日通过集中竞价交易方式首次回购公司股份10,000股，"
                         "已回购股份占公司当前总股本的比例为0.1%。")
    field, = _actual(document)
    assert field.value == 0.1 and "首次回购" in field.evidence[0].quote


@pytest.mark.parametrize("prefix", ("公司拟", "公司计划", "公司尚未", "如果满足条件，公司将"))
def test_disclosure_introduction_does_not_override_an_unconfirmed_actual_action(prefix):
    document = _document("现将公司首次回购股份的情况披露如下：" + prefix +
                         "首次回购公司股份10,000股，已回购股份占公司总股本比例为0.1%。")
    assert not _actual(document)


def test_hypothetical_context_before_the_introduction_remains_unconfirmed():
    document = _document("如果条件满足，现将公司首次回购情况披露如下：公司首次回购股份10,000股，"
                         "占公司总股本比例为0.1%。")
    assert not _actual(document)


@pytest.mark.parametrize("quantity,ratio", (("0", "0.1"), ("1", "101"), ("0.00001万", "0.1")))
def test_invalid_or_noninteger_quantity_cannot_escape_through_the_sentence_parser(quantity, ratio):
    assert not _actual(_document(_table(quantity, ratio)))


@pytest.mark.parametrize("context", ("本公司尚未实施回购。", "子公司已开展回购。", "本段为历史回购。"))
def test_table_cannot_override_explicit_nonimplementation_other_actor_or_history(context):
    assert not _actual(_document(context + _table()))


def test_a_plan_title_with_an_execution_shaped_table_is_not_an_execution_fact():
    assert not _actual(_document(_table(), title="关于回购股份计划的公告"))


def test_first_purchase_words_do_not_make_a_planned_title_an_actual_action():
    assert not _actual(_document(_table(), title="关于拟首次回购公司股份的公告"))


def test_a_hypothetical_cumulative_table_remains_unconfirmed():
    assert not _actual(_document("若股东会审议通过：" + _table()))


@pytest.mark.parametrize("title", (
    "关于2026年第二次回购A股股份方案首次回购股份的公告",
    "关于2026年以集中竞价交易方式回购公司股份方案的首次回购股份公告",
))
def test_first_execution_of_a_named_scheme_is_not_a_future_scheme(title):
    field, = _actual(_document(_table(), title=title))
    assert field.value == 0.1


def test_a_first_purchase_scheme_without_a_current_execution_title_remains_unconfirmed():
    assert not _actual(_document(_table(), title="关于首次回购股份方案的公告"))


def test_no_new_purchases_in_a_dated_month_does_not_deny_the_explicit_cumulative_value():
    columns = "累计已回购股数\n累计已回购股数占总股本比例\n累计已回购金额\n1,238.36万股\n2.46%\n8,660.67万元\n"
    body = (columns + "现将回购股份进展情况公告如下：2026年8月，公司未回购股份。"
            "截至2026年8月31日，公司通过集中竞价交易方式已累计回购股份1,238.36万股，"
            "占公司总股本的比例为2.46%。")
    field, = _actual(_document(body, title="关于以集中竞价交易方式回购股份的进展公告"))
    assert field.value == 2.46 and "累计回购股份1,238.36万股" in field.evidence[0].quote


@pytest.mark.parametrize("denial", ("公司未回购股份。", "如果2026年8月，公司未回购股份。"))
def test_unqualified_or_hypothetical_denial_is_not_treated_as_a_dated_month(denial):
    assert not _actual(_document(_table() + denial))


def test_completion_title_retains_the_existing_explicit_execution_sentence_owner():
    document = _document(_table() + "本公司已累计回购公司股份10,000股，占公司总股本比例为0.1%。",
                         title="关于回购股份实施结果暨股份变动的公告")
    field, = _actual(document)
    assert field.value == 0.1 and field.evidence[0].quote == "累计回购公司股份10,000股，占公司总股本比例为0.1%"
    assert "累计已回购股数" not in field.evidence[0].quote


@pytest.mark.parametrize("context,title", (
    ("", "关于拟回购股份实施结果的公告"),
    ("本公司尚未实施回购。", "关于回购股份实施结果的公告"),
    ("子公司已开展回购。", "关于回购股份实施结果的公告"),
    ("", "关于回购股份实施结果的更正公告"),
))
def test_completion_fallback_does_not_override_plans_negation_other_actor_or_unscoped_correction(context, title):
    document = _document(context + _table() + "本公司已累计回购公司股份10,000股，占公司总股本比例为0.1%。", title=title)
    assert not _actual(document)


def test_conflicting_narrative_and_table_actual_ratio_stays_unverified():
    document = _document(_table() + "本公司首次回购股份10,000股，占公司总股本比例为0.2%。")
    assert not _actual(document)


def test_first_action_quantity_must_agree_with_its_table_unit():
    document = _document(_table("10,000万") + "本公司首次回购股份10,000股，占公司总股本1000万股的比例为0.1%。")
    assert not _actual(document)


def test_correction_only_proves_the_explicit_after_table_and_rejects_unscoped_old_table():
    body = "本公司现对相关内容予以更正如下：一、重要内容提示（更正前）" + _table("10,000万")
    body += "二、重要内容提示（更正后）" + _table("10,000") + "除上述更正外，原公告其他内容不变。"
    document = _document(body, title="关于首次回购公司股份的更正公告")
    field, = _actual(document)
    assert field.value == 0.1 and "10,000股" in field.evidence[0].quote and "万股" not in field.evidence[0].quote
    assert not _actual(_document(body.replace("二、重要内容提示（更正后）", "二、相关情况"),
                                 title="关于首次回购公司股份的更正公告"))


def test_unchanged_ratio_and_quantity_in_correction_use_an_after_section_locator():
    body = "一、重要内容提示（更正前）" + _table()
    body += "二、重要内容提示（更正后）" + _table() + "除上述更正外，原公告其他内容不变。"
    document = _document(body, title="关于首次回购股份的更正公告")
    field, = _actual(document)
    assert "更正后" in field.evidence[0].quote and field.evidence[0].quote in document.text


def test_unique_current_purpose_clause_is_usable_despite_an_earlier_identical_plan_purpose():
    document = _document("本公司首次回购股份10,000股，占公司总股本比例0.1%。"
                         "此前方案约定用于维护公司价值及股东权益。"
                         "本次回购股份用途为维护公司价值及股东权益，本次回购股份将在十二个月后出售。")
    field, = (field for field in ExtractCompanyFieldEvidence(document, _AT, _POLICY) if field.field == "repurchase_purpose")
    assert field.value == "other" and field.evidence[0].quote == "本次回购股份用途为维护公司价值及股东权益"


@pytest.mark.parametrize("extra", (
    "√用于注销并减少注册资本。", "√用于转换公司可转债。",
    "本次回购股份用途为用于员工持股计划。",
    "本次回购股份用途为维护公司价值及股东权益。",
))
def test_current_purpose_clause_does_not_override_conflicting_or_duplicate_declarations(extra):
    document = _document("本公司首次回购股份10,000股，占公司总股本比例0.1%。" + extra +
                         "本次回购股份用途为维护公司价值及股东权益。")
    assert not any(field.field == "repurchase_purpose" for field in ExtractCompanyFieldEvidence(document, _AT, _POLICY))
