"""Bounded official-document verification; ambiguous conclusions remain review items."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta

from trader.download.domain.company_events import (
    CompanyEventPolicy,
    CompanyEventStatus,
    CompanyLevelMilestone,
    CompanyMeasurements,
    CompanyPositiveEvent,
    CompanyPositiveStatus,
    CompanyPositiveType,
    CompanyReviewReason,
    CompanyRiskEvent,
    CompanyRiskType,
    CompanySeverityBasis,
    CompanySubject,
    ContractMeasurements,
    IncreaseMeasurements,
    ReductionPlan,
    RepurchaseMeasurements,
    RepurchasePurpose,
)
from trader.download.domain.company_evidence import CompanyDocument

_RISK_HINTS = (
    "立案",
    "调查",
    "处罚",
    "处分",
    "违法",
    "造假",
    "虚假",
    "资金占用",
    "违规担保",
    "违法担保",
    "退市",
    "终止上市",
    "减持",
    "警示函",
    "监管措施",
)
_POSITIVE_HINTS = ("回购股份", "回购公司", "股份回购", "增持", "重大合同", "经营合同", "签订合同", "签署合同")
_OFFICIAL_SOURCES = frozenset(
    {
        "cninfo",
        "issuer_disclosure",
        "exchange_disclosure",
        "regulator_disclosure",
        "court_disclosure",
        "judicial_disclosure",
        "csrc_disclosure",
    }
)
_CASE = re.compile(r"(?:证监立案字|[〔（(【]\d{4}[〕）)】])[\w—\-]{1,30}号")
_SUBJECT_MARKERS = (
    ("控股股东", CompanySubject.CONTROLLING_SHAREHOLDER),
    ("实际控制人", CompanySubject.ACTUAL_CONTROLLER),
    ("持股5%以上", CompanySubject.MAJOR_SHAREHOLDER),
    ("5%以上股东", CompanySubject.MAJOR_SHAREHOLDER),
    ("大股东", CompanySubject.MAJOR_SHAREHOLDER),
)


@dataclass(frozen=True, slots=True)
class CompanyExtractedEvents:
    risks: tuple[CompanyRiskEvent, ...]
    positives: tuple[CompanyPositiveEvent, ...]
    reviews: tuple[CompanyReviewReason, ...]
    checked_documents: int


@dataclass(frozen=True, slots=True)
class _VerifiedDocument:
    document: CompanyDocument
    title: str
    body: str
    subject: CompanySubject


def CompanyDocumentNeedsReview(title: str) -> bool:
    return any(marker in title for marker in (*_RISK_HINTS, *_POSITIVE_HINTS))


def CompanyDocumentRiskRelevant(title: str) -> bool:
    return any(marker in title for marker in _RISK_HINTS)


def ExtractCompanyEvents(
    documents: tuple[CompanyDocument, ...],
    observed_at: datetime,
    policy: CompanyEventPolicy,
    *,
    previous_risks: tuple[CompanyRiskEvent, ...] = (),
    previous_positives: tuple[CompanyPositiveEvent, ...] = (),
    previous_reviews: tuple[CompanyReviewReason, ...] = (),
) -> CompanyExtractedEvents:
    if observed_at.utcoffset() is None:
        raise ValueError("company_event_observation_requires_timezone")
    risks = list(previous_risks)
    positives = list(previous_positives)
    observed_evidence = {item.disclosure.identity for item in documents}
    reviews = [
        item for item in previous_reviews if item.evidence_id is not None and item.evidence_id not in observed_evidence
    ]
    checked = 0
    for document in sorted(documents, key=lambda item: (item.disclosure.published_at, item.disclosure.identity)):
        item = document.disclosure
        if item.published_at > observed_at or not CompanyDocumentNeedsReview(item.title):
            continue
        verified, review = _verify_document(document, observed_at)
        if review is not None:
            reviews.append(review)
            _suspend_positive(positives, item.identity, item.published_at)
        if verified is None:
            continue
        checked += 1
        resolved = _resolve_risks(risks, verified, policy)
        if _risk_closure(verified.title):
            if not resolved:
                reviews.append(_review(document, observed_at, "risk", "resolution_unlinked", "解除公告未关联原事件"))
            continue
        reviews.extend(_confirm_risks(risks, verified, observed_at, policy))
        positive_type = _positive_type(verified.title)
        if positive_type is not None:
            reason = _append_positive(positives, verified, positive_type, policy)
            if reason:
                reviews.append(_review(document, observed_at, "positive", reason, "利好实施、数值或同案关系尚未核实"))
    _suspend_reviewed_positives(positives, reviews, observed_at)
    return CompanyExtractedEvents(
        tuple(sorted(risks, key=lambda event: (event.risk_type.value, event.event_id))),
        tuple(sorted(positives, key=lambda event: (event.event_type.value, event.event_id))),
        tuple(sorted(set(reviews), key=lambda item: (item.scope, item.evidence_id or "", item.reason_code))),
        checked,
    )


def _suspend_reviewed_positives(
    positives: list[CompanyPositiveEvent], reviews: list[CompanyReviewReason], observed_at: datetime
) -> None:
    # A later progress document cannot override a pending/corrected prerequisite.
    for review in reviews:
        if review.scope == "positive" and review.evidence_id is not None:
            _suspend_positive(positives, review.evidence_id, observed_at, refresh_time=False)


def _verify_document(
    document: CompanyDocument, observed_at: datetime
) -> tuple[_VerifiedDocument | None, CompanyReviewReason | None]:
    title = "".join(document.disclosure.title.split())
    body = "".join(document.text.split())
    scope = "risk" if CompanyDocumentRiskRelevant(title) else "positive"
    if not document.checked or not body:
        return None, _review(document, observed_at, scope, "document_pending", "公告正文尚未完整提取")
    if document.disclosure.source not in _OFFICIAL_SOURCES:
        return None, _review(document, observed_at, scope, "official_source_unverified", "公告来源未核实")
    if _unrelated_subject(title):
        return None, None
    subject = _subject(title, body)
    if subject is None:
        return None, _review(document, observed_at, scope, "subject_unverified", "正文主体无法确认")
    return _VerifiedDocument(document, title, body, subject), None


def _confirm_risks(
    risks: list[CompanyRiskEvent], verified: _VerifiedDocument, observed_at: datetime, policy: CompanyEventPolicy
) -> list[CompanyReviewReason]:
    document = verified.document
    categories = _risk_categories(verified.title)
    body = _subject_body(verified)
    reviews: list[CompanyReviewReason] = []
    for category in categories:
        clauses = re.split(r"[。；;！？!?，,]", body)
        positive = "；".join(clause for clause in clauses if not _negated(clause, category))
        negated = any(_negated(clause, category) for clause in clauses)
        confirmed = category in _risk_categories(positive) and _risk_authority(positive, category)
        if len(set(_CASE.findall(body))) > 1 or negated and confirmed:
            reviews.append(
                _review(
                    document, observed_at, "risk", "case_conclusion_ambiguous", "同一主体的案件或肯否结论无法唯一绑定"
                )
            )
            continue
        if negated:
            if any(document.disclosure.identity in event.evidence_ids for event in risks):
                reviews.append(
                    _review(document, observed_at, "risk", "revision_pending", "更正结论待核实，保留已确认风险")
                )
            continue
        if not confirmed:
            reviews.append(
                _review(document, observed_at, "risk", "conclusion_unverified", "标题与正式正文结论未形成一致证据")
            )
            continue
        _append_risk(risks, replace(verified, body=body), category, policy)
    # Ordinary reduction progress and general reminders do not invent new hard facts.
    if not categories and any(
        marker in verified.title
        for marker in ("处罚", "处分", "违法", "造假", "立案", "退市", "警示函", "资金占用", "违规担保")
    ):
        reviews.append(
            _review(document, observed_at, "risk", "conclusion_unverified", "需要核实正式结论，尚不能确定风险等级")
        )
    return reviews


def _risk_categories(text: str) -> tuple[CompanyRiskType, ...]:
    result: list[CompanyRiskType] = []
    decision = any(x in text for x in ("行政处罚决定", "纪律处分决定", "处罚决定书", "判决书"))
    major_holder = any(x in text for x in ("控股股东", "实际控制人", "持股5%以上", "5%以上股东", "大股东"))
    reduction_start = any(x in text for x in ("拟减持", "减持股份预披露", "减持股份的预披露")) or (
        "减持计划" in text and not any(x in text for x in ("进展", "实施情况", "结果", "终止"))
    )
    if major_holder and reduction_start:
        result.append(CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION)
    if decision and any(x in text for x in ("财务造假", "虚假记载", "虚增收入", "虚增利润", "重大遗漏")):
        result.append(CompanyRiskType.FINANCIAL_FRAUD)
    if any(x in text for x in ("立案告知书", "收到立案", "决定立案", "被立案")):
        result.append(CompanyRiskType.OFFICIAL_INVESTIGATION)
    if decision and "重大违法" in text:
        result.append(CompanyRiskType.MAJOR_ILLEGAL)
    if decision and "资金占用" in text:
        result.append(CompanyRiskType.FUND_OCCUPATION)
    if decision and any(x in text for x in ("违规担保", "违法担保")):
        result.append(CompanyRiskType.ILLEGAL_GUARANTEE)
    if any(x in text for x in ("强制退市决定", "终止上市决定", "强制退市程序")):
        result.append(CompanyRiskType.FORCED_DELISTING)
    return tuple(result) or _soft_risk_categories(text)


def _soft_risk_categories(text: str) -> tuple[CompanyRiskType, ...]:
    if "行政处罚决定" in text or "行政处罚决定书" in text:
        return (CompanyRiskType.ADMINISTRATIVE_PENALTY,)
    if "纪律处分决定" in text:
        return (CompanyRiskType.DISCIPLINARY_ACTION,)
    if any(x in text for x in ("出具警示函", "收到警示函", "采取警示函")):
        return (CompanyRiskType.REGULATORY_WARNING,)
    return ()


def _append_risk(
    risks: list[CompanyRiskEvent],
    verified: _VerifiedDocument,
    category: CompanyRiskType,
    policy: CompanyEventPolicy,
) -> None:
    item = verified.document.disclosure
    body = verified.body
    case_ids = _CASE.findall(body)
    case_id = case_ids[0] if len(set(case_ids)) == 1 else None
    previous = next(
        (
            x
            for x in risks
            if x.risk_type is category
            and x.subject_type is verified.subject
            and (
                item.identity in x.evidence_ids
                or case_id is not None
                and case_id == x.case_id
                or _references(body, x.evidence_ids)
            )
        ),
        None,
    )
    soft = category in {
        CompanyRiskType.REGULATORY_WARNING,
        CompanyRiskType.ADMINISTRATIVE_PENALTY,
        CompanyRiskType.DISCIPLINARY_ACTION,
    }
    level = 1 if category is CompanyRiskType.REGULATORY_WARNING else (3 if "情节严重" in body else 2) if soft else 4
    basis = (
        CompanySeverityBasis.WARNING
        if level == 1
        else CompanySeverityBasis.ORDINARY
        if level == 2
        else CompanySeverityBasis.SERIOUS
        if level == 3
        else CompanySeverityBasis.HARD
    )
    first = previous.first_disclosed_at if previous else item.published_at
    expires = (
        first + timedelta(days=policy.risk_days)
        if soft or category in {CompanyRiskType.FINANCIAL_FRAUD, CompanyRiskType.MAJOR_ILLEGAL}
        else None
    )
    reduction_plan = previous.reduction_plan if previous else None
    if category is CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION:
        reduction_plan = _reduction_plan(body) or reduction_plan or ReductionPlan()
        if reduction_plan.planned_end_date is not None:
            expires = datetime.combine(reduction_plan.planned_end_date, time.max, first.tzinfo) + timedelta(
                days=policy.reduction_tail_days
            )
    event = CompanyRiskEvent(
        previous.event_id if previous else f"{item.identity}:{category.value}",
        category,
        max(level, previous.risk_level) if previous else level,
        verified.subject,
        previous.severity_basis if previous and previous.risk_level > level else basis,
        case_id or (previous.case_id if previous else None),
        first,
        max(item.published_at, previous.last_disclosed_at) if previous else item.published_at,
        first,
        previous.status if previous else CompanyEventStatus.ACTIVE,
        previous.resolved_at if previous else None,
        previous.expires_at if previous and previous.resolved_at else expires,
        item.title[:240],
        _evidence(previous.evidence_ids if previous else (), item.identity),
        reduction_plan,
    )
    if previous:
        risks[risks.index(previous)] = event
    else:
        risks.append(event)


def _risk_closure(text: str) -> tuple[CompanyRiskType, ...]:
    result: list[CompanyRiskType] = []
    if any(x in text for x in ("减持完成", "减持结果", "减持期限届满", "终止减持", "提前终止减持")):
        result.append(CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION)
    if any(x in text for x in ("撤销立案", "终止调查", "调查终结", "不予处罚决定")):
        result.append(CompanyRiskType.OFFICIAL_INVESTIGATION)
    if any(x in text for x in ("撤回终止上市", "终止强制退市", "撤销退市决定")):
        result.append(CompanyRiskType.FORCED_DELISTING)
    if "资金占用" in text and any(x in text for x in ("全部归还", "全部清偿", "解除")):
        result.append(CompanyRiskType.FUND_OCCUPATION)
    if "担保" in text and any(x in text for x in ("解除", "全部终止")):
        result.append(CompanyRiskType.ILLEGAL_GUARANTEE)
    if "撤销" in text and "处罚" in text:
        result.extend(
            (CompanyRiskType.ADMINISTRATIVE_PENALTY, CompanyRiskType.FINANCIAL_FRAUD, CompanyRiskType.MAJOR_ILLEGAL)
        )
    if "撤销" in text and "警示函" in text:
        result.append(CompanyRiskType.REGULATORY_WARNING)
    if "撤销" in text and "处分" in text:
        result.append(CompanyRiskType.DISCIPLINARY_ACTION)
    return tuple(result)


def _reduction_plan(body: str) -> ReductionPlan | None:
    dates = re.search(
        r"减持(?:计划)?期间(?:为|：|:)?(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})日?(?:至|到|~)(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})日?",
        body,
    )
    if dates is None:
        return None
    try:
        start = date(*(int(dates[i]) for i in (1, 2, 3)))
        end = date(*(int(dates[i]) for i in (4, 5, 6)))
    except ValueError:
        return None
    return ReductionPlan(start, end) if start <= end else None


def _resolve_risks(risks: list[CompanyRiskEvent], verified: _VerifiedDocument, policy: CompanyEventPolicy) -> bool:
    document, title, body = verified.document, verified.title, _subject_body(verified)
    if any(marker in body for marker in ("尚未", "未解除", "未终止", "未结案", "未完成")):
        return False
    case_ids = set(_CASE.findall(body))
    if len(case_ids) > 1:
        return False
    matched = False
    for index, event in enumerate(risks):
        if event.subject_type is not verified.subject:
            continue
        if event.risk_type not in _risk_closure(title) or event.risk_type not in _risk_closure(body):
            continue
        if event.case_id and case_ids and event.case_id not in case_ids:
            continue
        if not (_references(body, event.evidence_ids) or event.case_id and event.case_id in _CASE.findall(body)):
            continue
        at = document.disclosure.published_at
        if at < event.first_disclosed_at:
            continue
        at = min(at, event.resolved_at) if event.resolved_at is not None else at
        tail = (
            timedelta(days=policy.reduction_tail_days)
            if event.risk_type is CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION
            else timedelta(0)
        )
        risks[index] = replace(
            event,
            status=CompanyEventStatus.RESOLVED,
            resolved_at=at,
            expires_at=at + tail,
            last_disclosed_at=max(event.last_disclosed_at, document.disclosure.published_at),
            evidence_ids=_evidence(event.evidence_ids, document.disclosure.identity),
            reduction_plan=replace(event.reduction_plan, completion_disclosed_at=at) if event.reduction_plan else None,
        )
        matched = True
    return matched


def _positive_type(title: str) -> CompanyPositiveType | None:
    if "增持" in title:
        return CompanyPositiveType.SHAREHOLDER_INCREASE
    if any(x in title for x in ("回购股份", "回购公司", "股份回购")):
        return CompanyPositiveType.SHARE_REPURCHASE
    if any(x in title for x in ("重大合同", "经营合同", "签订合同", "签署合同")):
        return CompanyPositiveType.OPERATING_CONTRACT
    return None


def _append_positive(
    positives: list[CompanyPositiveEvent],
    verified: _VerifiedDocument,
    kind: CompanyPositiveType,
    policy: CompanyEventPolicy,
) -> str | None:
    item = verified.document.disclosure
    body = _subject_body(verified)
    previous = next(
        (
            x
            for x in positives
            if x.event_type is kind and (item.identity in x.evidence_ids or _references(body, x.evidence_ids))
        ),
        None,
    )
    title = "".join(item.title.split())
    if previous is not None and previous.status is CompanyPositiveStatus.CANCELLED:
        return None
    if any(x in title for x in ("终止", "撤销", "取消")):
        if previous is None:
            return "cancellation_unlinked"
        positives[positives.index(previous)] = replace(
            previous,
            status=CompanyPositiveStatus.CANCELLED,
            cancelled_at=item.published_at,
            last_disclosed_at=item.published_at,
            evidence_ids=_evidence(previous.evidence_ids, item.identity),
        )
        return None
    if previous is None and any(x in title for x in ("进展", "实施情况", "结果")):
        return "progress_unlinked"
    measurements, ratio, implemented = _positive_measurements(verified, kind)
    thresholds = (
        policy.contract_thresholds if kind is CompanyPositiveType.OPERATING_CONTRACT else policy.share_thresholds
    )
    reason = None if implemented else "implementation_or_measurement_unverified"
    level = sum(ratio >= threshold for threshold in thresholds) if implemented and ratio is not None else 0
    milestones = list(previous.level_milestones if previous else ())
    if ratio is not None and level:
        for threshold_level in range(1, level + 1):
            if not any(x.level == threshold_level for x in milestones):
                milestones.append(CompanyLevelMilestone(threshold_level, item.published_at, ratio, item.identity))
    event = CompanyPositiveEvent(
        previous.event_id if previous else f"{item.identity}:{kind.value}",
        kind,
        verified.subject,
        CompanyPositiveStatus.COMPLETED
        if implemented and "完成" in title
        else CompanyPositiveStatus.IMPLEMENTING
        if implemented
        else CompanyPositiveStatus.PLANNED,
        previous.first_disclosed_at if previous else item.published_at,
        max(item.published_at, previous.last_disclosed_at) if previous else item.published_at,
        None,
        level,
        item.title[:240],
        _evidence(previous.evidence_ids if previous else (), item.identity),
        measurements,
        tuple(milestones),
        reason,
    )
    if previous:
        positives[positives.index(previous)] = event
    else:
        positives.append(event)
    return reason


def _positive_measurements(
    verified: _VerifiedDocument, kind: CompanyPositiveType
) -> tuple[CompanyMeasurements, float | None, bool]:
    body = _subject_body(verified)
    measurements: CompanyMeasurements
    if kind is CompanyPositiveType.OPERATING_CONTRACT:
        measurements = _contract(body, verified.document.disclosure.published_at)
        ratio = measurements.amount_to_revenue_pct
        implemented = measurements.binding_confirmed and measurements.amount_basis == "comparable"
    else:
        verb = "回购" if kind is CompanyPositiveType.SHARE_REPURCHASE else "增持"
        ratio = _actual_ratio(body, verb)
        implemented = ratio is not None
        if kind is CompanyPositiveType.SHARE_REPURCHASE:
            purpose = (
                RepurchasePurpose.CANCELLATION
                if ("用于注销" in body or "注销并减少注册资本" in body)
                and not any(x in body for x in ("员工持股", "股权激励"))
                else RepurchasePurpose.EMPLOYEE_PLAN
                if "员工持股" in body
                else RepurchasePurpose.INCENTIVE
                if "股权激励" in body
                else RepurchasePurpose.UNKNOWN
            )
            measurements = RepurchaseMeasurements(ratio, purpose)
            implemented = implemented and purpose is RepurchasePurpose.CANCELLATION
        else:
            measurements = IncreaseMeasurements(ratio)
            implemented = implemented and verified.subject is not CompanySubject.ISSUER
    return measurements, ratio, implemented


def _actual_ratio(body: str, verb: str) -> float | None:
    pattern = rf"(?:累计|已)(?:实际|通过[^。；]{{0,30}})?{verb}[^。；]{{0,140}}?占(?:本公司|公司)?(?:目前|当前|现有)?总股本(?:的比例)?(?:为|约为|为约)?[：:]?([0-9]+(?:\.[0-9]+)?)[%％]"
    # The measurement must describe the same actual action, without crossing a plan or negation.
    values = {
        float(match[1])
        for match in re.finditer(pattern, body)
        if not any(marker in match[0] for marker in ("拟", "计划", "预计", "将", "尚未", "未实施"))
        and not re.search(r"(?:未|不|尚未)$", body[max(0, match.start() - 3) : match.start()])
    }
    return next(iter(values)) if len(values) == 1 and all(0 <= x <= 100 for x in values) else None


def _contract(body: str, published: datetime) -> ContractMeasurements:
    binding = any(
        x in body for x in ("合同已生效", "合同自签署之日起生效", "已签订正式合同", "已签署正式合同")
    ) and not any(x in body for x in ("框架协议", "意向协议", "中标候选", "尚未生效"))
    binding = binding and re.search(r"(?:尚需|须经|需经)[^。；]{0,40}生效", body) is None
    amount = _single_measurement(
        r"合同(?:总)?金额(?:[（(](?:不含税|含税)[）)])?(?:为|约为)?人民币([0-9]+(?:\.[0-9]+)?)(亿|万)?元", body
    )
    revenue = _single_measurement(
        r"(20[0-9]{2})年度(?:经审计的)?营业收入(?:为|合计)?人民币([0-9]+(?:\.[0-9]+)?)(亿|万)?元", body
    )
    disclosed = _annual_report_disclosure(body, int(revenue[1]), published) if revenue else None
    comparable = (
        amount is not None
        and revenue is not None
        and _net_measurement(body, amount)
        and _net_measurement(body, revenue)
        and disclosed is not None
    )
    contract_amount = float(amount[1]) * _amount_unit(amount[2]) if amount else None
    annual_revenue = float(revenue[2]) * _amount_unit(revenue[3]) if revenue else None
    ratio = (
        100 * contract_amount / annual_revenue
        if comparable and contract_amount is not None and annual_revenue and annual_revenue > 0
        else None
    )
    return ContractMeasurements(
        contract_amount,
        annual_revenue,
        date(int(revenue[1]), 12, 31) if revenue else None,
        disclosed,
        ratio,
        binding,
        "comparable" if comparable else "unknown",
    )


def _single_measurement(pattern: str, body: str) -> re.Match[str] | None:
    matches = tuple(re.finditer(pattern, body))
    return matches[0] if len(matches) == 1 else None


def _net_measurement(body: str, match: re.Match[str]) -> bool:
    start = max(body.rfind(separator, 0, match.start()) for separator in ("；", "。", "，", ",")) + 1
    ends = tuple(body.find(separator, match.end()) for separator in ("；", "。", "，", ","))
    end = min((position for position in ends if position >= 0), default=len(body))
    clause = body[start:end]
    return "不含税" in clause and "含税" not in clause.replace("不含税", "")


def _annual_report_disclosure(body: str, year: int, published: datetime) -> datetime | None:
    match = _single_measurement(
        rf"(?:{year}年?年度报告|该年度报告|上述年度报告)于(20\d{{2}})[年/-](\d{{1,2}})[月/-](\d{{1,2}})日?(?:披露|公告)",
        body,
    )
    if match is None:
        return None
    try:
        disclosed = datetime(int(match[1]), int(match[2]), int(match[3]), tzinfo=published.tzinfo)
    except ValueError:
        return None
    return disclosed if date(year, 12, 31) < disclosed.date() and disclosed.date() < published.date() else None


def _amount_unit(unit: str | None) -> int:
    return 100_000_000 if unit == "亿" else 10_000 if unit == "万" else 1


def _subject(title: str, body: str) -> CompanySubject | None:
    subjects = {subject for marker, subject in _SUBJECT_MARKERS if marker in title and marker in body}
    if subjects:
        return next(iter(subjects)) if len(subjects) == 1 else None
    if any(marker in body for marker, _ in _SUBJECT_MARKERS):
        return None
    return CompanySubject.ISSUER if any(x in body for x in ("本公司", "公司收到", "公司已", "公司于")) else None


def _subject_body(verified: _VerifiedDocument) -> str:
    current: CompanySubject | None = None
    selected: list[str] = []
    parts = re.split(r"([。；;！？!?，,\n])", verified.body)
    for offset in range(0, len(parts), 2):
        clause = parts[offset]
        text = re.sub(r"(?:本公司|上市公司|公司)的?(?=控股股东|实际控制人|持股5%以上|5%以上股东|大股东)", "", clause)
        subjects = {subject for marker, subject in _SUBJECT_MARKERS if marker in text}
        if any(marker in text for marker in ("本公司", "上市公司", "公司收到", "公司已", "公司于", "公司未", "公司不")):
            subjects.add(CompanySubject.ISSUER)
        if subjects:
            current = next(iter(subjects)) if len(subjects) == 1 else None
        if current is verified.subject and clause:
            selected.append(clause)
            if offset + 1 < len(parts):
                selected.append(parts[offset + 1])
    return "".join(selected)


def _unrelated_subject(title: str) -> bool:
    return any(x in title for x in ("董事", "监事", "高级管理", "子公司", "参股")) and "公司及" not in title


def _risk_authority(body: str, category: CompanyRiskType) -> bool:
    if category is CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION:
        return "减持计划" in body or "拟减持" in body
    return any(x in body for x in ("中国证券监督管理", "证监会", "证券交易所", "人民法院", "监管局"))


def _negated(body: str, category: CompanyRiskType) -> bool:
    terms = {
        CompanyRiskType.FINANCIAL_FRAUD: ("不存在财务造假", "不存在虚假记载"),
        CompanyRiskType.OFFICIAL_INVESTIGATION: (
            "未被立案",
            "未收到立案",
            "不涉及立案",
            "未立案",
            "不予立案",
            "不存在立案",
        ),
        CompanyRiskType.FUND_OCCUPATION: ("不存在资金占用",),
        CompanyRiskType.ILLEGAL_GUARANTEE: ("不存在违规担保", "不存在违法担保"),
        CompanyRiskType.MAJOR_ILLEGAL: ("不存在重大违法",),
    }
    return any(x in body for x in terms.get(category, ()))


def _references(body: str, identities: tuple[str, ...]) -> bool:
    return any(
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(identity)}(?![A-Za-z0-9_])", body) is not None
        or f"公告{identity}" in body
        or f"公告编号{identity}" in body
        for identity in identities
    )


def _evidence(existing: tuple[str, ...], identity: str) -> tuple[str, ...]:
    return existing if identity in existing else (*existing, identity)


def _suspend_positive(
    events: list[CompanyPositiveEvent], identity: str, at: datetime, *, refresh_time: bool = True
) -> None:
    for index, event in enumerate(events):
        if identity in event.evidence_ids:
            events[index] = replace(
                event,
                positive_level=0,
                not_eligible_reason="revision_pending",
                last_disclosed_at=max(at, event.last_disclosed_at) if refresh_time else event.last_disclosed_at,
            )


def _review(document: CompanyDocument, now: datetime, scope: str, reason: str, summary: str) -> CompanyReviewReason:
    return CompanyReviewReason(scope, reason, document.disclosure.identity, now, summary)
