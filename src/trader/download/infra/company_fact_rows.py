"""Relational codec for a pinned company publication; all SQL is caller-owned."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence

from trader.download.domain.company_descriptions import (
    CompanyPositiveDescription,
    CompanyReviewDescription,
    CompanyRiskDescription,
)
from trader.download.domain.company_events import (
    CompanyLevelMilestone,
    CompanyPositiveEvent,
    CompanyPositiveType,
    CompanyReviewReason,
    CompanyRiskEvent,
    CompanySeverityBasis,
    ContractMeasurements,
    IncreaseMeasurements,
    ReductionPlan,
    RepurchaseMeasurements,
)
from trader.download.domain.company_evidence import CompanyFinancial, CompanyRatio
from trader.download.domain.company_facts import CompanyFacts
from trader.download.infra.company_facts_protocol import (
    DATA_STATES,
    EVIDENCE_SOURCES,
    POSITIVE_STATUSES,
    POSITIVE_TYPES,
    REPURCHASE_PURPOSES,
    REVIEW_REASONS,
    REVIEW_SCOPES,
    RISK_STATUSES,
    RISK_TYPES,
    SUBJECTS,
    CompanyIdentifier,
    CompanyInteger,
    CompanyNumber,
    DecodeCompanyDate,
    DecodeCompanyEnum,
    DecodeCompanyLabel,
    DecodeCompanyTime,
    EncodeCompanyDate,
    EncodeCompanyReason,
    EncodeCompanyTime,
    RequireCompanyTime,
)
from trader.infra.sqlite_connection import SqliteConnection

COMPANY_FACT_COLUMNS = (
    "code",
    "report_date",
    "disclosed_at",
    "finance_state",
    "eps",
    "bps",
    "revenue_growth_pct",
    "profit_growth_pct",
    "core_profit_growth_pct",
    "roe_pct",
    "parent_profit",
    "core_profit",
    "pledge_pct",
    "pledge_date",
    "pledge_state",
    "unlock_checked_at",
    "unlock_end_date",
    "unlock_state",
    "pending_docs",
    "announcement_start_date",
    "announcement_end_date",
    "announcement_state",
    "checked_at",
    "published_at",
)


def WriteCompanyFactRows(connection: SqliteConnection, facts: CompanyFacts, sources: Mapping[str, str]) -> None:
    """Replace one issuer inside the caller's publication transaction."""
    f = facts.financial
    values = (
        facts.code,
        EncodeCompanyDate(f.report_date) if f else None,
        EncodeCompanyTime(f.published_at) if f else None,
        DATA_STATES[facts.financial_state],
        *(
            (
                CompanyNumber(value)
                for value in (
                    f.basic_eps,
                    f.book_value_per_share,
                    f.revenue_growth_pct,
                    f.net_profit_growth_pct,
                    f.core_profit_growth_pct,
                    f.roe_pct,
                    f.parent_net_profit,
                    f.core_net_profit,
                )
            )
            if f
            else (None,) * 8
        ),
        CompanyNumber(facts.pledge.ratio_pct) if facts.pledge else None,
        EncodeCompanyDate(facts.pledge.day) if facts.pledge else None,
        DATA_STATES[facts.pledge_state],
        EncodeCompanyTime(facts.unlock_checked_at),
        EncodeCompanyDate(facts.unlock_coverage_end),
        DATA_STATES[facts.unlock_state],
        facts.pending_document_count,
        EncodeCompanyDate(facts.announcement_coverage_start),
        EncodeCompanyDate(facts.announcement_coverage_end),
        DATA_STATES[facts.announcement_state],
        EncodeCompanyTime(facts.checked_at),
        EncodeCompanyTime(facts.published_at),
    )
    connection.run("DELETE FROM company_facts WHERE code=?", (facts.code,))
    connection.run(f"INSERT INTO company_facts VALUES({','.join('?' for _ in values)})", values)
    evidence: dict[str, int] = {}

    def reference(key: str) -> int:
        if key not in evidence:
            source = EVIDENCE_SOURCES[sources[key]]
            connection.run("INSERT OR IGNORE INTO company_evidence_ref(source,source_key) VALUES(?,?)", (source, key))
            evidence[key] = CompanyInteger(
                connection.run(
                    "SELECT id FROM company_evidence_ref WHERE source=? AND source_key=?", (source, key)
                ).require_one()[0]
            )
        return evidence[key]

    for event in facts.risk_events:
        connection.run(
            "INSERT INTO company_risk_event VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                facts.code,
                CompanyIdentifier(event.event_id),
                RISK_TYPES[event.risk_type],
                SUBJECTS[event.subject_type],
                event.risk_level,
                event.case_id,
                EncodeCompanyTime(event.first_disclosed_at),
                EncodeCompanyTime(event.last_disclosed_at),
                EncodeCompanyTime(event.effective_at),
                RISK_STATUSES[event.status],
                EncodeCompanyTime(event.resolved_at),
                EncodeCompanyTime(event.expires_at),
            ),
        )
        identity = connection.run("SELECT last_insert_rowid()").require_one()[0]
        plan = event.reduction_plan
        if plan is not None:
            connection.run(
                "INSERT INTO company_reduction_plan VALUES(?,?,?,?,?)",
                (
                    identity,
                    EncodeCompanyDate(plan.planned_start_date),
                    EncodeCompanyDate(plan.planned_end_date),
                    CompanyNumber(plan.planned_ratio_pct),
                    EncodeCompanyTime(plan.completion_disclosed_at),
                ),
            )
        connection.run_many(
            "INSERT INTO company_risk_evidence VALUES(?,?,?)",
            ((identity, reference(key), position) for position, key in enumerate(event.evidence_ids)),
        )
    for positive in facts.positive_events:
        measurement = positive.measurements
        ratio, purpose = None, None
        contract: tuple[object, ...] = (None,) * 7
        if isinstance(measurement, RepurchaseMeasurements):
            ratio, purpose = CompanyNumber(measurement.actual_ratio_pct), REPURCHASE_PURPOSES[measurement.purpose]
        elif isinstance(measurement, IncreaseMeasurements):
            ratio = CompanyNumber(measurement.actual_ratio_pct)
        else:
            contract = (
                CompanyNumber(measurement.contract_amount_cny),
                CompanyNumber(measurement.reference_annual_revenue_cny),
                EncodeCompanyDate(measurement.reference_report_date),
                EncodeCompanyTime(measurement.reference_disclosed_at),
                CompanyNumber(measurement.amount_to_revenue_pct),
                int(measurement.binding_confirmed),
                {"unknown": 0, "comparable": 1}[measurement.amount_basis],
            )
        values = (
            facts.code,
            CompanyIdentifier(positive.event_id),
            POSITIVE_TYPES[positive.event_type],
            SUBJECTS[positive.subject_type],
            POSITIVE_STATUSES[positive.status],
            EncodeCompanyTime(positive.first_disclosed_at),
            EncodeCompanyTime(positive.last_disclosed_at),
            EncodeCompanyTime(positive.cancelled_at),
            positive.positive_level,
            EncodeCompanyReason(positive.not_eligible_reason) if positive.not_eligible_reason else None,
            ratio,
            purpose,
            *contract,
        )
        connection.run(f"INSERT INTO company_positive_event VALUES(NULL,{','.join('?' for _ in values)})", values)
        identity = connection.run("SELECT last_insert_rowid()").require_one()[0]
        connection.run_many(
            "INSERT INTO company_positive_evidence VALUES(?,?,?)",
            ((identity, reference(key), position) for position, key in enumerate(positive.evidence_ids)),
        )
        connection.run_many(
            "INSERT INTO company_positive_milestone VALUES(?,?,?,?,?)",
            (
                (
                    identity,
                    item.level,
                    EncodeCompanyTime(item.qualified_at),
                    CompanyNumber(item.measurement_value_pct),
                    reference(item.evidence_id),
                )
                for item in positive.level_milestones
            ),
        )
    connection.run_many(
        "INSERT INTO company_unlock VALUES(?,?,?)",
        ((facts.code, EncodeCompanyDate(item.day), CompanyNumber(item.ratio_pct)) for item in facts.unlock_schedule),
    )
    for review in facts.review_reasons:
        connection.run(
            "INSERT OR IGNORE INTO company_review VALUES(NULL,?,?,?,?,?)",
            (
                facts.code,
                REVIEW_SCOPES[review.scope],
                EncodeCompanyReason(review.reason_code),
                reference(review.evidence_id) if review.evidence_id else None,
                EncodeCompanyTime(review.detected_at),
            ),
        )


