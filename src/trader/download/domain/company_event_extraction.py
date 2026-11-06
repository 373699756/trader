"""Bounded official-document verification; ambiguous conclusions remain review items."""

from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Literal

from trader.download.domain.company_announcement_numbers import ExtractCompanyAnnouncementNumbers
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
from trader.download.domain.company_field_results import CompanyFieldConclusion, CompanyFieldEvidence

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
_SUBSIDIARY_MARKERS = ("子公司", "参股公司")
_REPURCHASE_OPTION = re.compile(
    r"([□☐√✓☑■☒])((?:用于)?减少注册资本|用于注销(?:并减少注册资本)?|"
    r"(?:用于)?员工持股计划(?:或股权激励(?:计划)?)?|(?:用于)?股权激励(?:计划)?|"
    r"用于转换公司(?:可转债|发行的可转换为股票的公司债券)|为维护公司价值及股东权益)"
)
_CONTRACT_ACTOR = re.compile(
    r"(?P<outside>子公司|参股公司)|"
    r"(?P<issuer>(?:本公司|上市公司|(?:^|[，,：:])公司)"
    r"(?=已|于|拟|与|将|根据|就|决定|经|近日|最近|签署|签订|订立|采购|购买|订购|中标|承包|承接|终止|解除|撤销|取消|履行|"
    r"收到[^。；;]{0,80}中标|被确定为[^。；;]{0,80}中标|为[^。；;]{0,120}中标))|"
    r"(?P<action>合同(?:已生效|自签署之日起生效)|签署|签订|订立|采购|购买|订购|中标|承包|承接|终止|解除|撤销|取消|履行合同)"
)


class _ContractActor(Enum):
    ISSUER = "issuer"
    OUTSIDE = "outside"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class CompanyExtractedEvents:
    risks: tuple[CompanyRiskEvent, ...]
    positives: tuple[CompanyPositiveEvent, ...]
    reviews: tuple[CompanyReviewReason, ...]
    checked_documents: int


@dataclass(frozen=True, slots=True)
class CompanyLinkCandidate:
    evidence_id: str
    published_at: datetime
    subject: CompanySubject
    references: tuple[str, ...]
    cases: tuple[str, ...]
    closures: tuple[CompanyRiskType, ...]
    positive: CompanyPositiveEvent | None
    cancellation: bool
    supersedes: tuple[str, ...] = ()


def ExtractCompanyLinkCandidate(document: CompanyDocument, now: datetime, policy: CompanyEventPolicy) -> CompanyLinkCandidate | None:
    verified, _ = _verify_document(document, now)
    if verified is None:
        return None
    body = _subject_body(verified)
    closures = tuple(kind for kind in _risk_closure(verified.title) if kind in _risk_closure(body))
    if any(marker in body for marker in ("尚未", "未解除", "未终止", "未结案", "未完成")):
        closures = ()
    if not _reduction_closure_is_unambiguous(verified):
        closures = tuple(kind for kind in closures if kind is not CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION)
    kind = _positive_type(verified.title)
    supersedes = tuple(sorted(set(re.findall(r"(?:本公告|本次公告)(?:的全部内容)?(?:更正并替代|完整替代|全部替代)(?:原)?公告(?:编号)?[：:（(]?([A-Za-z0-9_-]{4,80})", body))))
    cancellation = any(x in verified.title for x in ("终止", "撤销", "取消"))
    positive = _positive_event(verified, kind, policy, None)[0] if kind is not None else None
    if not closures and positive is None and not supersedes:
        return None
    return CompanyLinkCandidate(
        document.disclosure.identity, document.disclosure.published_at, verified.subject,
        tuple(sorted(set(re.findall(r"[A-Za-z0-9_]+(?:[-/.][A-Za-z0-9_]+)*", body)))),
        tuple(sorted(set(_CASE.findall(body)))), closures, positive, cancellation, supersedes,
    )


def ExtractCompanyFieldEvidence(
    document: CompanyDocument, observed_at: datetime, policy: CompanyEventPolicy
) -> tuple[CompanyFieldConclusion, ...]:
    """Prove supported source fields without changing event qualification.

    Results retain this document's candidate identity. The publication owner
    must bind them to the unique formal event and compare its actual value.
    A source field is proved only when its exact statement has a unique locator
    in the complete original text; unsupported and ambiguous fields stay absent.
    """
    item = document.disclosure
    if observed_at.utcoffset() is None or item.published_at.utcoffset() is None:
        raise ValueError("company_field_evidence_time_invalid")
    if (item.kind != "announcement" or document.terminal_failure or len(item.codes) != 1
        or item.published_at > observed_at or not CompanyDocumentNeedsReview(item.title)):
        return ()
    issuer_codes = ExtractCompanyAnnouncementNumbers(document.text).issuer_codes
    if issuer_codes and item.codes[0] not in issuer_codes:
        return ()
    verified, _ = _verify_document(document, observed_at)
    if verified is None or any(marker in verified.title for marker in ("终止", "撤销", "取消")):
        return ()
    kind = _positive_type(verified.title)
    if kind not in (CompanyPositiveType.SHARE_REPURCHASE, CompanyPositiveType.SHAREHOLDER_INCREASE):
        return ()
    if ((kind is CompanyPositiveType.SHARE_REPURCHASE and verified.subject is not CompanySubject.ISSUER)
        or (kind is CompanyPositiveType.SHAREHOLDER_INCREASE and verified.subject is CompanySubject.ISSUER)):
        return ()
    event, _ = _positive_event(verified, kind, policy, None)
    body = _subject_body(verified)
    results: list[CompanyFieldConclusion] = []
    if isinstance(event.measurements, (RepurchaseMeasurements, IncreaseMeasurements)):
        ratio = event.measurements.actual_ratio_pct
        if ratio is not None:
            verb = "回购" if kind is CompanyPositiveType.SHARE_REPURCHASE else "增持"
            for match in _actual_ratio_matches(body, verb):
                evidence = _field_statement_evidence(
                    document,
                    match[0],
                    "implemented_total_capital_ratio:2",
                    "percent",
                    verified.subject,
                )
                if evidence is not None:
                    results.append(CompanyFieldConclusion(
                        item.codes[0], event.event_id, "actual_pct", ratio, "verified", True,
                        "explicit_implemented_total_capital_ratio", (evidence,),
                    ))
                    break
    if isinstance(event.measurements, RepurchaseMeasurements):
        purpose = event.measurements.purpose
        statement = _repurchase_purpose_statement(body, purpose)
        evidence = _field_statement_evidence(
            document,
            statement,
            "explicit_repurchase_purpose:2",
            "text",
            verified.subject,
        ) if statement is not None else None
        if evidence is not None:
            results.append(CompanyFieldConclusion(
                item.codes[0], event.event_id, "repurchase_purpose", purpose.value, "verified", True,
                "explicit_repurchase_purpose", (evidence,),
            ))
    return tuple(results)


