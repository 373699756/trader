"""Read-only field population, source agreement and saved query-union diagnostics."""

from __future__ import annotations

from scripts.core.paths import PROJECT_ROOT as TOOL_ROOT

from pathlib import Path
from collections.abc import Mapping
from collections import Counter
from datetime import date, datetime

from trader.infra.sqlite_connection import SqliteConnection
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.download.domain.company_events import CompanyEventPolicy, ContractMeasurements, IncreaseMeasurements, RepurchaseMeasurements
from trader.download.domain.company_event_extraction import ExtractCompanyLinkCandidate
from trader.download.domain.company_evidence import CompanyDocument, Disclosure
from trader.download.domain.company_announcement_numbers import ExtractCompanyAnnouncementNumbers
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_protocol import EVIDENCE_SOURCES
from trader.download.infra.company_field_recovery import ReadCompanyFieldRecovery
from trader.download.infra.company_field_recovery import ReadRequiredCompanyMaterialDependencies
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults
from trader.download.infra.download_settings import LoadCompanySyncConfiguration


def InspectCompanyFieldMaterial(
    raw_path: Path, facts_path: Path, source_keys: tuple[str, ...], *, sqlite_policy: SqliteIoPolicy,
    observed_at: datetime, event_policy: CompanyEventPolicy,
) -> dict[str, object]:
    """Project bounded local excerpts and contract facts for source-value annotation.

    The document owner verifies text hashes. This inspection never fetches,
    repairs or certifies field recall; unreadable material remains explicit.
    """
    if not 1 <= len(source_keys) <= 6 or len(set(source_keys)) != len(source_keys) or any(
        not key or len(key) > 240 or any(character.isspace() for character in key) for key in source_keys
    ):
        raise ValueError("company_material_keys_invalid")
    if observed_at.utcoffset() is None:
        raise ValueError("company_material_observation_requires_timezone")
    configuration = LoadCompanySyncConfiguration(TOOL_ROOT)
    documents = CompanyDocumentFiles(
        raw_path.parent / "text", max_text_characters=configuration.extended_document_limits.max_text_characters,
        max_bytes=configuration.temporary_text_capacity_bytes, reserve_bytes=configuration.temporary_disk_reserve_bytes,
    )
    items: list[dict[str, object]] = []
    found: set[str] = set()
    options = SqliteConnectionOptions(read_only=True, timeout_seconds=3)
    with SqliteSession(raw_path, options, sqlite_policy=sqlite_policy) as raw, SqliteSession(
        facts_path, options, sqlite_policy=sqlite_policy,
    ) as formal:
        before = (raw.run("PRAGMA data_version").require_one()[0], formal.run("PRAGMA data_version").require_one()[0])
        with SqliteTransaction(raw), SqliteTransaction(formal):
            publication = ReadCompanyPublicationIdentity(formal)
            placeholders = ",".join("?" for _ in source_keys)
            rows = raw.run(
                "SELECT d.source_key,d.title,d.published_at,d.record_hash,d.body_hash,d.body_checked,c.code,d.url "
                "FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
                f"WHERE d.source='cninfo' AND d.source_key IN ({placeholders}) ORDER BY d.source_key,c.code LIMIT 33", source_keys,
            ).fetchall()
            if len(rows) > 32:
                raise ValueError("company_material_members_limit")
            for row in rows:
                found.add(row[0])
                item: dict[str, object] = {
                    "source_key": row[0], "code": row[6], "title": row[1], "published_at": row[2],
                    "record_hash": row[3], "body_hash": row[4].hex() if isinstance(row[4], bytes) else None,
                    "body_checked": bool(row[5]), "material_available": False,
                    "source_url": row[7] if row[7].startswith("https://static.cninfo.com.cn/finalpage/") else None,
                }
                if row[4] is not None:
                    try:
                        text = documents.read(row[4])
                    except (OSError, ValueError) as exc:
                        item["material_failure"] = type(exc).__name__
                    else:
                        numbers = ExtractCompanyAnnouncementNumbers(text)
                        item.update({"announcement_number": numbers.own,
                                     "referenced_announcement_numbers": numbers.references,
                                     "header_issuer_codes": numbers.issuer_codes})
                        lines = text.splitlines()
                        positions: set[int] = set()
                        for position, line in enumerate(lines):
                            if any(marker in line for marker in (
                                "签订", "签署", "金额", "营业收入", "营业总收入", "生效", "首次回购", "累计",
                                "总股本", "回购用途", "减少注册资本", "用于员工", "用于转换", "股权激励",
                            )):
                                positions.update(range(max(0, position - 1), min(len(lines), position + 2)))
                        selected = sorted(positions)[:16]
                        item.update({"material_available": True, "excerpts": [
                            {"line": position + 1, "text": lines[position][:150]} for position in selected
                        ], "excerpt_truncated": len(selected) < len(lines) or any(len(lines[position]) > 150 for position in selected)})
                        candidate = ExtractCompanyLinkCandidate(CompanyDocument(
                            Disclosure("cninfo", row[0], (row[6],), row[1], datetime.fromisoformat(row[2]), row[7], row[3]),
                            text, True,
                        ), observed_at, event_policy)
                        positive = candidate.positive if candidate is not None else None
                        if positive is not None and isinstance(positive.measurements, (RepurchaseMeasurements, IncreaseMeasurements)):
                            item["reparsed_share_action"] = {
                                "event_type": positive.event_type.value, "subject": positive.subject_type.value,
                                "status": positive.status.value, "actual_pct": positive.measurements.actual_ratio_pct,
                                "repurchase_purpose": positive.measurements.purpose.value
                                if isinstance(positive.measurements, RepurchaseMeasurements) else None,
                                "positive_level": positive.positive_level,
                                "published": False,
                            }
                item["current_receipt"] = formal.run(
                    "SELECT 1 FROM company_input_receipt WHERE source='cninfo' AND source_key=? AND code=? "
                    "AND record_hash=? AND body_hash IS ? AND extraction_hash=? AND outcome='valid' LIMIT 1",
                    (row[0], row[6], row[3], row[4], publication.extraction_hash if publication is not None else b""),
                ).fetchone() is not None
                facts = ReadCompanyFactRows(formal, (row[6],))
                conclusions = ReadCompanyFieldResults(formal, (row[6],))
                item["field_conclusions"] = [{
                    "event_id": result.event_id, "field": result.field, "state": result.state,
                    "value": result.value.isoformat() if isinstance(result.value, date) else result.value,
                    "applicable": result.applicable, "reason": result.reason,
                    "evidence": [{
                        "source": proof.source, "source_key": proof.source_key,
                        "record_hash": proof.record_hash, "body_hash": proof.body_hash.hex(),
                        "rule_id": proof.rule_id, "start_line": proof.start_line, "end_line": proof.end_line,
                        "unit": proof.unit, "subject": proof.subject.value,
                        "published_at": proof.published_at.isoformat(),
                    } for proof in result.evidence[:4]],
                } for result in conclusions if any(
                    proof.source == "cninfo" and proof.source_key == row[0]
                    and proof.record_hash == row[3] and proof.body_hash == row[4]
                    and proof.published_at == datetime.fromisoformat(row[2])
                    for proof in result.evidence
                )]
                item["share_actions"] = [
                    {
                        "event_id": event.event_id, "event_type": event.event_type.value,
                        "subject": event.subject_type.value, "status": event.status.value,
                        "actual_pct": event.measurements.actual_ratio_pct,
                        "repurchase_purpose": event.measurements.purpose.value
                        if isinstance(event.measurements, RepurchaseMeasurements) else None,
                    }
                    for fact in facts for event in fact.positive_events
                    if row[0] in event.evidence_ids and isinstance(event.measurements, (RepurchaseMeasurements, IncreaseMeasurements))
                ]
                item["contracts"] = [
                    {
                        "event_id": event.event_id, "subject": event.subject_type.value, "status": event.status.value,
                        "contract_amount": event.measurements.contract_amount_cny,
                        "annual_revenue": event.measurements.reference_annual_revenue_cny,
                        "revenue_report_date": event.measurements.reference_report_date.isoformat()
                        if event.measurements.reference_report_date else None,
                        "binding_confirmed": event.measurements.binding_confirmed,
                        "amount_basis": event.measurements.amount_basis,
                    }
                    for fact in facts for event in fact.positive_events
                    if row[0] in event.evidence_ids and isinstance(event.measurements, ContractMeasurements)
                ]
                items.append(item)
        after = (raw.run("PRAGMA data_version").require_one()[0], formal.run("PRAGMA data_version").require_one()[0])
    stable = before == after
    return {
        "schema_version": "company-field-material", "read_only": True, "stable_read": stable,
        "status": "observed" if stable else "degraded", "field_correctness_verified": False,
        "missing_source_keys": sorted(set(source_keys) - found), "items": items,
    }