def ReadCompanyFactRows(connection: SqliteConnection, codes: Sequence[str]) -> tuple[CompanyFacts, ...]:
    """Decode each batch with a fixed number of queries, including all child rows."""
    if not codes:
        return ()
    slots = ",".join("?" for _ in codes)
    main = connection.run(f"SELECT * FROM company_facts WHERE code IN ({slots}) ORDER BY code", codes).fetchall()
    risks: dict[str, list[CompanyRiskEvent]] = defaultdict(list)
    positives: dict[str, list[CompanyPositiveEvent]] = defaultdict(list)
    reviews: dict[str, list[CompanyReviewReason]] = defaultdict(list)
    unlocks: dict[str, list[CompanyRatio]] = defaultdict(list)
    links: dict[tuple[str, int], list[str]] = defaultdict(list)
    for kind in ("risk", "positive"):
        for row in connection.run(
            f"SELECT e.id,r.source_key FROM company_{kind}_event e JOIN company_{kind}_evidence l "
            f"ON l.{kind}_id=e.id JOIN company_evidence_ref r ON r.id=l.evidence_id "
            f"WHERE e.code IN ({slots}) ORDER BY e.id,l.position",
            codes,
        ):
            links[kind, row[0]].append(CompanyIdentifier(row[1]))
    milestones: dict[int, list[CompanyLevelMilestone]] = defaultdict(list)
    for row in connection.run(
        "SELECT m.*,r.source_key FROM company_positive_milestone m JOIN company_positive_event e "
        "ON e.id=m.positive_id JOIN company_evidence_ref r ON r.id=m.evidence_id "
        f"WHERE e.code IN ({slots}) ORDER BY e.id,m.level",
        codes,
    ):
        value = CompanyNumber(row["measured_pct"])
        if value is None:
            raise ValueError("company_milestone_value_missing")
        milestones[row["positive_id"]].append(
            CompanyLevelMilestone(
                CompanyInteger(row["level"]),
                RequireCompanyTime(row["qualified_at"]),
                value,
                CompanyIdentifier(row["source_key"]),
            )
        )
    for row in connection.run(
        "SELECT e.*,p.risk_id,p.start_date,p.end_date,p.planned_pct,p.completion_disclosed_at "
        "FROM company_risk_event e LEFT JOIN company_reduction_plan p ON p.risk_id=e.id "
        f"WHERE e.code IN ({slots}) ORDER BY e.risk_type,e.event_key",
        codes,
    ):
        kind = DecodeCompanyEnum(RISK_TYPES, row["risk_type"])
        level = CompanyInteger(row["level"])
        if not 1 <= level <= 4:
            raise ValueError("company_risk_level_invalid")
        risks[row["code"]].append(
            CompanyRiskEvent(
                CompanyIdentifier(row["event_key"]),
                kind,
                level,
                DecodeCompanyEnum(SUBJECTS, row["subject_type"]),
                (
                    CompanySeverityBasis.WARNING,
                    CompanySeverityBasis.ORDINARY,
                    CompanySeverityBasis.SERIOUS,
                    CompanySeverityBasis.HARD,
                )[level - 1],
                CompanyIdentifier(row["case_number"]) if row["case_number"] is not None else None,
                RequireCompanyTime(row["first_disclosed_at"]),
                RequireCompanyTime(row["last_disclosed_at"]),
                RequireCompanyTime(row["effective_at"]),
                DecodeCompanyEnum(RISK_STATUSES, row["status"]),
                DecodeCompanyTime(row["resolved_at"]),
                DecodeCompanyTime(row["expires_at"]),
                CompanyRiskDescription(kind),
                tuple(links["risk", row["id"]]),
                ReductionPlan(
                    DecodeCompanyDate(row["start_date"]),
                    DecodeCompanyDate(row["end_date"]),
                    CompanyNumber(row["planned_pct"]),
                    DecodeCompanyTime(row["completion_disclosed_at"]),
                )
                if row["risk_id"] is not None
                else None,
            )
        )
    for row in connection.run(
        f"SELECT * FROM company_positive_event WHERE code IN ({slots}) ORDER BY event_type,event_key", codes
    ):
        positives[row["code"]].append(_positive(row, tuple(links["positive", row["id"]]), tuple(milestones[row["id"]])))
    for row in connection.run(
        "SELECT v.*,r.source_key FROM company_review v LEFT JOIN company_evidence_ref r ON r.id=v.evidence_id "
        f"WHERE v.code IN ({slots}) ORDER BY v.scope,r.source_key,v.reason",
        codes,
    ):
        reason = DecodeCompanyLabel(REVIEW_REASONS, row["reason"])
        reviews[row["code"]].append(
            CompanyReviewReason(
                DecodeCompanyLabel(REVIEW_SCOPES, row["scope"]),
                reason,
                CompanyIdentifier(row["source_key"]) if row["source_key"] is not None else None,
                RequireCompanyTime(row["detected_at"]),
                CompanyReviewDescription(reason),
            )
        )
    for row in connection.run(f"SELECT * FROM company_unlock WHERE code IN ({slots}) ORDER BY unlock_date", codes):
        day, ratio = DecodeCompanyDate(row["unlock_date"]), CompanyNumber(row["ratio_pct"])
        if day is None or ratio is None:
            raise ValueError("company_unlock_identity_incomplete")
        unlocks[row["code"]].append(CompanyRatio(CompanyIdentifier(row["code"]), day, ratio))
    return tuple(
        _facts(
            row,
            tuple(sorted(risks[row["code"]], key=lambda item: (item.risk_type.value, item.event_id))),
            tuple(sorted(positives[row["code"]], key=lambda item: (item.event_type.value, item.event_id))),
            tuple(
                sorted(reviews[row["code"]], key=lambda item: (item.scope, item.evidence_id or "", item.reason_code))
            ),
            tuple(unlocks[row["code"]]),
        )
        for row in main
    )