def _field_statement_evidence(
    document: CompanyDocument,
    statement: str,
    rule_id: str,
    unit: Literal["percent", "text"],
    subject: CompanySubject,
) -> CompanyFieldEvidence | None:
    if not statement:
        return None
    compact = "".join(document.text.split())
    start = compact.find(statement)
    if start < 0 or compact.find(statement, start + 1) >= 0:
        return None
    offset = first = last = 0
    for index, character in enumerate(document.text):
        if character.isspace():
            continue
        if offset == start:
            first = index
        offset += 1
        if offset == start + len(statement):
            last = index + 1
            break
    quote = document.text[first:last]
    if len(quote) > 1000:
        return None
    line_offset = 0
    start_line = end_line = 1
    for number, line in enumerate(document.text.splitlines(keepends=True), 1):
        if line_offset <= first < line_offset + len(line):
            start_line = number
        if line_offset <= last - 1 < line_offset + len(line):
            end_line = number
            break
        line_offset += len(line)
    item = document.disclosure
    return CompanyFieldEvidence(
        item.source,
        item.identity,
        item.digest,
        hashlib.sha256(document.text.encode("utf-8")).digest(),
        rule_id,
        item.published_at,
        start_line,
        end_line,
        quote,
        unit,
        subject,
    )


def _repurchase_purpose_statement(body: str, purpose: RepurchasePurpose) -> str | None:
    if purpose is RepurchasePurpose.UNKNOWN:
        return None
    options = tuple(_REPURCHASE_OPTION.finditer(body))
    selected = tuple(match for match in options if match[1] not in {"□", "☐"})
    normalized = body
    for match in reversed(options):
        if match[1] in {"□", "☐"}:
            normalized = normalized[:match.start()] + normalized[match.end():]
    signals = (
        (RepurchasePurpose.CANCELLATION, r"减少注册资本|用于注销"),
        (RepurchasePurpose.EMPLOYEE_PLAN, r"员工持股"),
        (RepurchasePurpose.INCENTIVE, r"股权激励"),
        (RepurchasePurpose.OTHER, r"用于转换公司|维护公司价值"),
    )
    if tuple(value for value, expression in signals if re.search(expression, normalized)) != (purpose,):
        return None
    if re.search(
        r"(?:不是|并不|不再|不会|不得|不能|并非|无需|无须|尚未|未|不|非)(?:用于)?"
        r"(?:注销|减少注册资本|员工持股|股权激励|转换公司|为?维护公司价值)", normalized,
    ):
        return None
    if selected:
        statement = selected[0][0] if len(selected) == 1 and _repurchase_purpose(selected[0][0]) is purpose else None
    else:
        expression = {
            RepurchasePurpose.CANCELLATION: r"用于注销(?:并减少注册资本)?|注销并减少注册资本",
            RepurchasePurpose.EMPLOYEE_PLAN: r"用于员工持股(?:计划)?",
            RepurchasePurpose.INCENTIVE: r"用于股权激励(?:计划)?",
            RepurchasePurpose.OTHER: r"用于转换公司(?:可转债|发行的可转换为股票的公司债券)|为维护公司价值及股东权益",
        }[purpose]
        statements = tuple(re.finditer(expression, body))
        statement = statements[0][0] if len(statements) == 1 else None
    if statement is not None and re.search(
        r"(?:不是|并不|不再|不会|不得|不能|并非|无需|无须|尚未|未曾|不将|未将)[^。；;]{0,40}$",
        body[:body.find(statement)],
    ):
        return None
    return statement


