"""Review frozen company inputs and locate exact local materials before source admission."""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from scripts.core.projection_values import ProjectMapping, SafeNonnegativeInt, SafeStringList, SafeText
from trader.download.domain.company_evidence import CompanyDocument, StructuredCompanyQuery
from trader.download.domain.company_events import CompanyDataState
from trader.download.domain.company_financial_report import ExtractCompanyFinancialReport, IsCompanyFinancialReportTitle
from trader.download.domain.company_source_records import CompanyFinancialSourceObservation
from trader.download.domain.company_sync import CompanyFinancialRequestIdentity, CompanySyncRoundScope
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_fact_inputs import DecodeCompanyDisclosure
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_facts_protocol import DATA_STATES
from trader.download.infra.company_financial_observations import ReadRoundCompanyFinancialObservations
from trader.download.infra.company_financial_publication import CompanyFinancialReportValue
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.settings.data_paths import DataPaths
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.recommendation.domain.market.research import LongResearchPolicy

_MISSING_LIMIT = 128
_MATERIAL_LIMIT = 8
_GROUPS = ("neither", "deterioration_only", "loss_only", "both")
_REVISION_MARKERS = ("更正", "修订", "重述", "更新")
_ORIGINAL_TITLE = re.compile(r"报告(?:全文|正文)?$")
_FINANCIAL_EVIDENCE = ("issuer_original", "first_disclosure", "issuer_revision_lineage", "market_population_coverage")
_RATIO_EVIDENCE = ("exact_numerator", "same_basis_total_shares", "market_and_window_coverage", "absence_semantics")


@dataclass(frozen=True, slots=True)
class _Pages:
    saved: int
    expected: int | None
    rows: int
    expected_rows: int | None
    closed: bool

    def project(self) -> dict[str, object]:
        return {
            "saved": self.saved,
            "expected": self.expected,
            "rows": self.rows,
            "expected_rows": self.expected_rows,
            "closed": self.closed,
        }


@dataclass(frozen=True, slots=True)
class _Material:
    source_key: str
    record_hash: str
    disclosed_at: datetime
    role: str
    body_status: str
    body_hash: str | None
    compared_fields: tuple[str, ...]
    conflicting_fields: tuple[str, ...]
    source_null_numeric_fields: tuple[str, ...]

    def project(self) -> dict[str, object]:
        return {
            "source_key": self.source_key,
            "record_hash": self.record_hash,
            "disclosed_at": self.disclosed_at.isoformat(),
            "role": self.role,
            "body_status": self.body_status,
            "body_hash": self.body_hash,
            "compared_fields": list(self.compared_fields),
            "conflicting_fields": list(self.conflicting_fields),
            "source_null_numeric_fields": list(self.source_null_numeric_fields),
        }


class _QualificationInterrupted(InterruptedError):
    pass


def _fingerprints(paths: Sequence[Path]) -> tuple[tuple[int, ...] | None, ...]:
    fingerprints: list[tuple[int, ...] | None] = []
    for path in paths:
        for member in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-journal")):
            try:
                stat = member.stat()
            except FileNotFoundError:
                if member == path:
                    raise
                fingerprints.append(None)
                continue
            if member != path and stat.st_size:
                raise ValueError("company_source_qualification_uncheckpointed_database")
            fingerprints.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    return tuple(fingerprints)


def _pages(rows: Sequence[sqlite3.Row]) -> _Pages:
    if not rows:
        return _Pages(0, None, 0, None, False)
    expected, expected_rows = int(rows[0][1]), int(rows[0][2])
    if not 1 <= expected <= 256 or not 0 <= expected_rows <= expected * 500 or len(rows) > expected:
        raise ValueError("company_source_qualification_bad_page_count")
    received = 0
    for page, row in enumerate(rows, 1):
        number, total_pages, total_rows, received_rows = tuple(row)
        if (
            (number, total_pages, total_rows) != (page, expected, expected_rows)
            or received_rows != min(500, max(0, expected_rows - received))
        ):
            raise ValueError("company_source_qualification_page_drift")
        received += received_rows
    return _Pages(len(rows), expected, received, expected_rows, len(rows) == expected and received == expected_rows)