def AuditCompanyFields(
    formal: SqliteConnection, raw_path: Path, *, raw: SqliteConnection, observed_at: datetime
) -> dict[str, object]:
    formal.run("ATTACH DATABASE ? AS collected", (raw_path.resolve().as_uri() + "?mode=ro",))
    tables: list[dict[str, object]] = []
    invalid_types = 0
    nonfinite_values = 0
    field_count = 0
    for table in formal.run("SELECT name FROM main.sqlite_master WHERE type='table' AND name LIKE 'company_%' ORDER BY name"):
        name = '"' + table[0].replace('"', '""') + '"'
        columns = tuple(formal.run(f"PRAGMA main.table_info({name})"))
        expressions = ["COUNT(*)"]
        for column in columns:
            quoted = '"' + column[1].replace('"', '""') + '"'
            allowed = {"INTEGER": "'integer'", "REAL": "'real','integer'", "TEXT": "'text'", "BLOB": "'blob'"}[column[2]]
            expressions.extend((
                f"COUNT({quoted})",
                f"COALESCE(SUM(typeof({quoted}) NOT IN ('null',{allowed})),0)",
                f"COALESCE(SUM(typeof({quoted}) IN ('real','integer') AND ({quoted}>1.7976931348623157e308 OR {quoted}< -1.7976931348623157e308)),0)",
            ))
        counts = formal.run(f"SELECT {','.join(expressions)} FROM main.{name}").require_one()
        fields = []
        for offset, column in enumerate(columns):
            populated, invalid, nonfinite = counts[1 + offset * 3:4 + offset * 3]
            invalid_types += invalid
            nonfinite_values += nonfinite
            fields.append({"name": column[1], "populated": populated, "null": counts[0] - populated, "invalid_type": invalid, "nonfinite": nonfinite})
        field_count += len(columns)
        tables.append({"table": table[0], "rows": counts[0], "fields": fields})
    finance_columns = ("eps", "bps", "revenue_growth_pct", "profit_growth_pct", "core_profit_growth_pct", "roe_pct", "parent_profit", "core_profit")
    finance_match = " AND ".join(f"r.{name} IS f.{name}" for name in finance_columns)
    finance = formal.run(
        "SELECT COUNT(*),COALESCE(SUM(NOT EXISTS(SELECT 1 FROM collected.financials r "
        "WHERE r.code=f.code AND r.report_date=date(f.report_date*86400,'unixepoch') AND "
        + finance_match + ")),0) FROM main.company_facts f WHERE f.report_date IS NOT NULL"
    ).require_one()
    pledge = formal.run(
        "SELECT COUNT(*),COALESCE(SUM(EXISTS(SELECT 1 FROM collected.ratios r WHERE r.component='pledge' "
        "AND r.code=f.code AND r.as_of_date=date(f.pledge_date*86400,'unixepoch') AND r.ratio_pct IS f.pledge_pct)),0),"
        "COALESCE(SUM(f.pledge_pct=0 AND NOT EXISTS(SELECT 1 FROM collected.ratios r WHERE r.component='pledge' "
        "AND r.code=f.code AND r.as_of_date=date(f.pledge_date*86400,'unixepoch')) AND EXISTS("
        "SELECT 1 FROM collected.ratio_snapshots s WHERE s.component='pledge' "
        "AND s.start_date=date(f.pledge_date*86400,'unixepoch'))),0) "
        "FROM main.company_facts f WHERE f.pledge_date IS NOT NULL"
    ).require_one()
    unlock = formal.run(
        "SELECT COUNT(*),COALESCE(SUM(NOT EXISTS(SELECT 1 FROM collected.ratios r WHERE r.component='unlock' "
        "AND r.code=u.code AND r.as_of_date=date(u.unlock_date*86400,'unixepoch') AND r.ratio_pct IS u.ratio_pct)),0) "
        "FROM main.company_unlock u"
    ).require_one()
    source_cases = " ".join("WHEN ? THEN ?" for _ in EVIDENCE_SOURCES)
    source_parameters = tuple(value for name, identity in EVIDENCE_SOURCES.items() for value in (identity, name))
    missing_evidence = formal.run(
        "SELECT COUNT(*) FROM main.company_evidence_ref e WHERE NOT EXISTS(SELECT 1 FROM collected.disclosures d "
        "WHERE d.source_key=e.source_key AND d.source=CASE e.source " + source_cases + " END)", source_parameters,
    ).require_one()[0]
    risk_evidence = formal.run("SELECT COUNT(*) FROM main.company_risk_event e WHERE NOT EXISTS(SELECT 1 FROM main.company_risk_evidence r WHERE r.risk_id=e.id)").require_one()[0]
    positive_evidence = formal.run("SELECT COUNT(*) FROM main.company_positive_event e WHERE NOT EXISTS(SELECT 1 FROM main.company_positive_evidence r WHERE r.positive_id=e.id)").require_one()[0]
    applicable = formal.run(
        "SELECT COALESCE(SUM(event_type=3),0),COALESCE(SUM(event_type=3 AND contract_amount IS NULL),0),"
        "COALESCE(SUM(event_type=3 AND annual_revenue IS NULL),0),COALESCE(SUM(event_type=3 AND contract_revenue_pct IS NULL),0),"
        "COALESCE(SUM(event_type IN (1,2) AND status IN (2,3) AND actual_pct IS NULL),0) "
        "FROM main.company_positive_event WHERE status<>4"
    ).require_one()
    partitions = []
    for row in formal.run("SELECT market,start_date,end_date,failure,expected_rows,received_rows,next_page FROM collected.partitions WHERE is_complete=0 AND failure<>'' ORDER BY start_date DESC LIMIT 12"):
        key = tuple(row[:3])
        query = formal.run(
            "SELECT COUNT(*),COALESCE(SUM(is_complete),0),COALESCE(SUM(expected_rows),0) "
            "FROM collected.disclosure_queries WHERE market=? AND start_date=? AND end_date=?", key,
        ).require_one()
        union = formal.run(
            "SELECT COUNT(DISTINCT record_key),COUNT(DISTINCT source_key) FROM collected.disclosure_query_rows "
            "WHERE market=? AND start_date=? AND end_date=?", key,
        ).require_one()
        partitions.append({
            "market": row[0], "start": row[1], "end": row[2], "failure": row[3],
            "expected_rows": row[4], "received_rows": row[5], "next_page": row[6],
            "queries": query[0], "complete_queries": query[1], "query_total_sum": query[2],
            "unique_records": union[0], "unique_source_ids": union[1],
        })
    findings = []
    if any((invalid_types, nonfinite_values, finance[1], pledge[0] - pledge[1] - pledge[2], unlock[1], missing_evidence, risk_evidence, positive_evidence)):
        findings.append("company_field_integrity_failed")
    if any(applicable[1:]):
        findings.append("company_applicable_fields_missing")
    recovery = () if invalid_types or nonfinite_values else ReadCompanyFieldRecovery(
        formal, raw, tuple(row[0] for row in formal.run("SELECT code FROM company_facts ORDER BY code")),
        observed_at, raw_path.parent,
    )
    field_results = () if invalid_types or nonfinite_values else ReadCompanyFieldResults(
        formal, tuple(row[0] for row in formal.run("SELECT code FROM company_facts ORDER BY code")),
    )
    has_dependencies = raw.run(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_material_dependencies'"
    ).fetchone() is not None
    publication = ReadCompanyPublicationIdentity(formal)
    dependencies = ReadRequiredCompanyMaterialDependencies(raw, publication.extraction_hash) if (
        has_dependencies and publication is not None
    ) else ()
    dependency_items = []
    for dependency in dependencies[:100]:
        text_path = raw_path.parent / "text" / dependency.body_hash.hex()[:2] / (dependency.body_hash.hex() + ".txt.gz")
        dependency_items.append({
            "code": dependency.code, "source": dependency.source, "source_key": dependency.evidence_id,
            "scope": dependency.scope, "reason": dependency.reason_code,
            "record_hash": dependency.record_hash,
            "body_hash": dependency.body_hash.hex(),
            "extraction_hash": dependency.extraction_hash.hex(),
            "current_record_hash": dependency.current_record_hash,
            "current_body_hash": dependency.current_body_hash.hex() if dependency.current_body_hash is not None else None,
            "current_extraction_hash": dependency.current_extraction_hash.hex(),
            "is_current_version": dependency.is_current_version,
            "text_file_present": text_path.is_file(),
        })
    return {
        "read_only": True, "tables": tables, "table_count": len(tables), "field_count": field_count,
        "invalid_value_types": invalid_types,
        "nonfinite_values": nonfinite_values,
        "source_agreement": {
            "financial_rows": finance[0], "financial_value_mismatches": finance[1],
            "pledge_rows": pledge[0], "pledge_explicit_source_matches": pledge[1],
            "pledge_zeros_from_complete_snapshot": pledge[2],
            "pledge_unmatched": pledge[0] - pledge[1] - pledge[2],
            "unlock_rows": unlock[0], "unlock_value_mismatches": unlock[1],
            "unlocated_evidence_refs": missing_evidence, "risk_events_without_evidence": risk_evidence,
            "positive_events_without_evidence": positive_evidence,
        },
        "applicable_missing": {"contract_events": applicable[0], "contract_amount_missing": applicable[1], "contract_revenue_missing": applicable[2], "contract_ratio_missing": applicable[3], "share_action_actual_pct_missing": applicable[4]},
        "partition_details": partitions,
        "recovery_plan": {
            "read_only": True, "fields": len(recovery),
            "actions": dict(Counter(item.action for item in recovery)), "truncated": len(recovery) > 100,
            "items": [{
                "code": item.gap.code, "event_id": item.gap.event_id,
                "source_key": item.gap.evidence_id, "field": item.gap.field,
                "reason": item.reason, "next_step": item.action,
                "network_required": item.action == "download",
            } for item in recovery[:100]],
        },
        "field_results": {
            "derived_from_current_facts": False, "published_conclusions": True,
            "verified_fields": sum(item.state == "verified" for item in field_results),
            "field_correctness_verified": False,
            "field_evidence_locator_verified": False,
            "applicable": sum(item.applicable for item in field_results),
            "states": dict(Counter(item.state for item in field_results)),
            "truncated": len(field_results) > 100,
            "items": [{
                "code": item.code, "event_id": item.event_id,
                "source_key": item.evidence[0].source_key if item.evidence else None,
                "field": item.field, "state": item.state, "applicable": item.applicable,
                "reason": item.reason,
                "next_step": next((plan.action for plan in recovery if plan.gap.code == item.code
                                   and plan.gap.event_id == item.event_id and plan.gap.field == item.field), None),
            } for item in field_results[:100]],
        },
        "material_dependencies": {
            "ledger_available": has_dependencies,
            "reason": "dependency_ledger_uninitialized" if not has_dependencies else "publication_identity_missing" if publication is None else None,
            "required_reviews": len({
                (item.code, item.source, item.evidence_id, item.scope, item.reason_code) for item in dependencies
            }) if has_dependencies and publication is not None else None,
            "required_versions": len(dependencies) if has_dependencies and publication is not None else None,
            "file_presence_is_not_hash_verification": True,
            "truncated": len(dependencies) > 100, "items": dependency_items,
        },
        "interpretation": "population_and_source_agreement_do_not_prove_all_event_measurements_or_historical_coverage",
        "findings": findings,
    }