def ResolveCompanyLinks(
    candidates: tuple[CompanyLinkCandidate, ...],
    risks: tuple[CompanyRiskEvent, ...],
    positives: tuple[CompanyPositiveEvent, ...],
    reviews: tuple[CompanyReviewReason, ...],
    policy: CompanyEventPolicy,
) -> CompanyExtractedEvents:
    risk_rows, positive_rows, remaining = list(risks), list(positives), list(reviews)
    for candidate in sorted(candidates, key=lambda item: (item.published_at, item.evidence_id)):
        pending = tuple(r for r in remaining if r.evidence_id == candidate.evidence_id
                        and r.reason_code in {"resolution_unlinked", "progress_unlinked", "cancellation_unlinked"})
        if not pending:
            continue
        matched = False
        satisfied_positive_evidence: set[str] = set()
        risk_matches: list[int] = []
        for index, event in enumerate(risk_rows):
            if (event.risk_type not in candidate.closures or event.subject_type is not candidate.subject
                or candidate.published_at < event.first_disclosed_at or len(candidate.cases) > 1
                or event.case_id and candidate.cases and event.case_id not in candidate.cases):
                continue
            if not (set(event.evidence_ids).intersection(candidate.references) or event.case_id and event.case_id in candidate.cases):
                continue
            risk_matches.append(index)
        reduction_matches = sum(
            risk_rows[index].risk_type is CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION for index in risk_matches
        )
        for index in risk_matches:
            event = risk_rows[index]
            if event.risk_type is CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION and reduction_matches != 1:
                continue
            at = min(candidate.published_at, event.resolved_at) if event.resolved_at else candidate.published_at
            tail = timedelta(days=policy.reduction_tail_days) if event.risk_type is CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION else timedelta(0)
            risk_rows[index] = replace(event, status=CompanyEventStatus.RESOLVED, resolved_at=at, expires_at=at + tail,
                last_disclosed_at=max(event.last_disclosed_at, candidate.published_at), evidence_ids=_evidence(event.evidence_ids, candidate.evidence_id),
                reduction_plan=replace(event.reduction_plan, completion_disclosed_at=at) if event.reduction_plan else None)
            matched = True
        if candidate.positive is not None:
            for index, positive_event in enumerate(positive_rows):
                if (positive_event.event_type is not candidate.positive.event_type or positive_event.subject_type is not candidate.subject
                    or candidate.published_at < positive_event.first_disclosed_at or not set(positive_event.evidence_ids).intersection(candidate.references)):
                    continue
                if candidate.cancellation:
                    positive_rows[index] = replace(positive_event, status=CompanyPositiveStatus.CANCELLED, cancelled_at=candidate.published_at,
                        last_disclosed_at=max(positive_event.last_disclosed_at, candidate.published_at), evidence_ids=_evidence(positive_event.evidence_ids, candidate.evidence_id))
                elif positive_event.status is not CompanyPositiveStatus.CANCELLED and candidate.published_at >= positive_event.last_disclosed_at:
                    milestones = (*positive_event.level_milestones, *(m for m in candidate.positive.level_milestones if m.level not in {p.level for p in positive_event.level_milestones}))
                    positive_rows[index] = replace(candidate.positive, event_id=positive_event.event_id, first_disclosed_at=positive_event.first_disclosed_at,
                        evidence_ids=_evidence(positive_event.evidence_ids, candidate.evidence_id), level_milestones=milestones)
                    if candidate.positive.not_eligible_reason is None:
                        satisfied_positive_evidence.update(positive_event.evidence_ids)
                matched = True
                break
        if matched:
            remaining = _retire_measurement_reviews(
                [r for r in remaining if r not in pending], tuple(satisfied_positive_evidence)
            )
            if candidate.positive is not None and not candidate.cancellation and candidate.positive.not_eligible_reason:
                remaining.append(CompanyReviewReason("positive", candidate.positive.not_eligible_reason, candidate.evidence_id,
                                                    candidate.published_at, "实施数据仍待核实"))
    _suspend_reviewed_positives(positive_rows, remaining)
    return CompanyExtractedEvents(tuple(risk_rows), tuple(positive_rows), tuple(remaining), 0)


@dataclass(frozen=True, slots=True)
class _VerifiedDocument:
    document: CompanyDocument
    title: str
    body: str
    subject: CompanySubject


def CompanyDocumentNeedsReview(title: str) -> bool:
    return not CompanyDocumentOutsideSubjectScope(title) and (
        any(marker in title for marker in _RISK_HINTS) or _positive_type(title) is not None
    )