def _group(observation: CompanyFinancialSourceObservation, policy: LongResearchPolicy) -> str | None:
    report = observation.financial
    profits = (report.parent_net_profit, report.core_net_profit)
    growth = (report.revenue_growth_pct, report.net_profit_growth_pct, report.core_profit_growth_pct)
    thresholds = (
        policy.financial_revenue_deterioration_pct,
        policy.financial_profit_deterioration_pct,
        policy.financial_core_profit_deterioration_pct,
    )
    loss = any(value is not None and value < 0 for value in profits)
    deterioration = any(value is not None and value <= limit for value, limit in zip(growth, thresholds, strict=True))
    if (not loss and None in profits) or (not deterioration and None in growth):
        return None
    return _GROUPS[2 * int(loss) + int(deterioration)]


def _materials(
    collected: SqliteConnection,
    documents: CompanyDocumentFiles,
    observation: CompanyFinancialSourceObservation,
    cutoff: datetime,
    check: Callable[[], None],
) -> tuple[tuple[_Material, ...], bool]:
    report = observation.financial
    if report.report_date.month not in (9, 12):
        return (), False
    rows = collected.run(
        """SELECT d.*,c.code
        FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id
        WHERE c.code=? AND d.kind='announcement' AND d.source='cninfo' AND d.published_at>=? AND d.published_at<=?
        AND (SELECT COUNT(*) FROM disclosure_codes members WHERE members.disclosure_id=d.id)=1
        ORDER BY d.published_at,d.source_key""",
        (report.code, (cutoff.date() - timedelta(days=365)).isoformat(), cutoff.isoformat()),
    )
    known: list[_Material] = []
    for row in rows:
        check()
        compact = "".join(row["title"].split())
        if "英文" in compact or not IsCompanyFinancialReportTitle(compact, report.report_date):
            continue
        disclosure = DecodeCompanyDisclosure(row)
        published_at = disclosure.published_at
        revision = any(marker in compact for marker in _REVISION_MARKERS)
        if not revision and (
            _ORIGINAL_TITLE.search(compact) is None
            or abs((published_at.date() - observation.source_notice_at.date()).days) > 1
        ):
            continue
        if len(known) == _MATERIAL_LIMIT:
            return tuple(known), True
        body_status = "directory_only"
        body_hash = row["body_hash"]
        compared: list[str] = []
        conflicting: list[str] = []
        numeric_candidates: list[str] = []
        if row["body_checked"] and row["body_retained"] and isinstance(body_hash, bytes) and len(body_hash) == 32:
            try:
                text = documents.read(body_hash, check=check)
            except _QualificationInterrupted:
                raise
            except (OSError, EOFError, ValueError):
                body_status = "retained_text_unusable"
            else:
                body_status = "exact_retained_text"
                parsed = ExtractCompanyFinancialReport(CompanyDocument(disclosure, text, True))
                if parsed is not None and parsed.code == report.code and parsed.report_date == report.report_date:
                    for cell in parsed.cells:
                        if cell.state != "numeric" or cell.value is None:
                            continue
                        source_value = CompanyFinancialReportValue(report, cell.field)
                        if source_value is None:
                            numeric_candidates.append(cell.field)
                        else:
                            compared.append(cell.field)
                            if not math.isclose(source_value, cell.value, rel_tol=1e-9, abs_tol=1e-9):
                                conflicting.append(cell.field)
        known.append(
            _Material(
                disclosure.identity, disclosure.digest, published_at,
                "revision_candidate" if revision else "original_candidate",
                body_status, body_hash.hex() if isinstance(body_hash, bytes) and len(body_hash) == 32 else None,
                tuple(compared), tuple(conflicting), tuple(numeric_candidates),
            )
        )
    return tuple(known), False


