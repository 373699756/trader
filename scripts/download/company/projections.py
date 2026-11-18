from __future__ import annotations

from collections.abc import Mapping

from scripts.core.contracts import DiagnosticResult
from scripts.core.projection_values import ProjectMapping

_CONSUMER_READ_FIELDS = (
    "checked", "http_checked", "stocks", "fields", "facts_equal", "fields_equal",
    "publication_equal", "document_links", "document_links_equal", "consistent", "failure",
)
_COMPANY_HTTP_FIELDS = (
    "scope", "passed", "complete", "expected_publication_hash", "loaded_identity", "codes", "verified_links",
    "http_requests", "invalid_queries_rejected", "links_digest", "handler_file_attempts", "handler_sql_attempts",
    "handler_network_attempts", "external_network_attempts", "business_write_attempts", "source_unchanged",
    "source_payload_sha256_before", "source_payload_sha256_after", "server_stopped", "resources_stopped",
    "budget_exhausted", "failure", "source_admission_verified", "production_enabled", "full_sync_verified", "performance_accepted",
)

def ProjectCompanyRiskComparisonDetails(value: object) -> dict[str, object]:
    comparison = ProjectMapping(value)
    projected = {key: comparison.get(key) for key in (
        "read_only", "complete", "stable_read", "baseline_sha256", "baseline_risks", "new_risks",
        "exactly_preserved", "publication_hash", "baseline_publication_hash", "all_old_risks_and_evidence_preserved",
        "missing_means_resolved", "field_correctness_verified",
    )}
    for name, keys in (
        ("missing", ("code", "event_key")),
        ("changed_requires_review", ("code", "event_key", "changed_fields", "original_evidence_retained", "evidence_identity_confirmed")),
    ):
        rows = comparison.get(name)
        projected[name] = [
            {key: row.get(key) for key in keys}
            for row in (rows[:10_000] if isinstance(rows, list) else []) if isinstance(row, dict)
        ]
    return projected