def CompanyDocumentRiskRelevant(title: str) -> bool:
    return not CompanyDocumentOutsideSubjectScope(title) and any(marker in title for marker in _RISK_HINTS)


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
    tables = {
        item.disclosure.identity for item in documents
        if item.checked and item.text.strip() and item.disclosure.source in _OFFICIAL_SOURCES
        and item.disclosure.published_at <= observed_at and IsCompanyRepurchaseShareholderTable(item.disclosure.title)
        and IsCompanyRepurchaseShareholderTable("".join(item.text.split())[:800].partition("本公司")[0].rstrip("。；;，,：:"))
    }
    tables = {
        evidence_id for evidence_id in tables
        if sum(evidence_id in event.evidence_ids for event in previous_positives) <= 1
        and not any(evidence_id in event.evidence_ids and not IsCompanyRepurchasePlaceholder(event)
                    for event in previous_positives)
    }
    positives = [event for event in previous_positives if not (
        IsCompanyRepurchasePlaceholder(event) and event.evidence_ids[0] in tables
    )]
    processed_evidence = {
        item.disclosure.identity for item in documents
        if item.checked and item.text.strip() and item.disclosure.published_at <= observed_at
        and CompanyDocumentNeedsReview(item.disclosure.title) and item.disclosure.source in _OFFICIAL_SOURCES
    }
    reviews = [
        item for item in previous_reviews if item.evidence_id is not None and item.evidence_id not in processed_evidence
        and not (item.scope == "positive" and item.evidence_id in tables)
    ]
    checked = 0
    for document in sorted(documents, key=lambda item: (item.disclosure.published_at, item.disclosure.identity)):
        item = document.disclosure
        if item.published_at > observed_at or not CompanyDocumentNeedsReview(item.title):
            continue
        verified, review = _verify_document(document, observed_at)
        if verified is None and review is None and any(item.identity in event.evidence_ids for event in positives):
            # A changed source can disprove a former actor without proving that
            # its earlier event was cancelled. Keep that event under review.
            review = _review(document, observed_at, "positive", "subject_unverified", "原事件主体与当前正文不一致")
        if review is not None:
            reviews.append(review)
            _suspend_positive(positives, item.identity, item.published_at)
        if verified is None:
            continue
        checked += 1
        if (CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION in _risk_closure(verified.title)
            and _reduction_closure_is_confirmed(verified.body)):
            # Earlier parsers treated expiry notices as new plans. Only the
            # notice's own isolated derivation can be retired on exact replay.
            # Full notice classification never authorizes resolving another
            # plan: resolution below still requires its subject-specific body.
            risks = [event for event in risks if not (
                event.risk_type is CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION
                and event.status is CompanyEventStatus.ONGOING and event.resolved_at is None
                and event.evidence_ids == (item.identity,)
                and event.event_id == f"{item.identity}:{CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION.value}"
                and event.first_disclosed_at == event.last_disclosed_at == item.published_at
                and (sum(item.identity in other.evidence_ids for other in risks)
                     + sum(item.identity in other.evidence_ids for other in positives)) == 1
            )]
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
            else:
                for event in positives:
                    if (item.identity in event.evidence_ids and event.last_disclosed_at == item.published_at
                        and event.not_eligible_reason is None
                        and event.status in {CompanyPositiveStatus.IMPLEMENTING, CompanyPositiveStatus.COMPLETED}):
                        reviews = _retire_measurement_reviews(reviews, event.evidence_ids)
    _suspend_reviewed_positives(positives, reviews)
    return CompanyExtractedEvents(
        tuple(sorted(risks, key=lambda event: (event.risk_type.value, event.event_id))),
        tuple(sorted(positives, key=lambda event: (event.event_type.value, event.event_id))),
        tuple(sorted(set(reviews), key=lambda item: (item.scope, item.evidence_id or "", item.reason_code))),
        checked,
    )


def _retire_measurement_reviews(
    reviews: list[CompanyReviewReason], evidence_ids: tuple[str, ...]
) -> list[CompanyReviewReason]:
    # Detection time is a collection timestamp, not the disclosure's effective
    # time. A late first sync may detect the older gap after the new disclosure.
    return [review for review in reviews if not (
        review.scope == "positive" and review.reason_code == "implementation_or_measurement_unverified"
        and review.evidence_id in evidence_ids
    )]


def _suspend_reviewed_positives(
    positives: list[CompanyPositiveEvent], reviews: list[CompanyReviewReason]
) -> None:
    # A later progress document cannot override a pending/corrected prerequisite.
    for review in reviews:
        if review.scope == "positive" and review.evidence_id is not None:
            _suspend_positive(positives, review.evidence_id, review.detected_at, refresh_time=False)


def _verify_document(
    document: CompanyDocument, observed_at: datetime
) -> tuple[_VerifiedDocument | None, CompanyReviewReason | None]:
    title = "".join(document.disclosure.title.split())
    body = "".join(document.text.split())
    scope = "risk" if CompanyDocumentRiskRelevant(title) else "positive"
    if not document.checked or not body:
        return None, _review(document, observed_at, scope, "extraction_unsupported" if document.terminal_failure else "document_pending", "公告正文无法提取" if document.terminal_failure else "公告正文尚未完整提取")
    if document.disclosure.source not in _OFFICIAL_SOURCES:
        return None, _review(document, observed_at, scope, "official_source_unverified", "公告来源未核实")
    if CompanyDocumentOutsideSubjectScope(title):
        return None, None
    if _positive_type(title) is CompanyPositiveType.OPERATING_CONTRACT:
        actor = _contract_actor(title, body)
        if actor is _ContractActor.OUTSIDE and not CompanyDocumentRiskRelevant(title):
            return None, None
        if actor is not _ContractActor.ISSUER:
            return None, _review(document, observed_at, scope, "subject_unverified", "合同实际签署主体无法确认")
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
    freeze = any(x in text for x in ("行政处罚决定", "纪律处分决定", "处罚决定书", "判决书"))
    major_holder = any(x in text for x in ("控股股东", "实际控制人", "持股5%以上", "5%以上股东", "大股东"))
    reduction_start = _planned_reduction_intent(text) or any(
        x in text for x in ("减持股份预披露", "减持股份的预披露")
    ) or (
        "减持计划" in text and not any(x in text for x in ("进展", "实施情况", "结果", "终止"))
    )
    if major_holder and reduction_start:
        result.append(CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION)
    if freeze and any(x in text for x in ("财务造假", "虚假记载", "虚增收入", "虚增利润", "重大遗漏")):
        result.append(CompanyRiskType.FINANCIAL_FRAUD)
    if any(x in text for x in ("立案告知书", "收到立案", "决定立案", "被立案")):
        result.append(CompanyRiskType.OFFICIAL_INVESTIGATION)
    if freeze and "重大违法" in text:
        result.append(CompanyRiskType.MAJOR_ILLEGAL)
    if freeze and "资金占用" in text:
        result.append(CompanyRiskType.FUND_OCCUPATION)
    if freeze and any(x in text for x in ("违规担保", "违法担保")):
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
    if previous is not None and item.published_at < previous.last_disclosed_at:
        return
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
        parsed_plan = _reduction_plan(body)
        if parsed_plan is not None and reduction_plan is not None:
            reduction_plan = replace(
                reduction_plan,
                planned_start_date=parsed_plan.planned_start_date or reduction_plan.planned_start_date,
                planned_end_date=parsed_plan.planned_end_date or reduction_plan.planned_end_date,
                planned_ratio_pct=(
                    parsed_plan.planned_ratio_pct
                    if parsed_plan.planned_ratio_pct is not None
                    else reduction_plan.planned_ratio_pct
                ),
            )
        else:
            reduction_plan = parsed_plan or reduction_plan or ReductionPlan()
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
        previous.status if previous else CompanyEventStatus.ONGOING,
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
    if (any(x in text for x in ("减持结果", "终止减持"))
        or re.search(r"减持(?:股份)?(?:计划)?(?:已)?(?:实施)?(?:完成|完毕|期限(?:已)?届满)", text)
        or "减持" in text and "期限已届满" in text):
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