def _review_financial(
    normalized: SqliteConnection,
    collected: SqliteConnection,
    documents: CompanyDocumentFiles,
    scope: CompanySyncRoundScope,
    source_scope: str,
    round_id: str,
    policy: LongResearchPolicy,
    check: Callable[[], None],
) -> dict[str, object]:
    pages = _pages(normalized.run(
        "SELECT page,pages,total,raw_count FROM company_round_page WHERE scope=? ORDER BY page", (source_scope,),
    ).fetchall())
    observations = ReadRoundCompanyFinancialObservations(normalized, source_scope)
    previous_complete: set[str] = set()
    latest: dict[str, CompanyFinancialSourceObservation] = {}
    population = set(scope.codes)
    for item in observations:
        check()
        code = item.financial.code
        if code not in population or item.observed_at != scope.cutoff:
            raise ValueError("company_source_qualification_financial_scope_changed")
        if not item.missing_fields:
            previous_complete.add(code)
        if code not in latest or item.financial.report_date > latest[code].financial.report_date:
            latest[code] = item
    groups: dict[str, list[str]] = defaultdict(list)
    missing = []
    for code, item in sorted(latest.items()):
        if group := _group(item, policy):
            groups[group].append(code)
        if item.missing_fields:
            missing.append(code)
    sample = {
        name: sorted(
            groups[name],
            key=lambda code: hashlib.sha256(f"{round_id}:{name}:{code}".encode()).digest(),
        )[:5]
        for name in _GROUPS
    }
    review = set(missing[:_MISSING_LIMIT]).union(*(set(codes) for codes in sample.values()))
    reviewed = []
    for code in sorted(review):
        check()
        item = latest[code]
        materials, truncated = _materials(collected, documents, item, scope.cutoff, check)
        reviewed.append({
            "code": code,
            "report_date": item.financial.report_date.isoformat(),
            "source_notice_at": item.source_notice_at.isoformat(),
            "source_updated_at": item.source_updated_at.isoformat() if item.source_updated_at is not None else None,
            "source_record_hash": item.record_hash,
            "missing_fields": [field.field for field in item.missing_fields],
            "original_candidates": [material.project() for material in materials if material.role == "original_candidate"],
            "revision_candidates": [material.project() for material in materials if material.role == "revision_candidate"],
            "materials_truncated": truncated,
        })
    missing_by_field = Counter(field.field for item in latest.values() for field in item.missing_fields)
    return {
        "pages": pages.project(),
        "observation_rows": len(observations),
        "observed_stocks": len(latest),
        "without_observation": len(scope.codes) - len(latest),
        "latest_complete_stocks": len(latest) - len(missing),
        "latest_incomplete_stocks": len(missing),
        "without_complete_observation": len(scope.codes) - len(previous_complete),
        "missing_by_field": dict(missing_by_field),
        "observation_groups": {**{name: len(groups[name]) for name in _GROUPS}, "unknown": len(latest) - sum(map(len, groups.values()))},
        "sample": sample,
        "review": reviewed,
        "missing_candidates_truncated": len(missing) > _MISSING_LIMIT,
        "required_evidence": list(_FINANCIAL_EVIDENCE),
    }


def _review_ratios(
    inputs: SqliteConnection,
    scope: CompanySyncRoundScope,
    query: StructuredCompanyQuery,
    threshold: float,
) -> dict[str, object]:
    scope_id = scope.identity
    pages = _pages(inputs.run(
        "SELECT page,pages,total,raw_count FROM company_source_page WHERE scope=? AND component=? ORDER BY page",
        (scope_id, query.component),
    ).fetchall())
    snapshot = inputs.run("SELECT day FROM company_pledge_day WHERE scope=?", (scope_id,)).fetchone() if query.component == "pledge" else None
    start = end = date.fromisoformat(snapshot[0]) if snapshot is not None else None
    if query.component == "unlock":
        start, end = query.start, query.end
    if start is not None and not query.start <= start <= query.end:
        raise ValueError("company_source_qualification_snapshot_outside_scope")
    observations: dict[str, list[tuple[date, float]]] = defaultdict(list)
    population = set(scope.codes)
    rows = 0
    for row in inputs.run(
        "SELECT code,day,ratio FROM company_ratio_observation WHERE scope=? AND component=? ORDER BY code,day",
        (scope_id, query.component),
    ):
        code, day, ratio = row
        day = date.fromisoformat(day)
        if code not in population or start is None or end is None or not start <= day <= end or not 0 <= ratio <= 100:
            raise ValueError("company_source_qualification_ratio_outside_scope")
        observations[code].append((day, ratio))
        rows += 1
    candidates = sorted(
        code for code, entries in observations.items() if sum(ratio for _, ratio in entries) >= threshold
    )
    return {
        "requested_from": start.isoformat() if start is not None else None,
        "requested_through": end.isoformat() if end is not None else None,
        "pages": pages.project(),
        "observed_stocks": len(observations),
        "without_observation": len(population) - len(observations),
        "observation_rows": rows,
        "raw_unit": "percentage_points" if query.component == "pledge" else "fraction",
        "retained_unit": "percentage_points",
        "sample_threshold_pct": threshold,
        "threshold_candidates": len(candidates),
        "sample_observations": [
            {
                "code": code,
                "observation_count": len(observations[code]),
                "entries": [
                    {"date": day.isoformat(), "ratio_pct": ratio} for day, ratio in observations[code][:16]
                ],
            }
            for code in candidates[:10]
        ],
        "required_evidence": list(_RATIO_EVIDENCE),
    }