def _positive(
    row: sqlite3.Row, evidence: tuple[str, ...], milestones: tuple[CompanyLevelMilestone, ...]
) -> CompanyPositiveEvent:
    kind = DecodeCompanyEnum(POSITIVE_TYPES, row["event_type"])
    measurements: RepurchaseMeasurements | IncreaseMeasurements | ContractMeasurements
    if kind is CompanyPositiveType.SHARE_REPURCHASE:
        measurements = RepurchaseMeasurements(
            CompanyNumber(row["actual_pct"]), DecodeCompanyEnum(REPURCHASE_PURPOSES, row["repurchase_purpose"])
        )
    elif kind is CompanyPositiveType.SHAREHOLDER_INCREASE:
        measurements = IncreaseMeasurements(CompanyNumber(row["actual_pct"]))
    else:
        binding = CompanyInteger(row["binding_confirmed"])
        if binding not in (0, 1):
            raise ValueError("company_contract_boolean_invalid")
        measurements = ContractMeasurements(
            CompanyNumber(row["contract_amount"]),
            CompanyNumber(row["annual_revenue"]),
            DecodeCompanyDate(row["revenue_report_date"]),
            DecodeCompanyTime(row["revenue_disclosed_at"]),
            CompanyNumber(row["contract_revenue_pct"]),
            bool(binding),
            DecodeCompanyLabel({"unknown": 0, "comparable": 1}, row["amount_basis"]),
        )
    return CompanyPositiveEvent(
        CompanyIdentifier(row["event_key"]),
        kind,
        DecodeCompanyEnum(SUBJECTS, row["subject_type"]),
        DecodeCompanyEnum(POSITIVE_STATUSES, row["status"]),
        RequireCompanyTime(row["first_disclosed_at"]),
        RequireCompanyTime(row["last_disclosed_at"]),
        DecodeCompanyTime(row["cancelled_at"]),
        CompanyInteger(row["level"]),
        CompanyPositiveDescription(kind),
        evidence,
        measurements,
        milestones,
        DecodeCompanyLabel(REVIEW_REASONS, row["ineligible_reason"]) if row["ineligible_reason"] is not None else None,
    )