def _rows(value: object, limit: int) -> tuple[dict[str, object], ...]:
    return tuple(row for row in value[:limit] if isinstance(row, dict)) if isinstance(value, list) else ()


def ProjectCompanyFieldAudit(source: Mapping[str, object]) -> dict[str, object]:
    result = {key: source.get(key) for key in (
        "read_only", "table_count", "field_count", "invalid_value_types", "nonfinite_values", "interpretation",
    )}
    for name, keys in (
        ("source_agreement", ("financial_rows", "financial_value_mismatches", "pledge_rows", "pledge_explicit_source_matches", "pledge_zeros_from_complete_snapshot", "pledge_unmatched", "unlock_rows", "unlock_value_mismatches", "unlocated_evidence_refs", "risk_events_without_evidence", "positive_events_without_evidence")),
        ("applicable_missing", ("contract_events", "contract_amount_missing", "contract_revenue_missing", "contract_ratio_missing", "share_action_actual_pct_missing")),
    ):
        value = source.get(name)
        fields = value if isinstance(value, Mapping) else {}
        result[name] = {key: fields.get(key) for key in keys}
    result["partition_details"] = [{key: row.get(key) for key in (
        "market", "start", "end", "failure", "expected_rows", "received_rows", "next_page", "queries", "complete_queries", "query_total_sum", "unique_records", "unique_source_ids",
    )} for row in _rows(source.get("partition_details"), 12)]
    result["tables"] = [{
        "table": row.get("table"), "rows": row.get("rows"),
        "fields": [{key: field.get(key) for key in ("name", "populated", "null", "invalid_type", "nonfinite")} for field in _rows(row.get("fields"), 128)],
    } for row in _rows(source.get("tables"), 32)]
    recovery = source.get("recovery_plan")
    if isinstance(recovery, Mapping):
        result["recovery_plan"] = {
            **{key: recovery.get(key) for key in ("read_only", "fields", "actions", "truncated")},
            "items": [{key: row.get(key) for key in (
                "code", "event_id", "source_key", "field", "reason", "next_step", "network_required",
            )} for row in _rows(recovery.get("items"), 100)],
        }
    for name, summary_keys in (
        ("field_results", ("derived_from_current_facts", "published_conclusions", "verified_fields", "field_correctness_verified", "field_evidence_locator_verified", "applicable", "states", "truncated")),
        ("material_dependencies", ("ledger_available", "reason", "required_reviews", "required_versions", "file_presence_is_not_hash_verification", "truncated")),
    ):
        value = source.get(name)
        if not isinstance(value, Mapping):
            continue
        item_keys = (
            ("code", "event_id", "source_key", "field", "state", "applicable", "reason", "next_step")
            if name == "field_results" else (
                "code", "source", "source_key", "scope", "reason", "record_hash", "body_hash",
                "extraction_hash", "current_record_hash", "current_body_hash", "current_extraction_hash",
                "is_current_version", "text_file_present",
            )
        )
        result[name] = {
            **{key: value.get(key) for key in summary_keys},
            "items": [{key: row.get(key) for key in item_keys} for row in _rows(value.get("items"), 100)],
        }
    return result