def AuditCompanySourceQualification(
    paths: DataPaths,
    *,
    documents: CompanyDocumentFiles,
    sqlite_policy: SqliteIoPolicy,
    policy: LongResearchPolicy,
    unlock_days: int,
    deadline: float,
    monotonic: Callable[[], float],
) -> dict[str, object]:
    """Inventory evidence only, with immutable reads on checkpointed inputs."""
    def check() -> None:
        if monotonic() >= deadline:
            raise _QualificationInterrupted("company_source_qualification_timed_out")

    databases = tuple(
        path.resolve(strict=True)
        for path in (paths.company_sync, paths.company_normalized, paths.company_inputs, paths.company_collection, paths.company)
    )
    if len(set(databases)) != len(databases):
        raise ValueError("company_source_qualification_same_input")
    before = _fingerprints(databases)
    with ExitStack() as stack:
        options = SqliteConnectionOptions(read_only=True, immutable=True)
        journal, normalized, inputs, collected, formal = (
            stack.enter_context(SqliteSession(
                path, options, cancel_requested=lambda: monotonic() >= deadline, sqlite_policy=sqlite_policy,
            ))
            for path in databases
        )
        for connection in (journal, normalized, inputs, collected, formal):
            stack.enter_context(SqliteTransaction(connection))
        round_id, cutoff_text = journal.run("SELECT round_id,cutoff FROM company_sync_round WHERE active=1").require_one()
        scope = CompanySyncRoundScope(
            tuple(row[0] for row in journal.run("SELECT code FROM company_sync_code WHERE round_id=? ORDER BY code", (round_id,))),
            datetime.fromisoformat(cutoff_text),
        )
        plans = {row[0]: row[1] for row in journal.run("SELECT family,request_hash FROM company_sync_plan WHERE round_id=?", (round_id,))}
        cutoff_day = scope.cutoff.date()
        oldest_day = cutoff_day - timedelta(days=365)
        financial = StructuredCompanyQuery("financial", oldest_day, cutoff_day)
        pledge = StructuredCompanyQuery("pledge", oldest_day, cutoff_day)
        unlock = StructuredCompanyQuery("unlock", cutoff_day, cutoff_day + timedelta(days=min(180, unlock_days)))
        if (
            plans.get("financial") != CompanyFinancialRequestIdentity(financial, "observations")
            or plans.get("pledge") != CanonicalDigest(pledge)
            or plans.get("unlock") != CanonicalDigest(unlock)
        ):
            raise ValueError("company_source_qualification_plan_identity_mismatch")
        source_scope = CanonicalDigest((financial.component, financial.start, financial.end, scope.codes, scope.cutoff))
        qualified = ReadCompanyPublicationIdentity(formal)
        if qualified is None:
            raise ValueError("company_source_qualification_publication_missing")
        financial_review = _review_financial(
            normalized, collected, documents, scope, source_scope, round_id, policy, check,
        )
        pledge_review = _review_ratios(inputs, scope, pledge, policy.pledge_thresholds[0])
        unlock_review = _review_ratios(inputs, scope, unlock, policy.unlock_thresholds[0])
        population = set(scope.codes)
        counts = [0, 0, 0]
        for row in formal.run("SELECT code,finance_state,pledge_state,unlock_state FROM company_facts ORDER BY code"):
            if row[0] not in population:
                continue
            for index, state in enumerate(row[1:]):
                if state == DATA_STATES[CompanyDataState.COMPLETE]:
                    counts[index] += 1
        for review, count in zip((financial_review, pledge_review, unlock_review), counts, strict=True):
            review["published_complete_stocks"] = count
    check()
    if _fingerprints(databases) != before:
        raise ValueError("company_source_qualification_input_changed")
    return {
        "status": "degraded",
        "findings": [],
        "summary": {"source_qualification_audit": {
            "scope": "frozen_company_inputs",
            "read_only": True,
            "source_data_writes": 0,
            "network_requests": 0,
            "production_adopted": False,
            "source_admission_verified": False,
            "stable_source_read": True,
            "round_id": round_id,
            "source_cutoff": scope.cutoff.isoformat(),
            "population": len(scope.codes),
            "publication_hash": qualified.content_hash.hex(),
            "financial": financial_review,
            "pledge": pledge_review,
            "unlock": unlock_review,
        }},
    }


def _project_pages(value: object) -> dict[str, object]:
    pages = ProjectMapping(value)
    return {
        **{key: SafeNonnegativeInt(pages.get(key)) for key in ("saved", "expected", "rows", "expected_rows")},
        "closed": pages.get("closed") is True,
    }