def _facts(
    row: sqlite3.Row,
    risks: tuple[CompanyRiskEvent, ...],
    positives: tuple[CompanyPositiveEvent, ...],
    reviews: tuple[CompanyReviewReason, ...],
    unlocks: tuple[CompanyRatio, ...],
) -> CompanyFacts:
    code = CompanyIdentifier(row["code"])
    report, disclosed = (
        DecodeCompanyDate(row["report_date"]),
        DecodeCompanyTime(row["disclosed_at"]),
    )
    if (report is None) != (disclosed is None):
        raise ValueError("company_financial_identity_incomplete")
    financial = (
        CompanyFinancial(code, report, disclosed, *(CompanyNumber(row[key]) for key in COMPANY_FACT_COLUMNS[4:12]))
        if report is not None and disclosed is not None
        else None
    )
    day, ratio = DecodeCompanyDate(row["pledge_date"]), CompanyNumber(row["pledge_pct"])
    if (day is None) != (ratio is None):
        raise ValueError("company_pledge_identity_incomplete")
    pledge = CompanyRatio(code, day, ratio) if day is not None and ratio is not None else None
    return CompanyFacts(
        code,
        financial,
        DecodeCompanyEnum(DATA_STATES, row["finance_state"]),
        pledge,
        DecodeCompanyEnum(DATA_STATES, row["pledge_state"]),
        unlocks,
        DecodeCompanyTime(row["unlock_checked_at"]),
        DecodeCompanyDate(row["unlock_end_date"]),
        DecodeCompanyEnum(DATA_STATES, row["unlock_state"]),
        risks,
        positives,
        reviews,
        CompanyInteger(row["pending_docs"]),
        DecodeCompanyDate(row["announcement_start_date"]),
        DecodeCompanyDate(row["announcement_end_date"]),
        DecodeCompanyEnum(DATA_STATES, row["announcement_state"]),
        RequireCompanyTime(row["checked_at"]),
        RequireCompanyTime(row["published_at"]),
    )


__all__ = ["COMPANY_FACT_COLUMNS", "WriteCompanyFactRows", "ReadCompanyFactRows"]