def _reduction_closure_is_unambiguous(verified: _VerifiedDocument) -> bool:
    # Subject enums cannot distinguish several named holders in one notice.
    # Such a notice can be classified, but cannot clear a whole linked plan.
    if any(marker in verified.body for marker in ("分别", "各自", "一致行动人", "间接持有", "间接持股")):
        return False
    if re.search(r"(?:股东|实际控制人)[^。；;，,]{0,60}[、及与和]", verified.body):
        return False
    subjects = {subject for marker, subject in _SUBJECT_MARKERS if marker in verified.body}
    if len(subjects) > 1:
        return False
    return _reduction_closure_is_confirmed(_subject_body(verified))


def _reduction_closure_is_confirmed(body: str) -> bool:
    if re.search(
        r"尚未|未解除|未终止|未结案|未完成|未届满|尚不确定|部分完成|完成部分"
        r"|剩余[^。；;]{0,40}(?:继续|尚未|仍)|(?:仍在?|继续|正在)实施", body,
    ):
        return False
    conclusion = (
        r"(?:减持(?:股份)?(?:计划)?(?:已)?(?:实施)?(?:完成|完毕|期限(?:已)?届满)"
        r"|(?:提前)?终止减持(?:股份|计划)?)"
        r"(?=$|[。；;！？!?，,]|且|并|均未|未减持)"
    )
    for clause in re.split(r"[。；;！？!?，,]", body):
        if re.search(r"尚未|未|不|预计|拟|将|可能", clause):
            continue
        if re.search(conclusion, clause):
            return True
    return False


def _reduction_plan(body: str) -> ReductionPlan | None:
    ratio = _planned_reduction_ratio(body)
    interval = (
        r"(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})日?(?:至|到|~)"
        r"(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})日?"
    )
    patterns = (
        rf"减持(?:计划)?期间(?:为|：|:)?{interval}",
        r"减持(?:计划)?期间(?:为|：|:)?"
        r"(?:自(?:本公告|本次公告|本?减持计划)(?:发布|披露|公告)之日起"
        r"[一二三四五六七八九十百0-9]{1,4}个交易日后(?:的)?[一二三四五六七八九十0-9]{1,3}个月内)?"
        rf"[（(](?:即)?{interval}[）)]",
    )
    periods: set[tuple[date, date]] = set()
    for pattern in patterns:
        for dates in re.finditer(pattern, body):
            try:
                start = date(*(int(dates[i]) for i in (1, 2, 3)))
                end = date(*(int(dates[i]) for i in (4, 5, 6)))
            except ValueError:
                return ReductionPlan(planned_ratio_pct=ratio) if ratio is not None else None
            if start > end:
                return ReductionPlan(planned_ratio_pct=ratio) if ratio is not None else None
            periods.add((start, end))
    if len(periods) == 1:
        start, end = next(iter(periods))
        return ReductionPlan(start, end, ratio)
    return ReductionPlan(planned_ratio_pct=ratio) if ratio is not None else None


def _planned_reduction_ratio(body: str) -> float | None:
    pattern = (
        r"(?:拟|计划)(?:通过[^。；;]{0,30})?减持(?:公司)?(?:股份)?[^。；;]{0,90}?"
        r"占(?:本公司|公司)?(?:目前|当前|现有)?总股本(?:的?比例|的)?"
        r"(?:不超过|不高于|为|约为|为约)?[：:]?([0-9]+(?:\.[0-9]+)?)[%％]"
    )
    matches = tuple(re.finditer(pattern, body))
    if len(matches) != 1 or any(marker in matches[0][0] for marker in ("已减持", "已实施", "持有", "流通股", "所持股份")):
        return None
    match = matches[0]
    # Separate holders need separate identities. Unrelated sections do not make
    # the single plan ambiguous, and a section number is not a holder list.
    separators = "。；;！？!?"
    start = max(body.rfind(separator, 0, match.start()) for separator in separators) + 1
    ends = tuple(body.find(separator, match.end()) for separator in separators)
    end = min((position for position in ends if position >= 0), default=len(body))
    context = re.sub(r"^(?:[一二三四五六七八九十百]+|[0-9]+)、", "", body[start:end])
    if any(marker in context for marker in ("分别", "各自", "一致行动人", "合计", "、")) or re.search(
        r"(?:股东|实际控制人)[^。；;]{0,40}[及和与]", context
    ):
        return None
    prefix = body[max(0, match.start() - 3) : match.start()]
    if re.search(r"(?:未|不|尚未)$", prefix):
        return None
    ratio = float(match[1])
    return ratio if 0 <= ratio <= 100 else None


def _planned_reduction_intent(text: str) -> bool:
    return any(
        re.search(r"(?:未|不|尚未)$", text[max(0, match.start() - 3) : match.start()]) is None
        for match in re.finditer(r"(?:拟|计划)(?:通过[^。；;]{0,30})?减持", text)
    )