def _project_materials(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [
        {
            **{key: SafeText(row.get(key)) for key in (
                "source_key", "record_hash", "disclosed_at", "role", "body_status", "body_hash",
            )},
            **{key: SafeStringList(row.get(key), limit=8) for key in (
                "compared_fields", "conflicting_fields", "source_null_numeric_fields",
            )},
        }
        for row in value[:_MATERIAL_LIMIT] if isinstance(row, Mapping)
    ]


def ProjectCompanySourceQualification(source: Mapping[str, object]) -> dict[str, object]:
    financial = ProjectMapping(source.get("financial"))
    groups = ProjectMapping(financial.get("observation_groups"))
    sample = ProjectMapping(financial.get("sample"))
    missing_by_field = ProjectMapping(financial.get("missing_by_field"))
    review = financial.get("review")
    return {
        **{key: SafeText(source.get(key)) for key in ("scope", "round_id", "source_cutoff", "publication_hash")},
        **{key: SafeNonnegativeInt(source.get(key)) for key in ("population", "source_data_writes", "network_requests")},
        **{key: source.get(key) is True for key in (
            "read_only", "production_adopted", "source_admission_verified", "stable_source_read",
        )},
        "financial": {
            **{key: SafeNonnegativeInt(financial.get(key)) for key in (
                "observation_rows", "observed_stocks", "without_observation", "latest_complete_stocks",
                "latest_incomplete_stocks", "without_complete_observation", "published_complete_stocks",
            )},
            "pages": _project_pages(financial.get("pages")),
            "observation_groups": {key: SafeNonnegativeInt(groups.get(key)) for key in (*_GROUPS, "unknown")},
            "sample": {key: SafeStringList(sample.get(key), limit=5) for key in _GROUPS},
            "missing_by_field": {key: SafeNonnegativeInt(value) for key, value in missing_by_field.items() if key in (
                "basic_eps", "book_value_per_share", "revenue_growth_pct", "net_profit_growth_pct",
                "core_profit_growth_pct", "roe_pct", "parent_net_profit", "core_net_profit",
            )},
            "missing_candidates_truncated": financial.get("missing_candidates_truncated") is True,
            "required_evidence": list(_FINANCIAL_EVIDENCE),
            "review": [
                {
                    **{key: SafeText(row.get(key)) for key in (
                        "code", "report_date", "source_notice_at", "source_updated_at", "source_record_hash",
                    )},
                    "missing_fields": SafeStringList(row.get("missing_fields"), limit=8),
                    "original_candidates": _project_materials(row.get("original_candidates")),
                    "revision_candidates": _project_materials(row.get("revision_candidates")),
                    "materials_truncated": row.get("materials_truncated") is True,
                }
                for row in review[:_MISSING_LIMIT + 20] if isinstance(row, Mapping)
            ] if isinstance(review, list) else [],
        },
        **{component: _project_ratios(ProjectMapping(source.get(component))) for component in ("pledge", "unlock")},
    }


def _project_ratios(value: Mapping[str, object]) -> dict[str, object]:
    threshold = value.get("sample_threshold_pct")
    sample = value.get("sample_observations")
    return {
        **{key: SafeText(value.get(key)) for key in ("requested_from", "requested_through", "raw_unit", "retained_unit")},
        **{key: SafeNonnegativeInt(value.get(key)) for key in (
            "observed_stocks", "without_observation", "observation_rows", "threshold_candidates",
            "published_complete_stocks",
        )},
        "sample_threshold_pct": (
            float(threshold) if isinstance(threshold, float | int) and not isinstance(threshold, bool)
            and math.isfinite(threshold) else None
        ),
        "pages": _project_pages(value.get("pages")),
        "sample_observations": [
            {
                "code": SafeText(row.get("code")),
                "observation_count": SafeNonnegativeInt(row.get("observation_count")),
                "entries": _project_ratio_entries(row.get("entries")),
            }
            for row in sample[:10] if isinstance(row, Mapping)
        ] if isinstance(sample, list) else [],
        "required_evidence": list(_RATIO_EVIDENCE),
    }


def _project_ratio_entries(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [
        {"date": SafeText(row.get("date")), "ratio_pct": ratio}
        for row in value[:16] if isinstance(row, Mapping)
        and isinstance(ratio := row.get("ratio_pct"), int | float)
        and not isinstance(ratio, bool) and 0 <= ratio <= 100
    ]
