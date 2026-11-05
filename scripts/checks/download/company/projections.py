from __future__ import annotations

from collections.abc import Mapping

from scripts.checks.contracts import DiagnosticResult
from scripts.checks.projection_values import ProjectMapping

def ProjectCompanyResume(
    _result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]
) -> None:
    from scripts.checks.download.company.company_field_audit import ProjectCompanyFieldAudit

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
    runs = summary.get("runs")
    document_download = ProjectMapping(summary.get("document_download_audit"))
    if document_download:
        observations = document_download.get("observations")
        encrypted = document_download.get("encrypted_observations")
        selected["document_download_audit"] = {
            "read_only": document_download.get("read_only"),
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
                    "disclosure_id", "variant", "timeout_seconds", "io_timeout_seconds", "passed", "is_pdf", "payload_hash", "failure", "cause",
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
            "isolated_sample": pipeline.get("isolated_sample"),
            "equivalent_facts": pipeline.get("equivalent_facts"),
            "equivalent_documents": pipeline.get("equivalent_documents"),
            "equivalent_payloads": pipeline.get("equivalent_payloads"),
            "runs": [
                {
                    key: row.get(key)
                    for key in (
                        "workers",
                        "sample_documents",
                        "attempts",
                        "pending_documents",
                        "published_codes",
                        "seconds",
                        "first_publication_seconds",
                        "resume_changed_rows",
                        "resume_pending_documents",
                        "resume_attempts",
                        "failures",
                        "facts_digest",
                        "extracted_documents",
                        "pending_ocr_documents",
                        "unextractable_documents",
                        "documents_digest",
                        "payloads_digest",
                    )
                } | {
                    name: [
                        {label: entry.get(label), "documents": entry.get("documents")}
                        for entry in (row.get(name, [])[:64] if isinstance(row.get(name, []), list) else [])
                        if isinstance(entry, dict)
                    ]
                    for name, label in (("document_failures", "failure"), ("document_extractions", "method"))
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