def _resolve_risks(risks: list[CompanyRiskEvent], verified: _VerifiedDocument, policy: CompanyEventPolicy) -> bool:
    document, title, body = verified.document, verified.title, _subject_body(verified)
    if any(marker in body for marker in ("尚未", "未解除", "未终止", "未结案", "未完成")):
        return False
    case_ids = set(_CASE.findall(body))
    if len(case_ids) > 1:
        return False
    matched = False
    matches: list[int] = []
    for index, event in enumerate(risks):
        if event.subject_type is not verified.subject:
            continue
        if event.risk_type not in _risk_closure(title) or event.risk_type not in _risk_closure(body):
            continue
        if (event.risk_type is CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION
            and not _reduction_closure_is_unambiguous(verified)):
            continue
        if event.case_id and case_ids and event.case_id not in case_ids:
            continue
        if not (_references(body, event.evidence_ids) or event.case_id and event.case_id in _CASE.findall(body)):
            continue
        at = document.disclosure.published_at
        if at < event.first_disclosed_at:
            continue
        matches.append(index)
    reduction_matches = sum(
        risks[index].risk_type is CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION for index in matches
    )
    for index in matches:
        event = risks[index]
        if event.risk_type is CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION and reduction_matches != 1:
            continue
        at = document.disclosure.published_at
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
    if IsCompanyRepurchaseShareholderTable(title):
        return None
    if "增持" in title:
        return CompanyPositiveType.SHAREHOLDER_INCREASE
    if any(x in title for x in ("回购股份", "回购公司", "股份回购")):
        return CompanyPositiveType.SHARE_REPURCHASE
    if any(x in title for x in ("重大合同", "经营合同", "签订合同", "签署合同")):
        return CompanyPositiveType.OPERATING_CONTRACT
    return None


def IsCompanyRepurchaseShareholderTable(title: str) -> bool:
    """Recognize a standalone holder list, preserving mixed event disclosures."""
    compact = "".join(title.split())
    return (
        any(marker in compact for marker in ("回购股份", "回购公司", "股份回购"))
        and re.search(r"前(?:十|10)(?:名|大)?(?:普通股)?股东", compact) is not None
        and re.search(r"股东(?:持股(?:情况|信息)|情况)?(?:的公告|公告|表)?$", compact) is not None
        and not any(marker in compact for marker in (
            *_RISK_HINTS, "增持", "重大合同", "经营合同", "签订", "签署", "进展", "首次", "完成", "实施",
            "终止", "取消", "撤销", "变更", "调整", "更正", "修订", "回购报告书", "预案", "议案", "方案",
        ))
    )


def IsCompanyRepurchasePlaceholder(event: CompanyPositiveEvent) -> bool:
    """An empty plan shape; material classification is still required to retire it."""
    return (
        event.event_type is CompanyPositiveType.SHARE_REPURCHASE
        and event.status is CompanyPositiveStatus.PLANNED
        and isinstance(event.measurements, RepurchaseMeasurements)
        and event.measurements.actual_ratio_pct is None and event.measurements.purpose is RepurchasePurpose.UNKNOWN
        and event.positive_level == 0 and not event.level_milestones
        and len(event.evidence_ids) == 1 and event.event_id == f"{event.evidence_ids[0]}:share_repurchase"
    )


def _append_positive(
    positives: list[CompanyPositiveEvent],
    verified: _VerifiedDocument,
    kind: CompanyPositiveType,
    policy: CompanyEventPolicy,
) -> str | None:
    item = verified.document.disclosure
    body = _subject_body(verified)
    if any(
        event.event_type is kind and item.identity in event.evidence_ids and event.subject_type is not verified.subject
        for event in positives
    ):
        return "subject_unverified"
    previous = next(
        (
            x
            for x in positives
            if x.event_type is kind
            and x.subject_type is verified.subject
            and (item.identity in x.evidence_ids or _references(body, x.evidence_ids))
        ),
        None,
    )
    title = "".join(item.title.split())
    if previous is not None and item.published_at < previous.last_disclosed_at:
        if item.identity in previous.evidence_ids and not _positive_measurements(verified, kind)[2]:
            # An already superseded prerequisite has re-entered with an
            # unqualified conclusion. Unchanged progress cannot clear a source
            # revision as though it were an older first-pass measurement gap.
            return "revision_pending"
        return None
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
    event, reason = _positive_event(verified, kind, policy, previous)
    if previous:
        positives[positives.index(previous)] = event
    else:
        positives.append(event)
    return reason


def _positive_event(
    verified: _VerifiedDocument, kind: CompanyPositiveType, policy: CompanyEventPolicy, previous: CompanyPositiveEvent | None
) -> tuple[CompanyPositiveEvent, str | None]:
    item, title = verified.document.disclosure, verified.title
    measurements, ratio, eligible = _positive_measurements(verified, kind)
    # An observed action remains implemented even when its purpose or amount
    # basis cannot qualify for points. Missing evidence still receives review.
    implemented = measurements.binding_confirmed if isinstance(measurements, ContractMeasurements) else ratio is not None
    thresholds = (
        policy.contract_thresholds if kind is CompanyPositiveType.OPERATING_CONTRACT else policy.share_thresholds
    )
    reason = None if eligible else "implementation_or_measurement_unverified"
    level = sum(ratio >= threshold for threshold in thresholds) if eligible and ratio is not None else 0
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
    return event, reason


