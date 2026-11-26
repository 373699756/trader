"""Project one fixed company population from source observations and sealed production reads."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from trader.download.domain.company_evidence import CompanyFinancial
from trader.download.domain.company_events import CompanyMeasurements, IncreaseMeasurements, RepurchaseMeasurements
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_field_results import CompanyFieldConclusion
from trader.download.domain.company_source_records import (
    CompanyFinancialField, CompanyFinancialScoringObservation, CompanyFinancialSourceObservation, CompanyFinancialSourceValues,
)
from trader.download.domain.company_sync import CompanySyncRoundScope
from trader.download.infra.company_facts_index import CompanyPublicationIdentity
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.download.infra.company_financial_observations import ReadRoundLatestCompanyFinancialObservations
from trader.infra.sqlite_connection import SqliteConnection
from trader.recommendation.domain.market.research import FinancialReportIsFresh, ProjectCompanyFinancialReport
from trader.recommendation.domain.scoring.company import CompanyScoringPolicy


_FINANCIAL_USES: dict[CompanyFinancialField, str] = {
    "basic_eps": "d25_valuation", "book_value_per_share": "d25_valuation",
    "revenue_growth_pct": "formal_deterioration_gate_and_d25_growth", "net_profit_growth_pct": "formal_deterioration_gate_and_d25_growth",
    "core_profit_growth_pct": "formal_deterioration_gate_and_d25_growth", "roe_pct": "d25_quality",
    "parent_net_profit": "formal_loss_gate_and_d25_quality", "core_net_profit": "formal_loss_gate_and_d25_quality",
}


@dataclass(frozen=True, slots=True)
class StockFieldReport:
    output: Path
    observed_at: datetime
    scoring: CompanyScoringPolicy


def _time(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _financial(report: CompanyFinancial | None) -> dict[str, object] | None:
    if report is None:
        return None
    return {"report_date": report.report_date.isoformat(), "disclosed_at": report.published_at.isoformat(),
            "fields": dict(CompanyFinancialSourceValues(report))}


def _financial_comparison(
    source: CompanyFinancial | None, formal: CompanyFinancial | None,
) -> dict[str, list[str]]:
    """Describe differences on comparable periods; numeric zero is a present value."""
    source_values = dict(CompanyFinancialSourceValues(source)) if source else {}
    formal_values = dict(CompanyFinancialSourceValues(formal)) if formal else {}
    differences: dict[str, list[str]] = defaultdict(list)
    for field in _FINANCIAL_USES:
        if source is None:
            differences["source_report_missing"].append(field)
        elif source_values.get(field) is None:
            differences["source_null"].append(field)
        if formal is None:
            differences["formal_report_missing"].append(field)
        elif formal_values.get(field) is None:
            differences["formal_null"].append(field)
        if source is not None and formal is not None and formal.report_date != source.report_date:
            relation = "formal_older_report" if formal.report_date < source.report_date else "formal_newer_report"
            differences[relation].append(field)
        elif source_values.get(field) is not None and formal_values.get(field) is not None:
            matched = source_values.get(field) == formal_values.get(field)
            differences["same_period_equal" if matched else "same_period_conflict"].append(field)
    return dict(differences)


def _observation(item: CompanyFinancialSourceObservation | None) -> dict[str, object] | None:
    if item is None:
        return None
    return {"report": _financial(item.financial), "supplier_notice_at": item.source_notice_at.isoformat(),
            "supplier_updated_at": _time(item.source_updated_at), "observed_at": item.observed_at.isoformat(),
            "record_hash": item.record_hash, "currency": item.currency,
            "source_null_fields": [missing.field for missing in item.missing_fields]}


def _field(item: CompanyFieldConclusion) -> dict[str, object]:
    return {"event": item.event_id, "field": item.field,
            "value": item.value.isoformat() if isinstance(item.value, date) else item.value,
            "state": item.state, "applicable": item.applicable, "reason": item.reason,
            "evidence": [{"source": proof.source, "source_key": proof.source_key, "record_hash": proof.record_hash,
                          "body_hash": proof.body_hash.hex(), "rule": proof.rule_id,
                          "disclosed_at": proof.published_at.isoformat(), "lines": [proof.start_line, proof.end_line],
                          "unit": proof.unit, "subject": proof.subject.value} for proof in item.evidence]}


def _measurements(item: CompanyMeasurements) -> dict[str, object]:
    if isinstance(item, RepurchaseMeasurements):
        return {"actual_ratio_pct": item.actual_ratio_pct, "purpose": item.purpose.value}
    if isinstance(item, IncreaseMeasurements):
        return {"actual_ratio_pct": item.actual_ratio_pct}
    return {"contract_amount_cny": item.contract_amount_cny, "reference_annual_revenue_cny": item.reference_annual_revenue_cny,
            "reference_report_date": _time(item.reference_report_date), "reference_disclosed_at": _time(item.reference_disclosed_at),
            "amount_to_revenue_pct": item.amount_to_revenue_pct, "binding_confirmed": item.binding_confirmed,
            "amount_basis": item.amount_basis}


def _facts(fact: CompanyFacts | None) -> dict[str, object] | None:
    if fact is None:
        return None
    return {
        "published_at": fact.published_at.isoformat(), "checked_at": fact.checked_at.isoformat(),
        "financial_state": fact.financial_state.value,
        "financial": _financial(fact.financial), "latest_financial": _financial(fact.latest_financial),
        "pledge_state": fact.pledge_state.value,
        "pledge": {"date": fact.pledge.day.isoformat(), "ratio_pct": fact.pledge.ratio_pct} if fact.pledge else None,
        "unlock_state": fact.unlock_state.value, "unlock_checked_at": _time(fact.unlock_checked_at),
        "unlock_covered_through": _time(fact.unlock_coverage_end),
        "unlocks": [{"date": row.day.isoformat(), "ratio_pct": row.ratio_pct} for row in fact.unlock_schedule],
        "announcement_state": fact.announcement_state.value, "announcement_from": _time(fact.announcement_coverage_start),
        "announcement_through": _time(fact.announcement_coverage_end), "pending_documents": fact.pending_document_count,
        "risk_events": [{"id": event.event_id, "type": event.risk_type.value, "subject": event.subject_type.value,
                         "status": event.status.value, "level": event.risk_level, "effective_at": event.effective_at.isoformat(),
                         "first_disclosed_at": event.first_disclosed_at.isoformat(), "last_disclosed_at": event.last_disclosed_at.isoformat(),
                         "resolved_at": _time(event.resolved_at), "expires_at": _time(event.expires_at),
                         "severity_basis": event.severity_basis.value, "case_id": event.case_id,
                         "evidence_ids": event.evidence_ids, "reduction_plan": {
                             "planned_start_date": _time(plan.planned_start_date), "planned_end_date": _time(plan.planned_end_date),
                             "planned_ratio_pct": plan.planned_ratio_pct, "completion_disclosed_at": _time(plan.completion_disclosed_at),
                         } if (plan := event.reduction_plan) is not None else None}
                        for event in fact.risk_events],
        "positive_events": [{"id": event.event_id, "type": event.event_type.value, "status": event.status.value,
                             "level": event.positive_level, "subject": event.subject_type.value,
                             "first_disclosed_at": event.first_disclosed_at.isoformat(), "last_disclosed_at": event.last_disclosed_at.isoformat(),
                             "cancelled_at": _time(event.cancelled_at), "measurements": _measurements(event.measurements),
                             "milestones": [{"level": milestone.level, "qualified_at": milestone.qualified_at.isoformat(),
                                             "measurement_value_pct": milestone.measurement_value_pct, "evidence_id": milestone.evidence_id}
                                            for milestone in event.level_milestones],
                             "not_eligible_reason": event.not_eligible_reason,
                             "evidence_ids": event.evidence_ids} for event in fact.positive_events],
        "reviews": [{"scope": item.scope, "reason": item.reason_code, "detected_at": item.detected_at.isoformat(),
                     "evidence_id": item.evidence_id} for item in fact.review_reasons],
    }


def _scoring(item: CompanyFinancialScoringObservation | None) -> dict[str, object] | None:
    if item is None:
        return None
    return {"available_at": item.available_at.isoformat(), "observation": _observation(item.observation)}


def BuildStockFieldReport(
    request: StockFieldReport, sources: SqliteConnection, reader: SQLiteCompanyEvidenceReader, *, scope: CompanySyncRoundScope,
    round_id: str, financial_scope: str, publication: CompanyPublicationIdentity, maximum_age_days: int,
    cancelled: Callable[[], bool], source_pages: dict[str, object], source_windows: dict[str, object],
) -> dict[str, object]:
    """Use the round roster as denominator; reader visibility is measured at two specific times."""
    def check() -> None:
        if cancelled():
            raise TimeoutError("company_stock_fields_deadline")

    if request.observed_at.utcoffset() is None or request.observed_at < scope.cutoff:
        raise ValueError("company_stock_fields_read_time_invalid")
    version = publication.content_hash.hex()
    if reader.publication_version() != version:
        raise ValueError("company_stock_fields_reader_publication_changed")
    facts: list[CompanyFacts] = []
    reader.visit(scope.codes, facts.append, cancel_requested=cancelled)
    by_code = {fact.code: fact for fact in facts}
    visible = {item.observation.financial.code: item for item in reader.read_financial_scoring_observations(
        scope.codes, as_of=request.observed_at, cancel_requested=cancelled)}
    at_cutoff = {item.observation.financial.code: item for item in reader.read_financial_scoring_observations(
        scope.codes, as_of=scope.cutoff, cancel_requested=cancelled)}
    fields: dict[str, list[dict[str, object]]] = defaultdict(list)
    for offset in range(0, len(scope.codes), 512):
        snapshot = reader.read_fields(scope.codes[offset:offset + 512], cancel_requested=cancelled)
        if snapshot.publication_hash != version:
            raise ValueError("company_stock_fields_reader_publication_changed")
        for field in snapshot.results:
            fields[field.code].append(_field(field))
    if reader.publication_version() != version:
        raise ValueError("company_stock_fields_reader_publication_changed")
    supplier = {item.financial.code: item for item in ReadRoundLatestCompanyFinancialObservations(sources, financial_scope)}
    ratios: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for code, family, day, ratio in sources.run(
        "SELECT code,component,day,ratio FROM company_ratio_observation WHERE scope=? ORDER BY code,component,day",
        (scope.identity,),
    ):
        ratios[code, family].append({"date": day, "ratio_pct": ratio})
    annuals: dict[str, list[dict[str, object]]] = defaultdict(list)
    for code, period, disclosed, updated, observed, currency, revenue, digest in sources.run(
        "SELECT code,report_date,disclosed_at,updated_at,observed_at,currency,revenue,record_hash "
        "FROM company_annual_income_observation WHERE scope=? ORDER BY code,report_date,disclosed_at,updated_at", (scope.identity,),
    ):
        annuals[code].append({"report_date": period, "disclosed_at": disclosed, "updated_at": updated,
                              "observed_at": observed, "currency": currency, "revenue": revenue, "record_hash": digest})
    notices = {row[0]: {"rows": row[1], "latest_disclosed_at": row[2], "checked_bodies": row[3]} for row in sources.run(
        "SELECT c.code,COUNT(*),MAX(d.published_at),SUM(d.body_checked) FROM disclosures d "
        "JOIN disclosure_codes c ON c.disclosure_id=d.id WHERE d.kind='announcement' AND d.published_at BETWEEN ? AND ? "
        "GROUP BY c.code", ((scope.cutoff.date() - timedelta(days=365)).isoformat(), scope.cutoff.isoformat()))}
    periods: Counter[str] = Counter()
    results = []
    for code in scope.codes:
        check()
        source = supplier.get(code)
        observed = visible.get(code)
        cutoff_observation = at_cutoff.get(code)
        formal = by_code.get(code)
        latest = formal.latest_financial if formal is not None else None
        reader_financial = observed.observation.financial if observed is not None else None
        if source is not None:
            periods[source.financial.report_date.isoformat()] += 1
        results.append({
            "code": code, "latest_in_supplier_batch": _observation(source), "issuer_latest_independently_verified": None,
            "financial_field_comparison": _financial_comparison(source.financial if source else None, latest),
            "source_pledges": ratios.get((code, "pledge"), []), "source_unlocks": ratios.get((code, "unlock"), []),
            "source_annual_incomes": annuals.get(code, []), "retained_window_notices": notices.get(code),
            "formal": _facts(formal), "formal_event_fields": fields.get(code, []),
            "d25_reader_at_source_cutoff": _scoring(cutoff_observation), "d25_reader_at_audit": _scoring(observed),
            "reader_matches_source_record": source.record_hash == observed.observation.record_hash if source and observed else None,
            "formal_latest_fresh_at_audit": FinancialReportIsFresh(ProjectCompanyFinancialReport(latest, request.observed_at),
                request.observed_at, maximum_age_days=maximum_age_days) if latest else None,
            "d25_reader_report_fresh_at_audit": FinancialReportIsFresh(ProjectCompanyFinancialReport(reader_financial, request.observed_at),
                request.observed_at, maximum_age_days=maximum_age_days) if reader_financial else None,
        })
    return {
        "round_id": round_id, "source_cutoff": scope.cutoff.isoformat(), "reader_checked_at": request.observed_at.isoformat(),
        "publication_hash": version, "extraction_hash": publication.extraction_hash.hex(),
        "publication_time": publication.published_at.isoformat(), "source_pages": source_pages, "source_windows": source_windows,
        "population": len(scope.codes), "stocks_with_formal_row": len(by_code), "source_report_periods": dict(periods),
        "reader_at_source_cutoff": len(at_cutoff), "reader_at_audit": len(visible),
        "formal_read_mode": "current_sealed_publication", "issuer_latest_check": "not_independently_performed",
        "decision_or_seat_effect": "unmeasured", "service_adoption": "unmeasured",
        "field_uses": {**_FINANCIAL_USES,
                       "pledge,unlock_schedule,risk_events": "risk_subject_to_coverage_and_time",
                       "announcement_coverage": "action_restriction",
                       "annual_revenue": "basis_for_qualifying_operating_contract_measurements",
                       "positive_events": "extra_company_reward_enabled" if request.scoring.enabled else "extra_company_reward_disabled",
                       "event_field_evidence": "publication_and_audit"},
        "stocks": results,
    }