def ProjectCompanyResume(
    _result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]
) -> None:
    from scripts.download.company.company_field_audit import ProjectCompanyFieldAudit

    summary = ProjectMapping(source.get("summary"))
    selected = {
        key: summary.get(key)
        for key in (
            "isolated_backup",
            "implementation_digest",
            "selected_slices",
            "completed_slices",
            "covered_slices",
            "saved_pages_replayed",
            "document_sample_limit",
            "directory_only",
        )
    }
    consumer_audit = ProjectMapping(summary.get("consumer_audit"))
    field_proof = ProjectMapping(summary.get("field_proof_audit"))
    recovery = ProjectMapping(summary.get("field_proof_recovery"))
    if recovery:
        selected["field_proof_recovery"] = {
            **{key: recovery.get(key) for key in (
                "scope", "read_only_source", "isolated_copy", "complete", "observed_at",
                "source_data_writes", "network_requests", "network_request_count_complete", "source_publication_hash", "field_rule_hash",
                "planned_objects", "attempted_objects", "exact_text_objects", "recovered_objects",
                "target_fields", "reproved_fields", "unreproved_fields", "source_unchanged", "budget_exhausted",
                "production_adopted", "field_correctness_verified", "full_market_verified", "service_http_checked",
            )},
            **{name: [
                {key: row.get(key) for key in fields}
                for row in rows[:limit] if isinstance(row, Mapping)
            ] if isinstance(rows := recovery.get(name), list) else [] for name, fields, limit in (
                ("material_observations", ("source_key", "code", "status", "requests", "failure", "same_pdf", "same_body", "recovered"), 52),
                ("field_results", ("code", "event_id", "field", "state", "reason", "reproved"), 68),
            )},
        }
    if field_proof:
        rows = field_proof.get("items")
        failures = ProjectMapping(field_proof.get("material_failures"))
        basis = ProjectMapping(field_proof.get("unreproved_basis"))
        selected["field_proof_audit"] = {
            **{key: field_proof.get(key) for key in (
                "read_only", "scope", "observed_at", "complete", "stable_read", "budget_exhausted",
                "publication_hash", "extraction_hash", "current_extraction_hash", "field_rule_hash", "target_fields", "checked_fields",
                "reproved_fields", "unreproved_fields", "checked_stocks", "material_reads", "items_truncated",
                "network_requests", "source_data_writes", "production_adopted", "field_correctness_verified",
                "full_market_verified", "http_checked",
            )},
            "material_failures": {key: value for key, value in failures.items() if key in {
                "OSError", "FileNotFoundError", "PermissionError", "ValueError", "EOFError", "BadGzipFile", "UnicodeDecodeError",
            } and type(value) is int and value >= 0},
            "unreproved_basis": {key: value for key, value in basis.items() if key in {
                "source_missing_or_ambiguous", "material_identity_changed", "current_valid_receipt_missing",
                "original_body_released", "released_text_present", "original_body_missing", "current_rule_not_reproduced",
            } and type(value) is int and value >= 0},
            "items": [{key: row.get(key) for key in ("code", "event_id", "field", "state", "reason", "basis")}
                      for row in rows[:100] if isinstance(row, Mapping)] if isinstance(rows, list) else [],
        }
    if consumer_audit:
        read = ProjectMapping(consumer_audit.get("consumer_read"))
        selected["consumer_audit"] = {
            **{key: consumer_audit.get(key) for key in (
                "read_only", "scope", "observed_at", "complete", "budget_exhausted", "stable_read",
                "publication_hash", "extraction_hash", "published_at", "collection_checked", "recovery_planned",
                "field_correctness_verified", "full_market_verified", "http_checked",
            )},
            "consumer_read": {key: read.get(key) for key in _CONSUMER_READ_FIELDS},
            **({"http_audit": {key: http.get(key) for key in _COMPANY_HTTP_FIELDS}}
               if (http := ProjectMapping(consumer_audit.get("http_audit"))) else {}),
        }
    runs = summary.get("runs")
    document_download = ProjectMapping(summary.get("document_download_audit"))
    if document_download:
        observations = document_download.get("observations")
        encrypted = document_download.get("encrypted_observations")
        selected["document_download_audit"] = {
            "read_only": document_download.get("read_only"),
            "active_writes": document_download.get("active_writes"),
            "pending_records": document_download.get("pending_records"),
            "sample_size": document_download.get("sample_size"),
            "complete": document_download.get("complete"),
            "production_passed": document_download.get("production_passed"),
            "encrypted_observations": [
                {key: row.get(key) for key in (
                    "disclosure_id", "read_only", "status_code", "same_pdf", "metadata_exit_code", "encrypted",
                    "pages", "password_required", "text_exit_code", "text_bytes", "failure",
                )}
                for row in (encrypted[:3] if isinstance(encrypted, list) else []) if isinstance(row, dict)
            ],
            "observations": [
                {key: row.get(key) for key in (
                    "disclosure_id", "record", "variant", "timeout_seconds", "io_timeout_seconds", "passed", "is_pdf", "payload_hash", "failure", "cause",
                    "text_extracted", "characters",
                )} | {"attempts": [
                    {key: attempt.get(key) for key in (
                        "seconds", "response_bytes", "status_code", "failure", "phase",
                    )}
                    for attempt in (row.get("attempts", [])[:1] if isinstance(row.get("attempts"), list) else [])
                    if isinstance(attempt, dict)
                ]}
                for row in (observations[:10] if isinstance(observations, list) else [])
                if isinstance(row, dict)
            ],
        }
    ocr = ProjectMapping(summary.get("ocr_audit"))
    if ocr:
        selected["ocr_audit"] = {key: ocr.get(key) for key in (
            "sample_size", "active_writes", "policy", "passed", "characters", "confidence", "same_pdf",
            "failure", "diagnostic", "seconds", "reason", "samples", "recovered_documents",
        )}
    pipeline = ProjectMapping(summary.get("pipeline_audit"))
    if pipeline:
        pipeline_runs = pipeline.get("runs")
        selected["pipeline_audit"] = {
            **{key: pipeline.get(key) for key in (
                "isolated_sample", "source_admission_verified", "production_enabled", "performance_accepted",
                "full_sync_verified", "equivalent_facts", "equivalent_documents",
            )},
            "runs": [
                {
                    key: row.get(key)
                    for key in (
                        "workers", "pacing", "sample_documents", "seconds", "failures", "facts_digest",
                        "extracted_documents", "documents_digest", "restart_unchanged",
                    )
                } | {
                    "directory": {key: ProjectMapping(row.get("directory")).get(key)
                                  for key in ("complete", "pages", "rows", "failure", "digest")}
                                  if row.get("directory") is not None else None,
                    "operations": [{
                        **{key: entry.get(key) for key in (
                            "operation", "calls", "wall_seconds", "thread_cpu_seconds", "inflight_calls",
                            "oldest_inflight_seconds", "inflight_duration_complete", "untracked_inflight_calls",
                        )},
                        "failures": [{key: failure.get(key) for key in ("category", "calls")}
                                     for failure in operation_failures[:32] if isinstance(failure, Mapping)]
                                    if isinstance(operation_failures := entry.get("failures"), list) else [],
                    } for entry in operations[:64] if isinstance(entry, Mapping)]
                    if isinstance(operations := row.get("operations"), list) else [],
                }
                for row in (pipeline_runs[:2] if isinstance(pipeline_runs, list) else [])
                if isinstance(row, dict)
            ],
        }
    database = ProjectMapping(summary.get("database"))
    compaction = ProjectMapping(summary.get("compaction"))
    if compaction:
        selected["compaction"] = {
            key: compaction.get(key)
            for key in (
                "isolated_copy",
                "before_bytes",
                "after_bytes",
                "removed_completed_members",
                "retained_data_equal",
            )
        }
    if database:
        projected_database = {
            key: database.get(key)
            for key in (
                "read_only",
                "complete",
                "budget_exhausted",
                "failure_category",
                "failure_code",
                "incompatible_stores",
                "field_correctness_verified",
                "full_market_verified",
                "pdf_archived",
                "database_bytes",
                "free_bytes",
                "announcement_records",
                "extracted_bodies",
                "retained_bodies",
                "pending_ranges",
                "pending_documents",
                "recorded_retry_cooling",
                "recorded_retry_ready",
                "deterministic_failures",
                "retry_payload_hashes",
                "ocr_diagnostics",
                "retained_failed_pdfs",
                "failure_history",
                "failure_receipts",
                "retry_inventory",
                "file_inventory_is_atomic",
            )
        }
        failures = ProjectMapping(database.get("failure_objects"))
        if failures:
            projected_failures = {key: failures.get(key) for key in (
                "available", "stable_read", "events", "unidentified_events", "population_stocks", "population_hash",
                "publication_hash", "extraction_hash", "configuration_hash", "object_count", "states",
            )}
            objects = failures.get("objects")
            projected_failures["objects"] = [
                {key: row.get(key) for key in ("source_key", "record_hash", "events", "state")}
                for row in (objects[:50] if isinstance(objects, list) else []) if isinstance(row, dict)
            ]
            projected_failures["truncated"] = isinstance(objects, list) and len(objects) > 50
            projected_database["failure_objects"] = projected_failures
        tables = database.get("table_bytes")
        projected_database["table_bytes"] = [
            {"name": row.get("name"), "bytes": row.get("bytes")}
            for row in (tables[:64] if isinstance(tables, list) else [])
            if isinstance(row, dict)
        ]
        formal = ProjectMapping(database.get("formal"))
        projected_formal = {key: formal.get(key) for key in ("available", "database_bytes", "stocks")}
        formal_tables = formal.get("table_bytes")
        projected_formal["table_bytes"] = [
            {"name": row.get("name"), "bytes": row.get("bytes")}
            for row in (formal_tables[:64] if isinstance(formal_tables, list) else [])
            if isinstance(row, dict)
        ]
        projected_database["formal"] = projected_formal
        publication = ProjectMapping(database.get("publication"))
        projected_publication = {
            key: publication.get(key)
            for key in (
                "available", "observed_at", "scope", "publication_hash", "extraction_hash", "published_at",
                "population_stocks", "published_stocks", "unpublished_stocks", "dirty_stocks", "stable_read",
                "outside_population_dirty_stocks",
            )
        }
        for name, keys in (
            ("coverage", ("announcement_start_min", "announcement_start_max", "announcement_end_min",
                          "announcement_end_max", "unlock_end_min", "unlock_end_max", "unlock_required_end",
                          "unlock_incomplete_stocks", "pending_document_references")),
            ("ongoing_risks", ("events", "stocks", "older_than_year", "missing_evidence_events")),
            ("receipts", ("checked", "awaiting_publication_documents", "released_without_receipts")),
            ("consumer_read", _CONSUMER_READ_FIELDS),
        ):
            fields = ProjectMapping(publication.get(name))
            projected_publication[name] = {key: fields.get(key) for key in keys}
        for name, keys, limit in (
            ("component_states", ("component", "state", "stocks"), 12),
            ("reviews", ("scope", "reason", "records", "stocks"), 125),
        ):
            rows = publication.get(name)
            projected_publication[name] = [
                {key: row.get(key) for key in keys}
                for row in (rows[:limit] if isinstance(rows, list) else [])
                if isinstance(row, dict)
            ]
        findings = publication.get("findings")
        projected_publication["findings"] = [
            item for item in (findings[:16] if isinstance(findings, list) else [])
            if isinstance(item, str) and item in {
                "company_formal_unavailable", "company_publication_missing", "company_population_empty",
                "company_population_unpublished", "company_publication_dirty", "company_components_incomplete",
                "company_unlock_180_days_incomplete", "company_reviews_pending", "company_risk_evidence_missing",
                "company_released_body_receipt_missing", "company_audit_concurrent_change",
                "company_field_integrity_failed", "company_applicable_fields_missing",
            }
        ]
        field_audit = ProjectMapping(publication.get("field_audit"))
        if field_audit:
            projected_publication["field_audit"] = ProjectCompanyFieldAudit(field_audit)
        projected_database["publication"] = projected_publication
        risk_comparison = ProjectMapping(database.get("risk_comparison"))
        if risk_comparison:
            projected_database["risk_comparison"] = ProjectCompanyRiskComparisonDetails(risk_comparison)
        for name, count_key in (("partition_failures", "ranges"), ("document_failures", "documents")):
            rows = database.get(name)
            projected_database[name] = [
                {"failure": row.get("failure"), count_key: row.get(count_key)}
                for row in (rows[:64] if isinstance(rows, list) else [])
                if isinstance(row, dict)
            ]
        classes = database.get("document_failure_classes")
        recent_failures = database.get("recent_document_failures")
        projected_database["recent_document_failures"] = [
            {key: row.get(key) for key in ("disclosure_id", "failure", "attempted_at", "retry_at", "payload_hash")}
            for row in (recent_failures[:10] if isinstance(recent_failures, list) else [])
            if isinstance(row, dict)
        ]
        projected_database["document_failure_classes"] = [
            {"class": row.get("class"), "documents": row.get("documents")}
            for row in (classes[:16] if isinstance(classes, list) else [])
            if isinstance(row, dict)
        ]
        extractions = database.get("document_extractions")
        projected_database["document_extractions"] = [
            {"method": row.get("method"), "documents": row.get("documents")}
            for row in (extractions[:4] if isinstance(extractions, list) else [])
            if isinstance(row, dict)
        ]
        selected["database"] = projected_database
    selected["runs"] = (
        [
            {
                key: run.get(key)
                for key in (
                    "interrupted",
                    "pages",
                    "document_attempts",
                    "pending_slices",
                    "sampled_pending_documents",
                    "failures",
                    "elapsed_seconds",
                )
            }
            for run in runs[:3]
            if isinstance(run, dict)
        ]
        if isinstance(runs, list)
        else []
    )
    audit = ProjectMapping(summary.get("query_audit"))
    if audit:
        audit_pages = audit.get("pages")
        audit_totals = audit.get("totals")
        pagination = audit.get("pagination")
        selected["query_audit"] = {
            "pagination": [
                {
                    key: row.get(key)
                    for key in ("market", "date", "page_size", "total", "page_rows", "equivalent", "failure", "seconds")
                }
                for row in (pagination[:9] if isinstance(pagination, list) else [])
                if isinstance(row, dict)
            ],
            "category_filter": audit.get("category_filter"),
            "read_only": audit.get("read_only"),
            "pages": [
                {
                    key: row.get(key)
                    for key in (
                        "market",
                        "start",
                        "end",
                        "plate",
                        "page",
                        "members",
                        "total",
                        "rows",
                        "outside_codes",
                        "same_issuer",
                        "b_share_rows",
                        "code_prefixes",
                        "failure",
                        "order",
                    )
                }
                for row in (audit_pages[:192] if isinstance(audit_pages, list) else [])
                if isinstance(row, dict)
            ],
            "totals": [
                {
                    key: row.get(key)
                    for key in (
                        "market",
                        "start",
                        "parent_total",
                        "board_total",
                        "group_total",
                        "sample_complete",
                    )
                }
                for row in (audit_totals[:3] if isinstance(audit_totals, list) else [])
                if isinstance(row, dict)
            ],
        }
    payload["summary"] = selected