def _positive_measurements(
    verified: _VerifiedDocument, kind: CompanyPositiveType
) -> tuple[CompanyMeasurements, float | None, bool]:
    body = _subject_body(verified)
    measurements: CompanyMeasurements
    if kind is CompanyPositiveType.OPERATING_CONTRACT:
        measurements = _contract(body, verified.document.disclosure.published_at)
        ratio = measurements.amount_to_revenue_pct
        eligible = measurements.binding_confirmed and measurements.amount_basis == "comparable"
    else:
        verb = "回购" if kind is CompanyPositiveType.SHARE_REPURCHASE else "增持"
        ratio = _actual_ratio(body, verb)
        eligible = ratio is not None
        if kind is CompanyPositiveType.SHARE_REPURCHASE:
            purpose = _repurchase_purpose(body)
            measurements = RepurchaseMeasurements(ratio, purpose)
            eligible = eligible and purpose is RepurchasePurpose.CANCELLATION
        else:
            measurements = IncreaseMeasurements(ratio)
            eligible = eligible and verified.subject is not CompanySubject.ISSUER
    return measurements, ratio, eligible


def _repurchase_purpose(body: str) -> RepurchasePurpose:
    # Exchange templates print every option. An empty box does not disclose a
    # purpose, even when its words would otherwise match an affirmative rule.
    options = tuple(_REPURCHASE_OPTION.finditer(body))
    selected_cancellation = any(
        match[1] not in {"□", "☐"} and any(word in match[2] for word in ("减少注册资本", "用于注销"))
        for match in options
    )
    for match in reversed(options):
        body = body[:match.start()] + (match[2] if match[1] not in {"□", "☐"} else "") + body[match.end():]
    cancellation = selected_cancellation or any(word in body for word in ("用于注销", "注销并减少注册资本"))
    denied = re.search(
        r"(?:不再|不会|不得|不能|并非|无需|无须|尚未|未|不|非)(?:用于注销|注销并减少注册资本|减少注册资本)", body
    )
    if cancellation and not denied and not any(word in body for word in ("员工持股", "股权激励", "用于转换", "维护公司价值")):
        return RepurchasePurpose.CANCELLATION
    if "员工持股" in body:
        return RepurchasePurpose.EMPLOYEE_PLAN
    if "股权激励" in body:
        return RepurchasePurpose.INCENTIVE
    if "用于转换公司" in body or "为维护公司价值及股东权益" in body:
        return RepurchasePurpose.OTHER
    return RepurchasePurpose.UNKNOWN


def _actual_ratio(body: str, verb: str) -> float | None:
    values = {float(match[1]) for match in _actual_ratio_matches(body, verb)}
    return next(iter(values)) if len(values) == 1 and all(0 <= value <= 100 for value in values) else None


def _actual_ratio_matches(body: str, verb: str) -> tuple[re.Match[str], ...]:
    action = (r"(?:累计(?:已)?|已|首次(?=回购(?:公司)?(?:股份)?(?:[0-9]|[，,]?占)))"
              if verb == "回购" else r"(?:累计(?:已)?|已)")
    pattern = (
        rf"{action}(?:实际|通过[^。；;]{{0,30}})?{verb}[^。；;]{{0,140}}?"
        r"占(?:本公司|公司)?(?:目前|当前|现有)?总股本(?:的?比例|的)?"
        r"(?:为|约为|为约)?[：:]?([0-9]+(?:\.[0-9]+)?)[%％]"
    )
    # The measurement must describe the same actual action, without crossing a plan or negation.
    return tuple(
        match
        for match in re.finditer(pattern, body)
        if not any(marker in match[0] for marker in ("拟", "计划", "预计", "将", "尚未", "未实施", "持有", "所持股份"))
        and not re.search(r"(?:未|不|尚未)$", body[max(0, match.start() - 3) : match.start()])
        and not (match[0].startswith("首次") and re.search(
            r"(?:拟|计划|预计|将|尚待|尚未|未曾|未|不会|不)[^。；;，,]{0,30}$", body[max(0, match.start() - 40):match.start()]
        ))
    )


