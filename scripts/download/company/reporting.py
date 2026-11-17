"""Company source and publication diagnostic projections."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from scripts.core.contracts import DiagnosticResult
from scripts.core.projection_values import ProjectMapping, ProjectSummary, SafeNonnegativeInt, SafeStringList, SafeText
from scripts.download.company.projections import ProjectCompanyResume, ProjectCompanyRiskComparisonDetails
from scripts.download.company.company_official_financial_audit import ProjectCompanyOfficialFinancialAudit
from trader.download.domain.company_evidence import EvidenceMarket

_FINANCIAL_PAGE_COLUMNS = (
    "SECUCODE", "REPORT_DATE", "NOTICE_DATE", "EPSJB", "BPS", "TOTALOPERATEREVETZ",
    "PARENTNETPROFITTZ", "KCFJCXSYJLRTZ", "ROEJQ", "PARENTNETPROFIT", "KCFJCXSYJLR",
)
_FINANCIAL_SOURCE_COLUMNS = (
    "SECUCODE", "SECURITY_CODE", "REPORT_DATE", "REPORT_TYPE", "NOTICE_DATE", "UPDATE_DATE", "CURRENCY",
    *_FINANCIAL_PAGE_COLUMNS[3:],
)


def _structured_code(value: object) -> str | None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{6}", value) is None:
        return None
    try:
        EvidenceMarket(value)
    except ValueError:
        return None
    return value


def NormalizeResult(result: DiagnosticResult, *, local: bool = False) -> DiagnosticResult:
    source = result.payload
    if source is None or result.error_code is not None:
        return result
    if source.get("probe") == "structured_page":
        observation = source.get("observation_status", source.get("status"))
        calls, attempts = source.get("fetch_invocations"), source.get("physical_http_attempts")
        key_filter = source.get("key_filter_requested") is True
        source_records = source.get("financial_source_records_requested", False)
        codes = source.get("requested_codes")
        key_count = len(codes) if isinstance(codes, list) else 0
        request_limit = 2 if key_filter else 1
        valid = (
            observation in ("decoded", "empty", "failed", "timed_out", "skipped")
            and source.get("component") in ("financial", "pledge", "unlock")
            and source.get("read_only") is True
            and all(source.get(name) is False for name in (
                "production_adopted", "field_correctness_verified", "full_window_coverage_verified",
                "latest_snapshot_verified", "key_lookup_verified", "revision_index_verified",
            ))
            and type(source.get("key_filter_requested")) is bool
            and type(source_records) is bool
            and type(calls) is int and 0 <= calls <= request_limit
            and (attempts is None or type(attempts) is int and 0 <= attempts <= request_limit)
            and all(type(source.get(name)) is int and source.get(name) == expected for name, expected in (
                ("page_limit", request_limit), ("request_limit", request_limit),
                ("page_size", key_count if key_filter else 500),
                ("max_response_bytes", 1024 * 1024 if key_filter or source_records else 16 * 1024 * 1024), ("pages_followed", 0),
            ))
        )
        if source_records:
            valid = valid and not key_filter and source.get("component") == "financial" and (
                source.get("requested_fields") == list(_FINANCIAL_SOURCE_COLUMNS)
                and source.get("remote_column_projection_requested") is True
            )
            if observation in ("decoded", "empty"):
                valid = valid and _valid_financial_source_records(source)
        if key_filter:
            valid = valid and source.get("component") == "financial" and isinstance(codes, list) and (
                2 <= len(codes) <= 8 and all(isinstance(code, str) and _structured_code(code) is not None for code in codes)
                and len(set(codes)) == len(codes)
            )
            valid = valid and source.get("request_style") == "APP_F10_MAINFINADATA" and (
                source.get("remote_column_projection_requested") is False and source.get("requested_fields") == []
                and source.get("required_fields") == list(_FINANCIAL_PAGE_COLUMNS)
            )
            key_probe = source.get("key_filter_probe")
            if isinstance(key_probe, Mapping):
                queries = key_probe.get("queries")
                verified = key_probe.get("filter_verified")
                valid = valid and isinstance(queries, list) and len(queries) == calls and type(verified) is bool and (
                    key_probe.get("support_status") == ("sample_verified" if verified else "not_verified")
                    and key_probe.get("date_filter_independently_verified") is False
                    and key_probe.get("codes") == codes and key_probe.get("report_date") == source.get("requested_report_date")
                    and key_probe.get("request_style") == source.get("request_style")
                )
                if verified:
                    valid = valid and calls == 2 and (attempts is None or attempts == 2) and isinstance(queries, list) and isinstance(codes, list) and all(
                        isinstance(query, Mapping) and query.get("query") == label and query.get("status") == "decoded"
                        and query.get("selected_codes") == selected
                        and query.get("request_style") == source.get("request_style")
                        and type(query.get("http_status")) is int and query.get("http_status") == 200
                        and type(query.get("provider_code")) is int and query.get("provider_code") == 0
                        and query.get("provider_success") is True
                        and all(type(query.get(name)) is int and query.get(name) == expected for name, expected in (
                            ("raw_rows", len(selected)), ("reported_total", len(selected)), ("reported_pages", 1),
                        ))
                        for query, label, selected in zip(queries, ("batch", "exclude_last_key"), (codes, codes[:-1]), strict=False)
                    )
            else:
                valid = valid and calls == 0 and observation == "skipped"
        page_status = "degraded" if valid and observation not in ("failed", "timed_out") and source.get("status") not in (
            "failed", "timed_out", "cancelled",
        ) else "failed"
        return replace(result, payload=dict(source, status=page_status, observation_status=observation))
    if source.get("probe") == "st_name_history":
        return _normalize_st_source_probe(result, source)
    if "financial_revision_probe" in source:
        revision = source["financial_revision_probe"]
        if not isinstance(revision, Mapping):
            return replace(result, payload=dict(source, status="failed"))
        queries = revision.get("queries")
        count = revision.get("request_count")
        valid = (
            source.get("status") in ("passed", "degraded", "failed")
            and revision.get("read_only") is True
            and all(revision.get(name) is False for name in (
                "production_adopted", "field_correctness_verified", "full_window_coverage_verified", "sort_verified",
                "issuer_revision_verified", "original_disclosure_verified",
            ))
            and type(count) is int and 0 <= count <= 3
            and isinstance(queries, list) and len(queries) == count
            and type(revision.get("request_limit")) is int and revision.get("request_limit") == 3
            and type(revision.get("max_response_bytes")) is int and revision.get("max_response_bytes") == 1024 * 1024
            and type(revision.get("update_filter_verified")) is bool
            and revision.get("support_status") in ("sample_verified", "unverified", "not_verified")
        )
        if revision.get("update_filter_verified") is True:
            valid = valid and revision.get("support_status") == "sample_verified" and count == 3
            if isinstance(queries, list):
                valid = valid and len(queries) == 3 and all(
                    isinstance(query, Mapping) and query.get("query") == label
                    and query.get("status") == status and type(query.get("row_count")) is int
                    and query.get("row_count") == rows
                    for query, label, status, rows in zip(queries, (
                        "baseline", "include_update_boundary", "exclude_update_boundary",
                    ), ("decoded", "decoded", "empty"), (1, 1, 0), strict=False)
                )
        elif revision.get("support_status") == "sample_verified":
            valid = False
        if "production_fields_control" in revision:
            valid = valid and source.get("status") == "failed" and revision.get("update_filter_verified") is False
        revision_status = "degraded" if valid and source.get("status") != "failed" else "failed"
        return replace(result, payload=dict(source, status=revision_status))
    schema = source.get("schema_version")
    summary = source.get("summary")
    if schema == "company-resume" and isinstance(summary, Mapping) and "field_proof_audit" in summary:
        audit = summary["field_proof_audit"]
        valid = isinstance(audit, Mapping) and (
            source.get("status") in ("passed", "degraded", "failed")
            and audit.get("read_only") is True and audit.get("scope") == "current_rules_published_verified_fields"
            and all(audit.get(name) is False for name in (
                "production_adopted", "field_correctness_verified", "full_market_verified", "http_checked",
            ))
            and all(type(audit.get(name)) is int and audit.get(name) == 0 for name in ("network_requests", "source_data_writes"))
        )
        if valid and source.get("status") == "passed" and isinstance(audit, Mapping):
            count = audit.get("target_fields")
            valid = (audit.get("complete") is True and audit.get("stable_read") is True
                     and audit.get("budget_exhausted") is False and source.get("findings") == []
                     and type(count) is int and count > 0
                     and all(type(audit.get(key)) is int and audit.get(key) == count for key in ("checked_fields", "reproved_fields"))
                     and audit.get("current_extraction_hash") == audit.get("extraction_hash")
                     and type(audit.get("unreproved_fields")) is int and audit.get("unreproved_fields") == 0
                     and all(isinstance(audit.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", audit[key]) is not None
                             for key in ("publication_hash", "extraction_hash", "current_extraction_hash", "field_rule_hash")))
        return replace(result, payload=dict(source, status=source.get("status") if valid else "failed"))
    if schema == "company-resume" and isinstance(summary, Mapping) and "consumer_audit" in summary:
        audit = summary["consumer_audit"]
        valid = isinstance(audit, Mapping) and (
            source.get("status") in ("passed", "degraded", "failed")
            and audit.get("read_only") is True and audit.get("scope") == "formal_reader_consistency_only"
            and all(audit.get(name) is False for name in (
                "collection_checked", "recovery_planned", "field_correctness_verified", "full_market_verified", "http_checked",
            ))
        )
        if valid and source.get("status") == "passed" and isinstance(audit, Mapping):
            read = audit.get("consumer_read")
            valid = (
                audit.get("complete") is True and audit.get("stable_read") is True
                and audit.get("budget_exhausted") is False and source.get("findings") == []
                and all(isinstance(audit.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", audit[key]) is not None
                        for key in ("publication_hash", "extraction_hash"))
                and isinstance(audit.get("published_at"), str)
                and isinstance(read, Mapping) and read.get("http_checked") is False
                and all(read.get(name) is True for name in (
                    "checked", "facts_equal", "fields_equal", "publication_equal", "consistent",
                ))
                and type(read.get("stocks")) is int and 0 < read["stocks"] <= 12000
                and type(read.get("fields")) is int and read["fields"] >= 0
            )
        return replace(result, payload=dict(source, status=source.get("status") if valid else "failed"))
    if schema == "company-field-replay":
        valid = (
            isinstance(source.get("status"), str) and source.get("status") in ("passed", "degraded", "failed")
            and source.get("read_only_source") is True and source.get("isolated_copy") is True
            and source.get("production_authorized") is False and source.get("field_correctness_verified") is False
            and source.get("full_market_verified") is False and source.get("performance_verified") is False
            and _migration_count(source.get("network_requests")) == 0
            and _migration_count(source.get("source_data_writes")) == 0
        )
        if source.get("status") == "passed":
            valid = valid and all(_migration_count(source.get(name)) == 0 for name in (
                "second_new_field_attempted", "missing_texts", "round_2_publication_text_reads",
                "round_1_publication_pending_codes", "round_2_publication_pending_codes",
            )) and all(source.get(name) is True for name in (
                "source_input_unchanged", "collection_backup_generation_stable", "original_valid_receipts_unchanged",
                "second_fields_and_publication_unchanged", "second_zero_publication_text_reads",
                "facts_backup_generation_stable",
                "original_risks_and_evidence_preserved", "unselected_facts_unchanged",
            ))
            for number in (1, 2):
                comparison = source.get(f"round_{number}_risk_comparison")
                valid = valid and isinstance(comparison, Mapping) and (
                    comparison.get("all_old_risks_and_evidence_preserved") is True
                    and comparison.get("missing") == [] and comparison.get("changed_requires_review") == []
                )
        return replace(result, payload=dict(source, status=source.get("status") if valid else "failed"))
    if schema in ("company-annual-income-source", "company-structured-field-sampling"):
        return _normalize_source_probe(result, source)
    if schema != "company-field-material":
        return result
    status = source.get("status")
    if status in ("failed", "timed_out", "cancelled", "skipped", "empty"):
        return result
    observation = source.get("observation_status", status)
    valid = (
        status in ("observed", "degraded")
        and observation in ("observed", "degraded")
        and source.get("read_only") is True
        and source.get("field_correctness_verified") is False
        and isinstance(source.get("stable_read"), bool)
        and (observation != "observed" or source.get("stable_read") is True)
        and isinstance(source.get("items"), list)
        and isinstance(source.get("missing_source_keys"), list)
    )
    return replace(result, payload=dict(source, status="degraded" if valid else "failed", observation_status=observation))


def _normalize_source_probe(result: DiagnosticResult, source: Mapping[str, object]) -> DiagnosticResult:
    status = source.get("status")
    if status in ("failed", "timed_out", "cancelled", "skipped", "empty"):
        return result
    annual = source.get("schema_version") == "company-annual-income-source"
    native_statuses = ("decoded", "degraded") if annual else ("passed", "degraded")
    observation = source.get("observation_status", status)
    valid = (
        status in native_statuses
        and observation in native_statuses
        and source.get("read_only") is True
        and source.get("field_correctness_verified") is False
        and source.get("full_window_coverage_verified") is False
    )
    if annual:
        valid = valid and isinstance(source.get("requested_periods"), list) and (
            observation != "decoded" or isinstance(source.get("records"), list)
        )
    else:
        valid = valid and source.get("production_adopted") is False and isinstance(source.get("sources"), list)
    return replace(result, payload=dict(source, status="degraded" if valid else "failed", observation_status=observation))


_ST_SOURCES = ("eastmoney_f10", "sina_name_history")
_ST_STATUSES = ("ever_st", "clear", "unknown")
_ST_OBSERVATION_LIMIT = 64 * (5 + 1)
_ST_STRUCTURE_FIELDS = frozenset({"jbzl", "SECURITY_CODE", "SECURITY_NAME_ABBR", "STR_NAMEA", "FORMERNAME", "name_history"})
_ST_COUNTS = (
    "planned_requests", "actual_requests", "http_successes", "usable_results", "unknown_results",
    "failed_requests", "not_requested", "known_positive_misses", "known_positive_unknown",
    "historical_positive_hits", "historical_positive_unverified", "local_clear_conflicts",
)


def _st_count(value: object, maximum: int = _ST_OBSERVATION_LIMIT) -> int | None:
    return value if type(value) is int and 0 <= value <= maximum else None


def _st_code(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[036][0-9]{5}", value) else None


def _normalize_st_source_probe(result: DiagnosticResult, source: Mapping[str, object]) -> DiagnosticResult:
    status = source.get("status")
    observation = source.get("observation_status", status)
    rounds, workers = _st_count(source.get("rounds"), 5), _st_count(source.get("workers"), 4)
    planned = _st_count(source.get("planned_requests"))
    actual = _st_count(source.get("actual_requests"))
    timeout = _st_number(source.get("timeout_seconds"))
    sources, observations, comparisons = source.get("sources"), source.get("observations"), source.get("comparisons")
    valid = (
        observation in ("observed", "degraded", "cancelled", "timed_out", "failed")
        and (status == observation or status == "degraded" and observation == "observed" or status == "failed")
        and "schema_version" not in source and source.get("read_only") is True
        and type(source.get("source_data_writes")) is int and source.get("source_data_writes") == 0
        and all(source.get(name) is False for name in ("production_adopted", "field_correctness_verified", "full_market_verified"))
        and source.get("expectation_role") == "local_reference_not_absolute_truth"
        and source.get("session_policy") == "new_transport_per_request"
        and rounds is not None and rounds >= 1 and workers is not None and workers >= 1
        and timeout is not None and 0 < timeout <= 15
        and type(source.get("max_response_bytes")) is int and source.get("max_response_bytes") == 1024 * 1024
        and planned is not None and actual is not None and actual <= planned
        and _st_count(source.get("request_limit")) == planned
        and isinstance(sources, list) and len(sources) == 2
        and isinstance(observations, list) and len(observations) == planned
        and isinstance(comparisons, list) and 1 <= len(comparisons) <= 64
        and ("reference_hash" not in source or _st_digest(source.get("reference_hash")) is not None)
    )
    if valid and isinstance(sources, list) and isinstance(observations, list) and isinstance(comparisons, list) and rounds is not None:
        valid = planned == len(comparisons) * (rounds + 1)
        source_names = [item.get("source") if isinstance(item, Mapping) and isinstance(item.get("source"), str) else None for item in sources]
        valid = valid and set(source_names) == set(_ST_SOURCES)
        codes = {_st_code(item.get("code")) if isinstance(item, Mapping) else None for item in comparisons}
        valid = valid and None not in codes and len(codes) == len(comparisons)
        observed_keys: set[tuple[str, str, int]] = set()
        attempted = 0
        for item in observations:
            if not isinstance(item, Mapping):
                valid = False
                continue
            provider, code, number = item.get("source"), _st_code(item.get("code")), _st_count(item.get("round"), rounds)
            known = isinstance(provider, str) and provider in _ST_SOURCES and code in codes and number is not None and number >= 1
            known = known and (provider != "sina_name_history" or number == 1)
            if known and isinstance(provider, str) and code is not None and number is not None:
                key = provider, code, number
                valid = valid and key not in observed_keys
                observed_keys.add(key)
            else:
                valid = False
            valid = valid and type(item.get("attempted")) is bool and item.get("status") in (
                "decoded", "unknown_unverified", "failed", "timed_out", "cancelled", "not_requested",
            ) and item.get("eligibility") in _ST_STATUSES and item.get("expected_status") in _ST_STATUSES
            attempted += item.get("attempted") is True
        valid = valid and attempted == actual
        for item in sources:
            if not isinstance(item, Mapping):
                valid = False
                continue
            entries = [value for value in observations if isinstance(value, Mapping) and value.get("source") == item.get("source")]
            valid = valid and _st_count(item.get("planned_requests")) == len(entries)
            valid = valid and _st_count(item.get("actual_requests")) == sum(value.get("attempted") is True for value in entries)
            if observation == "observed":
                valid = valid and all(item.get(name) == 0 for name in (
                    "known_positive_misses", "known_positive_unknown", "historical_positive_unverified", "local_clear_conflicts",
                ))
        if observation == "observed":
            valid = valid and source.get("source_conflicts") == 0
            valid = valid and source.get("eligibility_drift_codes") == source.get("observation_drift_codes") == []
            valid = valid and all(isinstance(item, Mapping) and item.get("status") == "decoded" for item in observations)
    normalized = "failed" if status == "failed" else "degraded" if observation == "observed" else observation
    return replace(result, payload=dict(source, status=normalized if valid else "failed", observation_status=observation))


def _st_excerpt(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = re.sub(r"(?:https?://|www\.)\S+|\S+@\S+|[0-9]{7,}", "[省略]", value)
    return "".join(character for character in clean if character.isprintable())[:160]


def _st_token(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:+-]{1,100}", value) else None


def _st_digest(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None


def _st_diagnostic(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    causes = value.split(" > ")
    pattern = r"[A-Za-z_][A-Za-z0-9_]{0,63}(?::(?:errno_-?[0-9]{1,5}|HTTP_[0-9]{3}|SQLITE_[A-Z0-9_]{1,48}))*(?::[A-Za-z0-9_.-]{1,80}:[A-Za-z0-9_.-]{1,80}:[0-9]{1,8})?"
    return value[:1000] if len(causes) <= 4 and all(re.fullmatch(pattern, cause) for cause in causes) else None


def _st_number(value: object, *, rate: bool = False) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1_000_000_000:
        return None
    number = _number(value)
    return number if number is not None and 0 <= number and (not rate or number <= 1) else None


def _st_structure(value: object) -> dict[str, object]:
    source = value if isinstance(value, Mapping) else {}
    fields = source.get("fields")
    projected = {name: kind for name, kind in fields.items()
                 if name in _ST_STRUCTURE_FIELDS and kind in ("str", "int", "float", "bool", "list", "object", "null", "other")} if isinstance(fields, Mapping) else {}
    return {
        "format": source.get("format") if source.get("format") in ("json", "html_name_history", "not_decoded") else None,
        "rows": _st_count(source.get("rows"), 1_000_000), "fields": projected,
        "observed_code": (code if isinstance((code := source.get("observed_code")), str)
                          and re.fullmatch(r"[0-9]{6}", code) else None),
        **{name: _st_identity_name(source.get(name)) for name in ("observed_current_name", "observed_a_name")},
    }


def _st_identity_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = unicodedata.normalize("NFKC", value).strip()
    excerpt = _st_excerpt(name) if 1 <= len(name) <= 40 else None
    return excerpt[:40] if excerpt is not None else None


def _st_source_details(source: Mapping[str, object], payload: dict[str, object]) -> None:
    sources, observations, comparisons = source.get("sources"), source.get("observations"), source.get("comparisons")
    projected_sources: list[dict[str, object]] = []
    for item in sources[:2] if isinstance(sources, list) else []:
        if not isinstance(item, Mapping) or item.get("source") not in _ST_SOURCES:
            continue
        confusion = item.get("confusion")
        rows = confusion if isinstance(confusion, Mapping) else {}
        projected_sources.append({
            "source": item["source"], **{name: _st_count(item.get(name)) for name in _ST_COUNTS},
            **{name: _st_number(item.get(name), rate=True) for name in ("success_rate", "http_success_rate")},
            **{name: _st_number(item.get(name)) for name in ("p50_seconds", "p95_seconds", "max_seconds")},
            "confusion": {expected: {actual: _st_count(row.get(actual)) for actual in sorted(_ST_STATUSES)}
                          for expected in sorted(_ST_STATUSES) if isinstance((row := rows.get(expected)), Mapping)},
        })
    projected_observations: list[dict[str, object]] = []
    for item in observations[:_ST_OBSERVATION_LIMIT] if isinstance(observations, list) else []:
        if not isinstance(item, Mapping):
            continue
        projected_observations.append({
            "source": item.get("source") if item.get("source") in _ST_SOURCES else None,
            "code": _st_code(item.get("code")), "round": _st_count(item.get("round"), 5),
            **{name: _st_excerpt(item.get(name)) for name in ("current_name", "former_names_excerpt")},
            **{name: item.get(name) if item.get(name) in _ST_STATUSES else None
               for name in ("expected_status", "eligibility", "original_parser_status")},
            "status": item.get("status") if item.get("status") in (
                "decoded", "unknown_unverified", "failed", "timed_out", "cancelled", "not_requested",
            ) else None,
            **{name: item.get(name) if type(item.get(name)) is bool else None for name in (
                "attempted", "expected_match", "current_name_matches", "current_name_is_st", "history_is_st", "empty_history",
            )},
            "elapsed_seconds": _st_number(item.get("elapsed_seconds")),
            "http_status": _st_count(item.get("http_status"), 599) if type(item.get("http_status")) is int and item["http_status"] >= 100 else None,
            "response_bytes": _st_count(item.get("response_bytes"), 1024 * 1024 * 1024),
            "reason": _st_token(item.get("reason")), "diagnostic": _st_diagnostic(item.get("diagnostic")),
            "structure": _st_structure(item.get("structure")),
            "former_names_sha256": _st_digest(item.get("former_names_sha256")),
        })
    projected_comparisons: list[dict[str, object]] = []
    for item in comparisons[:64] if isinstance(comparisons, list) else []:
        if not isinstance(item, Mapping):
            continue
        eligibility = item.get("eastmoney_eligibility")
        projected_comparisons.append({
            "code": _st_code(item.get("code")), "current_name": _st_excerpt(item.get("current_name")),
            **{name: item.get(name) if item.get(name) in _ST_STATUSES else None for name in ("expected_status", "sina_eligibility")},
            "eastmoney_eligibility": [value if value in _ST_STATUSES else None for value in eligibility[:5]] if isinstance(eligibility, list) else [],
            "source_conflicts": _st_count(item.get("source_conflicts"), 5),
            **{name: item.get(name) if type(item.get(name)) is bool else None for name in ("eligibility_drift", "observation_drift", "sina_expected_match")},
            **{name: [value if type(value) is bool else None for value in values[:5]] if isinstance((values := item.get(name)), list) else []
               for name in ("expected_matches", "historical_positive_found")},
        })
    payload["summary"] = {
        "probe": "st_name_history", "observation_status": (observation if (observation := source.get("observation_status", source.get("status")))
                                                            in ("observed", "degraded", "cancelled", "timed_out", "failed") else None),
        "read_only": source.get("read_only") is True, "source_data_writes": _st_count(source.get("source_data_writes")),
        **{name: source.get(name) if type(source.get(name)) is bool else None
           for name in ("production_adopted", "field_correctness_verified", "full_market_verified")},
        "observed_at": _st_token(source.get("observed_at")),
        "reference_hash": _st_digest(source.get("reference_hash")),
        "expectation_role": "local_reference_not_absolute_truth" if source.get("expectation_role") == "local_reference_not_absolute_truth" else None,
        "session_policy": "new_transport_per_request" if source.get("session_policy") == "new_transport_per_request" else None,
        **{name: _st_count(source.get(name)) for name in ("rounds", "workers", "planned_requests", "request_limit", "actual_requests", "source_conflicts")},
        "max_response_bytes": _st_count(source.get("max_response_bytes"), 1024 * 1024),
        "timeout_seconds": _st_number(source.get("timeout_seconds")), "elapsed_seconds": _st_number(source.get("elapsed_seconds")),
        "sources": projected_sources, "observations": projected_observations, "comparisons": projected_comparisons,
        **{name: [code for code in values[:64] if _st_code(code) is not None] if isinstance((values := source.get(name)), list) else []
           for name in ("eligibility_drift_codes", "observation_drift_codes")},
        "observations_truncated": isinstance(observations, list) and len(observations) > _ST_OBSERVATION_LIMIT,
    }


def CompleteReport(report: dict[str, object], results: Sequence[DiagnosticResult]) -> None:
    return None


def _text(value: object, limit: int = 160) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _configuration_hash(source: Mapping[str, object]) -> str | None:
    value = source.get("runtime_configuration_sha256")
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None else None


def _annual_income_details(source: Mapping[str, object], payload: dict[str, object]) -> None:
    records = source.get("records")
    periods = source.get("requested_periods")
    observation = source.get("observation_status", source.get("status"))
    projected: list[dict[str, object]] = []
    for record in (records[:8] if isinstance(records, list) else []):
        if not isinstance(record, dict):
            continue
        row: dict[str, object] = {name: _text(record.get(name), 64) for name in (
            "report_date", "disclosed_at", "updated_at", "currency", "record_hash",
        )}
        row["operating_revenue"] = _number(record.get("operating_revenue"))
        projected.append(row)
    payload["summary"] = {
        "runtime_configuration_sha256": _configuration_hash(source),
        "read_only": source.get("read_only") is True,
        "field_correctness_verified": False,
        "full_window_coverage_verified": False,
        "observation_status": observation if observation in ("decoded", "degraded") else None,
        "source": _text(source.get("source")),
        "code": _text(source.get("code"), 6),
        "requested_periods": [value[:10] for value in (periods[:8] if isinstance(periods, list) else []) if isinstance(value, str)],
        "records": projected,
        "records_truncated": isinstance(records, list) and len(records) > 8,
        "failure": _text(source.get("failure")),
        "elapsed_seconds": _number(source.get("elapsed_seconds")),
    }


def _valid_financial_source_records(source: Mapping[str, object]) -> bool:
    records = source.get("source_records")
    if not isinstance(records, Mapping):
        return False
    raw, accepted, excluded = (records.get(name) for name in ("raw_rows", "accepted_rows", "excluded_rows"))
    unknown, nulls = (records.get(name) for name in ("source_updated_at_unknown_rows", "source_null_fields"))
    digest = records.get("response_sha256")
    return (
        type(raw) is int and type(accepted) is int and type(excluded) is int
        and 0 <= accepted <= raw <= 500 and excluded == raw - accepted
        and raw == source.get("raw_rows") and accepted == source.get("decoded_rows")
        and type(unknown) is int and 0 <= unknown <= accepted
        and type(nulls) is int and 0 <= nulls <= accepted * 8
        and records.get("currency") == "CNY" and records.get("issuer_revision_verified") is False
        and records.get("original_disclosure_verified") is False
        and records.get("business_key") == "code_report_date"
        and records.get("hash_basis") == "normalized_same_source_row"
        and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
    )


def _structured_page_details(source: Mapping[str, object], payload: dict[str, object]) -> None:
    component = source.get("component")
    columns = {
        "financial": _FINANCIAL_PAGE_COLUMNS,
        "pledge": ("SECURITY_CODE", "TRADE_DATE", "PLEDGE_RATIO"),
        "unlock": ("SECURITY_CODE", "FREE_DATE", "TOTAL_RATIO"),
    }.get(component if isinstance(component, str) else "", ())
    if source.get("financial_source_records_requested") is True and component == "financial":
        columns = _FINANCIAL_SOURCE_COLUMNS
    fields = source.get("fields")
    projected = []
    for field in (fields[:8] if isinstance(fields, list) else []):
        if not isinstance(field, Mapping) or field.get("field") not in columns:
            continue
        projected.append({
            "field": field["field"],
            **{name: _st_count(field.get(name), 500) for name in ("non_null_rows", "decoded_null_rows")},
        })
    observation = source.get("observation_status", source.get("status"))
    discovery = source.get("normal_path_discovery")
    summary: dict[str, object] = {
        "runtime_configuration_sha256": _configuration_hash(source), "probe": "structured_page",
        "source": "eastmoney", "read_only": source.get("read_only") is True,
        "key_filter_requested": source.get("key_filter_requested") is True,
        "requested_codes": [code for code in SafeStringList(source.get("requested_codes"), limit=8) if _structured_code(code) is not None],
        "requested_report_date": _text(source.get("requested_report_date"), 10),
        "request_style": "APP_F10_MAINFINADATA" if source.get("request_style") == "APP_F10_MAINFINADATA" else None,
        "remote_column_projection_requested": source.get("remote_column_projection_requested") is True,
        "component": component if component in ("financial", "pledge", "unlock") else None,
        "observation_status": observation if observation in ("decoded", "empty", "failed", "timed_out", "skipped") else None,
        **{name: False for name in (
            "production_adopted", "field_correctness_verified", "full_window_coverage_verified",
            "latest_snapshot_verified", "key_lookup_verified", "revision_index_verified",
        )},
        **{name: _text(source.get(name), 64) for name in ("observed_at", "window_start", "window_end", "failure")},
        **{name: _st_count(source.get(name), limit) for name, limit in (
            ("fetch_invocations", 1_000_000), ("physical_http_attempts", 1_000_000), ("request_limit", 2), ("page_limit", 2),
            ("page_size", 500), ("pages_followed", 0), ("raw_rows", 500), ("decoded_rows", 500),
            ("reported_pages", 256), ("reported_total", 128000),
            ("max_response_bytes", 16 * 1024 * 1024), ("response_bytes", 16 * 1024 * 1024),
        )},
        "normal_path_discovery": discovery if discovery in ("pledge_day_required", "none_in_current_fetch") else None,
        "more_pages_available": source.get("more_pages_available") if type(source.get("more_pages_available")) is bool else None,
        "requested_fields": [field for field in SafeStringList(source.get("requested_fields"), limit=15) if field in columns],
        "required_fields": [field for field in SafeStringList(source.get("required_fields"), limit=11) if field in columns],
        "fields": projected, "elapsed_seconds": _number(source.get("elapsed_seconds")),
    }
    payload["summary"] = summary
    summary["financial_source_records_requested"] = source.get("financial_source_records_requested") is True
    records = source.get("source_records")
    if source.get("financial_source_records_requested") is True and isinstance(records, Mapping):
        digest = records.get("response_sha256")
        summary["source_records"] = {
            **{name: _st_count(records.get(name), maximum) for name, maximum in (
                ("raw_rows", 500), ("accepted_rows", 500), ("excluded_rows", 500),
                ("source_updated_at_unknown_rows", 500), ("source_null_fields", 4000),
            )},
            "response_sha256": digest if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) else None,
            "currency": "CNY" if records.get("currency") == "CNY" else None,
            "issuer_revision_verified": False, "original_disclosure_verified": False,
            "business_key": "code_report_date", "hash_basis": "normalized_same_source_row",
        }
    probe = source.get("key_filter_probe")
    if isinstance(probe, Mapping):
        queries = probe.get("queries")
        verified = source.get("status") != "failed" and probe.get("filter_verified") is True
        evidence = []
        for query in queries[:2] if isinstance(queries, list) else []:
            if not isinstance(query, Mapping):
                continue
            digest = query.get("response_sha256")
            evidence.append({
                "query": query.get("query") if query.get("query") in ("batch", "exclude_last_key") else None,
                "status": query.get("status") if query.get("status") in ("decoded", "failed") else None,
                "selected_codes": [code for code in SafeStringList(query.get("selected_codes"), limit=8) if _structured_code(code) is not None],
                "request_style": "APP_F10_MAINFINADATA" if query.get("request_style") == "APP_F10_MAINFINADATA" else None,
                **{name: _st_count(query.get(name), maximum) for name, maximum in (
                    ("http_status", 599), ("response_bytes", 1024 * 1024),
                    ("raw_rows", 500), ("reported_total", 128000), ("reported_pages", 256),
                )},
                "response_sha256": digest if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None else None,
                "provider_code": query.get("provider_code") if type(query.get("provider_code")) is int else None,
                "provider_success": query.get("provider_success") if type(query.get("provider_success")) is bool else None,
            })
        summary["key_filter_probe"] = {
            "support_status": "sample_verified" if verified else "not_verified", "filter_verified": verified,
            "codes": [code for code in SafeStringList(probe.get("codes"), limit=8) if _structured_code(code) is not None],
            "report_date": _text(probe.get("report_date"), 10),
            "request_style": "APP_F10_MAINFINADATA" if probe.get("request_style") == "APP_F10_MAINFINADATA" else None,
            "date_filter_independently_verified": False, "queries": evidence,
        }


_IDENTITY_FIELDS: Mapping[str, tuple[str, ...]] = {
    "repurchase": (
        "DIM_SCODE", "UPDATEDATE", "REPURNUM", "REPURAMOUNT", "REPURNUMLOWER", "REPURNUMCAP", "ZSZXX", "ZSZSX",
        "REPUROBJECTIVE", "REPURPROGRESS", "REPURCODE", "DIM_TRADEDATE",
    ),
    "holder_trade": (
        "SECURITY_CODE", "NOTICE_DATE", "CHANGE_NUM", "CHANGE_NUM_SYMBOL", "AFTER_CHANGE_RATE", "CHANGE_FREE_RATIO",
        "HOLD_RATIO", "HOLDER_NAME", "DIRECTION", "START_DATE", "END_DATE",
    ),
    "annual_income": (
        "SECURITY_CODE", "NOTICE_DATE", "OPERATE_INCOME", "TOTAL_OPERATE_INCOME", "REPORT_DATE", "REPORT_TYPE_CODE", "DATA_STATE",
    ),
    "contract": (
        "SECURITYCODE", "DIM_RDATE", "AMOUNTS", "SNDYYSR", "ZSNDYYSRBL", "OPERATEREVE", "SIGNATORY",
        "SIGNATORYRELNAME", "SIGNDATE", "SIGNEFFECT", "ISABOLISHED", "YEAR", "CONTRACTNAME", "COUNTERPARTY", "UPDATEDATE",
    ),
}


def _identity_samples(value: object, dataset: object) -> list[dict[str, object]]:
    fields = _IDENTITY_FIELDS.get(dataset, ()) if isinstance(dataset, str) else ()
    samples: list[dict[str, object]] = []
    for row in (value[:5] if isinstance(value, list) else []):
        if not isinstance(row, dict):
            continue
        projected: dict[str, object] = {}
        for field in fields:
            if field not in row:
                continue
            observed = row[field]
            if isinstance(observed, str):
                projected[field] = observed[:160]
            elif observed is None or _number(observed) is not None:
                projected[field] = observed
        samples.append(projected)
    return samples


def _field_statistics(value: object) -> list[dict[str, object]]:
    fields: list[dict[str, object]] = []
    for field in (value[:32] if isinstance(value, list) else []):
        if not isinstance(field, dict):
            continue
        projected: dict[str, object] = {"field": _text(field.get("field"), 80)}
        for name in ("present_rows", "non_null_rows", "finite_numeric_rows"):
            projected[name] = SafeNonnegativeInt(field.get(name))
        fields.append(projected)
    return fields


def _structured_field_details(source: Mapping[str, object], payload: dict[str, object]) -> None:
    sources = source.get("sources")
    samples: list[dict[str, object]] = []
    for sample in (sources[:4] if isinstance(sources, list) else []):
        if not isinstance(sample, dict):
            continue
        projected: dict[str, object] = {
            name: _text(sample.get(name))
            for name in ("source", "dataset", "report", "status", "failure", "reason", "response_sha256")
        }
        for name in ("requests", "sample_limit", "rows", "reported_count", "reported_pages", "outside_requested_window", "response_bytes"):
            projected[name] = SafeNonnegativeInt(sample.get(name))
        projected["seconds"] = _number(sample.get("seconds"))
        projected["unresolved_semantics"] = SafeStringList(sample.get("unresolved_semantics"), limit=16)
        projected["missing_required_fields"] = SafeStringList(sample.get("missing_required_fields"), limit=32)
        returned = sample.get("returned_fields")
        projected["returned_fields"] = [
            field for field in (returned[:256] if isinstance(returned, list) else [])
            if isinstance(field, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", field) is not None
        ]
        projected["fields"] = _field_statistics(sample.get("fields"))
        projected["identity_samples"] = _identity_samples(sample.get("identity_samples"), sample.get("dataset"))
        typed = sample.get("typed_fields")
        if isinstance(typed, dict):
            typed_status = typed.get("status")
            failure = typed.get("failure")
            observations = SafeNonnegativeInt(typed.get("observations"))
            reasons = ProjectMapping(typed.get("reasons"))
            projected["typed_fields"] = {
                "status": typed_status if typed_status in ("decoded", "rejected") else None,
                "failure": failure if failure in ("TypeError", "ValueError") else None,
                "observations": observations if observations is not None and observations <= 500 else None,
                "reasons": {
                    reason: count for reason in (
                        "event_identity_unverified", "point_in_time_unverified", "source_after_cutoff",
                    ) if (count := SafeNonnegativeInt(reasons.get(reason))) is not None and count <= 500
                },
                "production_adopted": False,
                "field_correctness_verified": False,
            }
        samples.append(projected)
    observation = source.get("observation_status", source.get("status"))
    payload["summary"] = {
        "runtime_configuration_sha256": _configuration_hash(source),
        "read_only": source.get("read_only") is True,
        "production_adopted": False,
        "field_correctness_verified": False,
        "full_window_coverage_verified": False,
        "observation_status": observation if observation in ("passed", "degraded") else None,
        "observed_at": _text(source.get("observed_at"), 64),
        "window_start": _text(source.get("window_start"), 10),
        "window_end": _text(source.get("window_end"), 10),
        "request_count_basis": _text(source.get("request_count_basis")),
        "schema_discovery_only": source.get("schema_discovery_only") is True,
        "elapsed_seconds": _number(source.get("elapsed_seconds")),
        "sources": samples,
    }


def _material_details(source: Mapping[str, object], payload: dict[str, object]) -> None:
    items = source.get("items")
    projected: list[dict[str, object]] = []
    for item in (items[:32] if isinstance(items, list) else []):
        if not isinstance(item, dict):
            continue
        url = item.get("source_url")
        row: dict[str, object] = {
            "source_key": _text(item.get("source_key"), 240),
            "code": _text(item.get("code"), 6),
            **{name: _text(item.get(name)) for name in (
                "title", "published_at", "record_hash", "body_hash", "material_failure", "announcement_number",
            )},
            **{name: item.get(name) is True for name in (
                "body_checked", "material_available", "current_receipt", "excerpt_truncated",
            )},
            "source_url": url[:1024] if isinstance(url, str) and url.startswith("https://static.cninfo.com.cn/finalpage/") else None,
            "referenced_announcement_numbers": SafeStringList(item.get("referenced_announcement_numbers"), limit=16),
            "header_issuer_codes": SafeStringList(item.get("header_issuer_codes"), limit=16),
        }
        excerpts = item.get("excerpts")
        row["excerpts"] = [
            {"line": SafeNonnegativeInt(excerpt.get("line")), "text": _text(excerpt.get("text"), 150)}
            for excerpt in (excerpts[:16] if isinstance(excerpts, list) else [])
            if isinstance(excerpt, dict)
        ]
        contracts = item.get("contracts")
        row["contracts"] = [
            _material_contract(contract)
            for contract in (contracts[:16] if isinstance(contracts, list) else [])
            if isinstance(contract, dict)
        ]
        row["contracts_truncated"] = isinstance(contracts, list) and len(contracts) > 16
        actions = item.get("share_actions")
        row["share_actions"] = [
            {**{name: _text(action.get(name)) for name in (
                "event_id", "event_type", "subject", "status", "repurchase_purpose",
            )}, "actual_pct": _number(action.get("actual_pct"))}
            for action in (actions[:16] if isinstance(actions, list) else []) if isinstance(action, dict)
        ]
        row["share_actions_truncated"] = isinstance(actions, list) and len(actions) > 16
        conclusions = item.get("field_conclusions")
        row["field_conclusions"] = _field_conclusions(conclusions)
        row["field_conclusions_truncated"] = isinstance(conclusions, list) and len(conclusions) > 32
        unverified = item.get("unverified_fields")
        row["unverified_fields"] = _field_conclusions(unverified)
        row["unverified_fields_truncated"] = item.get("unverified_fields_truncated") is True or (
            isinstance(unverified, list) and len(unverified) > 32
        )
        contract = item.get("reparsed_contract")
        if isinstance(contract, dict):
            row["reparsed_contract"] = {**_material_contract(contract), "published": False}
        reparsed = item.get("reparsed_share_action")
        if isinstance(reparsed, dict):
            row["reparsed_share_action"] = {
                **{name: _text(reparsed.get(name)) for name in ("event_type", "subject", "status", "repurchase_purpose")},
                "actual_pct": _number(reparsed.get("actual_pct")),
                "positive_level": SafeNonnegativeInt(reparsed.get("positive_level")),
                "published": False,
            }
        projected.append(row)
    observation = source.get("observation_status", source.get("status"))
    missing = source.get("missing_source_keys")
    payload["summary"] = {
        "read_only": source.get("read_only") is True,
        "stable_read": source.get("stable_read") is True,
        "field_correctness_verified": False,
        "observation_status": observation if observation in ("observed", "degraded") else None,
        "missing_source_keys": [value[:240] for value in (missing[:6] if isinstance(missing, list) else []) if isinstance(value, str)],
        "items": projected,
        "items_truncated": isinstance(items, list) and len(items) > 32,
    }


def _material_contract(contract: Mapping[str, object]) -> dict[str, object]:
    return {
        **{name: _text(contract.get(name)) for name in (
            "event_id", "subject", "status", "revenue_report_date", "revenue_disclosed_at", "amount_basis",
        )},
        **{name: _number(contract.get(name)) for name in (
            "contract_amount", "annual_revenue", "contract_revenue_pct",
        )},
        "binding_confirmed": contract.get("binding_confirmed") is True,
        "positive_level": SafeNonnegativeInt(contract.get("positive_level")),
    }


def _field_conclusions(value: object, limit: int = 32) -> list[dict[str, object]]:
    projected: list[dict[str, object]] = []
    for result in (value[:limit] if isinstance(value, list) else []):
        if not isinstance(result, dict):
            continue
        raw_value = result.get("value")
        proofs = result.get("evidence")
        projected.append({
            **{name: _text(result.get(name)) for name in ("event_id", "field", "state", "reason")},
            "code": _text(result.get("code"), 6),
            "value": _text(raw_value) if isinstance(raw_value, str) else _number(raw_value),
            "applicable": result.get("applicable") is True,
            "evidence": [{
                **{name: _text(proof.get(name)) for name in (
                    "source", "source_key", "record_hash", "body_hash", "rule_id", "unit", "subject", "published_at",
                )},
                **{name: SafeNonnegativeInt(proof.get(name)) for name in ("start_line", "end_line")},
            } for proof in (proofs[:4] if isinstance(proofs, list) else []) if isinstance(proof, dict)],
        })
    return projected


def _research_source_compare_details(
    _result: DiagnosticResult,
    source: Mapping[str, object],
    payload: dict[str, object],
) -> None:
    official = source.get("official_financial_probe")
    if isinstance(official, Mapping):
        payload["official_financial_probe"] = ProjectCompanyOfficialFinancialAudit(official)
        return
    revision = source.get("financial_revision_probe")
    if isinstance(revision, Mapping):
        fields = (
            "eps", "bps", "revenue_growth_pct", "profit_growth_pct", "core_profit_growth_pct",
            "roe_pct", "parent_profit", "core_profit",
        )
        report_fields = (
            "SECUCODE", "REPORT_DATE", "NOTICE_DATE", "UPDATE_DATE", "EPSJB", "BPS", "TOTALOPERATEREVETZ",
            "PARENTNETPROFITTZ", "KCFJCXSYJLRTZ", "ROEJQ", "PARENTNETPROFIT", "KCFJCXSYJLR",
        )
        record = revision.get("record")
        record = record if isinstance(record, Mapping) else {}
        values = record.get("values")
        values = values if isinstance(values, Mapping) else {}
        queries = revision.get("queries")
        queries = queries if isinstance(queries, list) else []
        projected_queries: list[dict[str, object]] = []
        for query in queries[:3]:
            if not isinstance(query, Mapping):
                continue
            digest = query.get("response_sha256")
            projected_queries.append({
                "query": query.get("query") if query.get("query") in (
                    "baseline", "include_update_boundary", "exclude_update_boundary", "production_fields_control",
                ) else None,
                "status": query.get("status") if query.get("status") in ("decoded", "empty", "failed") else None,
                "provider_code": query.get("provider_code") if type(query.get("provider_code")) is int else None,
                "provider_success": query.get("provider_success") if type(query.get("provider_success")) is bool else None,
                "received_at": _text(query.get("received_at"), 64),
                "response_sha256": digest if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) else None,
                **{name: SafeNonnegativeInt(query.get(name)) for name in (
                    "http_status", "response_bytes", "reported_count", "reported_pages", "row_count",
                )},
                "missing_fields": [name for name in SafeStringList(query.get("missing_fields"), limit=12) if name in report_fields],
            })
        allowed_failures = (
                "company_financial_revision_deadline_exhausted", "company_financial_revision_clock_invalid",
                "company_financial_revision_response_size_invalid", "company_financial_revision_provider_rejected",
                "company_financial_revision_result_invalid", "company_financial_revision_identity_count_invalid",
                "company_financial_revision_row_invalid", "company_financial_revision_required_fields_missing",
                "company_financial_revision_record_absent", "company_financial_revision_inclusion_not_reproduced",
                "company_financial_revision_filter_ignored", "company_financial_revision_identity_mismatch",
                "company_financial_revision_time_invalid", "company_financial_revision_number_invalid",
                "OSError", "RuntimeError", "TypeError", "ValueError", "CompanySourceFailure", "HttpError",
                "HttpTimeout", "HttpConnectionError", "HttpCancelled", "HttpResponseTooLarge", "JSONDecodeError",
        )
        failure = revision.get("failure")
        failure = failure if isinstance(failure, str) and failure in allowed_failures else None
        payload["summary"] = {"runtime_configuration_sha256": _configuration_hash(source)}
        projected: dict[str, object] = {
            **{name: _text(revision.get(name), 64) for name in (
                "source", "report", "code", "report_date", "observed_at",
            )},
            "read_only": True, "production_adopted": False, "field_correctness_verified": False,
            "full_window_coverage_verified": False, "sort_verified": False,
            "issuer_revision_verified": False, "original_disclosure_verified": False,
            "update_filter_verified": revision.get("update_filter_verified") is True,
            "support_status": revision.get("support_status") if revision.get("support_status") in (
                "sample_verified", "unverified", "not_verified",
            ) else None,
            "request_limit": 3, "max_response_bytes": 1024 * 1024,
            "request_count": SafeNonnegativeInt(revision.get("request_count")),
            "elapsed_seconds": _number(revision.get("elapsed_seconds")), "failure": failure,
            "queries": projected_queries,
        }
        payload["financial_revision_probe"] = projected
        if isinstance(revision.get("record"), Mapping):
            projected["record"] = {
                **{name: _text(record.get(name), 64) for name in ("identity", "report_date", "notice_at", "updated_at")},
                "values": {name: _number(values.get(name)) for name in fields},
                "missing_value_fields": [name for name in SafeStringList(record.get("missing_value_fields"), limit=8) if name in fields],
            }
        control = revision.get("production_fields_control")
        if isinstance(control, Mapping):
            control_values = control.get("values")
            control_values = control_values if isinstance(control_values, Mapping) else {}
            control_failure = control.get("failure")
            projected["production_fields_control"] = {
                "status": control.get("status") if control.get("status") in ("decoded", "empty", "failed") else None,
                "update_date_requested": False, "revision_verified": False,
                "failure": control_failure if isinstance(control_failure, str) and control_failure in allowed_failures else None,
                **{name: _text(control.get(name), 64) for name in ("identity", "report_date", "notice_at")},
                "values": {name: _number(control_values.get(name)) for name in fields},
                "missing_value_fields": [name for name in SafeStringList(control.get("missing_value_fields"), limit=8) if name in fields],
            }
        return
    payload["market_pagination"] = ProjectMapping(source.get("market_pagination"))
    payload["structured_samples"] = source.get("structured_samples", [])
    sources = source.get("sources")
    source_values = sources if isinstance(sources, list) else []
    projected_sources: list[dict[str, object]] = []
    for item in source_values[:4]:
        if not isinstance(item, dict):
            continue
        samples = item.get("samples")
        sample_values = samples if isinstance(samples, list) else []
        missing_field_counts: dict[str, int] = {}
        component_error_counts: dict[str, int] = {}
        for sample in sample_values[:50]:
            if not isinstance(sample, dict):
                continue
            for field in SafeStringList(sample.get("missing_fields"), limit=12):
                missing_field_counts[field] = missing_field_counts.get(field, 0) + 1
            for error in SafeStringList(sample.get("component_errors"), limit=12):
                component_error_counts[error] = component_error_counts.get(error, 0) + 1
        projected_sources.append(
            {
                "source": SafeText(item.get("source")),
                "tested": item.get("tested") is True,
                "excluded_by_budget": item.get("excluded_by_budget") is True,
                "physical_http_requests": SafeNonnegativeInt(item.get("physical_http_requests")),
                "sample_count": SafeNonnegativeInt(item.get("sample_count")),
                "field_coverage": ProjectMapping(item.get("field_coverage")),
                "interface_status": SafeText(item.get("interface_status")),
                "production_role": SafeText(item.get("production_role")),
                "regulatory_history_complete": item.get("regulatory_history_complete") is True,
                "exchange_cross_check_status": SafeText(item.get("exchange_cross_check_status")),
                "missing_field_counts": missing_field_counts,
                "component_error_counts": component_error_counts,
            }
        )
    payload["summary"] = {
        "runtime_configuration_sha256": _configuration_hash(source),
        "required_fields": SafeStringList(source.get("required_fields"), limit=12),
        "sources": projected_sources,
        "selected_route": ProjectMapping(source.get("selected_route")),
        "budget": ProjectMapping(source.get("budget")),
    }


_DETAILS: Mapping[str, Callable[[DiagnosticResult, Mapping[str, object], dict[str, object]], None]] = {
    "company_resume": ProjectCompanyResume,
    "research_source_compare": _research_source_compare_details,
}


def ProjectDetails(result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    if source.get("probe") == "structured_page":
        _structured_page_details(source, payload)
        return
    if source.get("probe") == "st_name_history":
        _st_source_details(source, payload)
        return
    if source.get("schema_version") == "company-field-replay":
        codes = source.get("codes")
        keys = source.get("source_keys")
        payload["summary"] = {
            **{name: source.get(name) is True for name in (
                "read_only_source", "isolated_copy", "collection_backup_generation_stable",
                "original_valid_receipts_unchanged", "second_fields_and_publication_unchanged",
                "second_zero_publication_text_reads", "source_input_unchanged", "fields_truncated", "deadline_exceeded",
                "collection_source_files_unchanged", "facts_source_files_unchanged", "source_empty_wal_created",
                "facts_backup_generation_stable", "recovered_required_text",
                "original_risks_and_evidence_preserved", "unselected_facts_unchanged",
                "round_1_read_materials_truncated", "round_2_read_materials_truncated",
            )},
            **{name: _migration_count(source.get(name)) for name in (
                "network_requests", "source_data_writes", "required_texts", "copied_texts", "missing_texts", "initial_missing_texts",
                "compressed_text_bytes", "expanded_text_bytes", "round_1_initialize_text_reads",
                "round_1_publication_text_reads", "round_1_total_text_reads", "round_2_initialize_text_reads",
                "round_2_publication_text_reads", "round_2_total_text_reads", "initial_field_attempted",
                "first_field_attempted", "second_field_attempted", "second_new_field_attempted", "fields",
                "final_required_texts", "unselected_stocks", "round_1_publication_pending_codes", "round_2_publication_pending_codes",
            )},
            **{name: _text(source.get(name), 110) for name in (
                "first_publication_hash", "second_publication_hash", "failure_category", "failure_code", "observed_at",
            )},
            "source_input_comparison": "database_wal_receipt_sha256_exact",
            "codes": [code for code in (codes[:6] if isinstance(codes, (tuple, list)) else [])
                      if isinstance(code, str) and len(code) == 6 and code.isascii() and code.isdigit()],
            "source_keys": [key[:240] for key in (keys[:6] if isinstance(keys, (tuple, list)) else []) if isinstance(key, str)],
            "field_items": _field_conclusions(source.get("field_items"), 100),
            **{f"round_{number}_risk_comparison": ProjectCompanyRiskComparisonDetails(
                source.get(f"round_{number}_risk_comparison"),
            ) for number in (1, 2)},
            **{f"round_{number}_publication_material_reads": _material_reads(
                source.get(f"round_{number}_publication_material_reads"),
            ) for number in (1, 2)},
            "field_states": _bounded_migration_counts(source.get("field_states"), (
                "verified", "present_unverified", "not_applicable", "pending_download", "pending_extraction",
                "pending_publication", "verification_required", "unsupported", "out_of_range", "not_disclosed",
            )),
            "production_authorized": False, "field_correctness_verified": False,
            "full_market_verified": False, "performance_verified": False,
            **{name: _digest_triplet(source.get(name)) for name in (
                "collection_source_files_sha256_before", "collection_source_files_sha256_after",
            )},
        }
        return
    if source.get("schema_version") == "company-field-material":
        _material_details(source, payload)
        return
    if source.get("schema_version") == "company-annual-income-source":
        _annual_income_details(source, payload)
        return
    if source.get("schema_version") == "company-structured-field-sampling":
        _structured_field_details(source, payload)
        return
    _DETAILS.get(result.name, ProjectSummary)(result, source, payload)


def _bounded_migration_counts(value: object, names: tuple[str, ...]) -> dict[str, int]:
    fields = ProjectMapping(value)
    return {name: count for name in names
            if (count := _migration_count(fields.get(name))) is not None}


def _material_reads(value: object) -> list[dict[str, object]]:
    projected: list[dict[str, object]] = []
    for row in value[:10] if isinstance(value, list) else []:
        if not isinstance(row, Mapping):
            continue
        digest = row.get("body_hash")
        count = _migration_count(row.get("reads"))
        if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) and count is not None:
            projected.append({"body_hash": digest, "reads": count})
    return projected


def _migration_count(value: object) -> int | None:
    count = SafeNonnegativeInt(value)
    return count if count is not None and count <= 1_000_000_000 else None


def _digest_triplet(value: object) -> dict[str, str | None]:
    values = value if isinstance(value, (tuple, list)) and len(value) == 3 else (None, None, None)
    return {name: digest if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) else None
            for name, digest in zip(("database", "wal", "receipt"), values, strict=True)}
