"""Read-only field population, source agreement and saved query-union diagnostics."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Mapping

from trader.infra.sqlite_connection import SqliteConnection
from trader.download.infra.company_facts_protocol import EVIDENCE_SOURCES


def AuditCompanyFields(formal: SqliteConnection, raw_path: Path) -> dict[str, object]:
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
        "COALESCE(SUM(event_type IN (1,2) AND actual_pct IS NULL),0) FROM main.company_positive_event"
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
    return {
        "read_only": True, "tables": tables, "table_count": len(tables), "field_count": field_count,
        "invalid_storage_types": invalid_types,
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
        "interpretation": "population_and_source_agreement_do_not_prove_all_event_measurements_or_historical_coverage",
        "findings": findings,
    }


def _rows(value: object, limit: int) -> tuple[dict[str, object], ...]:
    return tuple(row for row in value[:limit] if isinstance(row, dict)) if isinstance(value, list) else ()


def ProjectCompanyFieldAudit(source: Mapping[str, object]) -> dict[str, object]:
    result = {key: source.get(key) for key in (
        "read_only", "table_count", "field_count", "invalid_storage_types", "nonfinite_values", "interpretation",
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
    return result