def _contract(body: str, published: datetime) -> ContractMeasurements:
    binding = any(
        x in body for x in ("合同已生效", "合同自签署之日起生效", "已签订正式合同", "已签署正式合同")
    ) and not any(x in body for x in ("框架协议", "意向协议", "中标候选", "尚未生效"))
    binding = binding and re.search(r"(?:尚需|须经|需经)[^。；]{0,40}生效", body) is None
    amount = _single_measurement(
        r"(?:合同|协议)(?:总)?金额(?:[（(](?:不含税|含税)[）)])?(?:为|约为)?[：:]?人民币([0-9]+(?:[,，][0-9]{3})*(?:\.[0-9]+)?)(亿|万)?元", body
    )
    revenue = _single_measurement(
        r"(20[0-9]{2})年度(?:经审计的)?营业收入(?:为|合计)?[：:]?人民币([0-9]+(?:[,，][0-9]{3})*(?:\.[0-9]+)?)(亿|万)?元", body
    )
    disclosed = _annual_report_disclosure(body, int(revenue[1]), published) if revenue else None
    comparable = (
        amount is not None
        and revenue is not None
        and _net_measurement(body, amount)
        and _net_measurement(body, revenue)
        and disclosed is not None
    )
    contract_amount = float(amount[1].replace(",", "").replace("，", "")) * _amount_unit(amount[2]) if amount else None
    annual_revenue = float(revenue[2].replace(",", "").replace("，", "")) * _amount_unit(revenue[3]) if revenue else None
    reference_years = set(re.findall(r"(20[0-9]{2})(?:年度|年)(?:经审计的?)?营业收入", body))
    reference_date = (
        date(int(revenue[1]), 12, 31) if revenue else
        date(int(next(iter(reference_years))), 12, 31) if len(reference_years) == 1 else None
    )
    ratio = (
        100 * contract_amount / annual_revenue
        if comparable and contract_amount is not None and annual_revenue and annual_revenue > 0
        else None
    )
    return ContractMeasurements(
        contract_amount,
        annual_revenue,
        reference_date,
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


def _contract_actor(title: str, body: str) -> _ContractActor:
    if any(marker in title for marker in ("公司及", "公司与", "公司和")):
        return _ContractActor.UNVERIFIED
    issuer, outside = False, False
    for sentence in re.split(r"[。；;！？!?]", body):
        actor: _ContractActor | None = None
        issuer_action = False
        has_subsidiary = False
        for token in _CONTRACT_ACTOR.finditer(sentence):
            if token.lastgroup == "action" and token.group() in {"签署", "签订"} and re.match(
                r"方|主体|概况|情况|事宜|流程|程序", sentence[token.end():]
            ):
                continue
            if token.lastgroup == "outside":
                prefix = sentence[max(0, token.start() - 24):token.start()]
                if actor is _ContractActor.ISSUER:
                    return _ContractActor.UNVERIFIED
                if re.search(r"(?:公司|本公司|上市公司)(?:及|与|和)(?:其)?(?:全资|控股)?$", prefix):
                    return _ContractActor.UNVERIFIED
                if re.search(r"(?:不是|并非|不属于|不涉及|不含|不包括|未涉及)[^，,]{0,8}$", prefix):
                    return _ContractActor.UNVERIFIED
                actor = _ContractActor.OUTSIDE
                has_subsidiary = True
            elif token.lastgroup == "issuer":
                actor = _ContractActor.ISSUER
            elif actor is _ContractActor.OUTSIDE:
                outside = True
            elif actor is _ContractActor.ISSUER:
                issuer, issuer_action = True, True
            elif re.search(
                r"公司(?:于[^。；;]{0,30}|已|拟|与[^。；;]{0,30})?$",
                sentence[max(0, token.start() - 64):token.start()],
            ):
                # A legal name or undeclared alias is not proof that every
                # contract belongs to the previously mentioned subsidiary.
                return _ContractActor.UNVERIFIED
        if issuer_action and has_subsidiary:
            # Roles in a shared sentence may be counterparties or co-signers.
            # Its numeric fields cannot safely share one issuer scope.
            return _ContractActor.UNVERIFIED
    if issuer and not outside:
        return _ContractActor.ISSUER
    return _ContractActor.OUTSIDE if outside and not issuer else _ContractActor.UNVERIFIED


def _subject(title: str, body: str) -> CompanySubject | None:
    subjects = {subject for marker, subject in _SUBJECT_MARKERS if marker in title and marker in body}
    if subjects:
        return next(iter(subjects)) if len(subjects) == 1 else None
    if (_positive_type(title) is CompanyPositiveType.SHARE_REPURCHASE
        and any(marker in title for marker in ("回购报告书", "回购股份方案", "回购公司股份方案"))):
        # Plans also disclose holders' trading/intentions. Only an explicit
        # issuer decision establishes this plan's actor; incidental mentions
        # neither establish another actor nor supply its numeric measurements.
        decisions = re.finditer(
            r"(?:本公司|上市公司|(?:^|[。；;，,：:])公司)将(?:在回购期限内|根据董事会决议)"
            r"[^。；;]{0,100}(?:作出回购决策|进行回购)", body,
        )
        if any(not any(marker in match[0] for marker in (*_SUBSIDIARY_MARKERS, *(word for word, _ in _SUBJECT_MARKERS)))
               for match in decisions):
            return CompanySubject.ISSUER
    if any(marker in body for marker, _ in _SUBJECT_MARKERS):
        return None
    return CompanySubject.ISSUER if any(x in body for x in (
        "本公司", "公司收到", "公司已", "公司于", "公司被确定为", "公司签署", "公司签订"
    )) else None


def _subject_body(verified: _VerifiedDocument) -> str:
    current: CompanySubject | None = None
    selected: list[str] = []
    parts = re.split(r"([。；;！？!?，,\n])", verified.body)
    for offset in range(0, len(parts), 2):
        clause = parts[offset]
        text = re.sub(r"(?:本公司|上市公司|公司)的?(?=控股股东|实际控制人|持股5%以上|5%以上股东|大股东)", "", clause)
        if (_positive_type(verified.title) is CompanyPositiveType.OPERATING_CONTRACT
            and any(marker in text for marker in _SUBSIDIARY_MARKERS)):
            current = None
            continue
        subjects = {subject for marker, subject in _SUBJECT_MARKERS if marker in text}
        if any(marker in text for marker in (
            "本公司", "上市公司", "公司收到", "公司已", "公司于", "公司未", "公司不", "公司被确定为", "公司签署", "公司签订"
        )):
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


def CompanyDocumentOutsideSubjectScope(title: str) -> bool:
    """Apply certain existing subject exclusions before document I/O.

    Issuer organs and directors who are also qualifying shareholders still
    require normal verification. Their titles alone cannot exclude an event.
    """
    compact = "".join(title.split())
    return _unrelated_subject(compact) and not any(
        marker in compact for marker in ("董事会", "监事会", *(marker for marker, _subject in _SUBJECT_MARKERS))
    )


def _risk_authority(body: str, category: CompanyRiskType) -> bool:
    if category is CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION:
        return "减持计划" in body or _planned_reduction_intent(body)
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
