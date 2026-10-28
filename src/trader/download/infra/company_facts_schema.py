"""Relational company publication, separate from collection and retry state."""

from trader.infra.sqlite_connection import SqliteSchema

COMPANY_FACTS_VERSION = 1

COMPANY_FACTS_SCHEMA = SqliteSchema(
    COMPANY_FACTS_VERSION,
    (
        """CREATE TABLE company_facts (
          code TEXT PRIMARY KEY CHECK(length(code)=6 AND code NOT GLOB '*[^0-9]*'),
          financial_report_date INTEGER, financial_disclosed_at INTEGER,
          financial_state INTEGER NOT NULL CHECK(financial_state IN (0,1,2)),
          basic_eps REAL, book_value_per_share REAL, revenue_growth_pct REAL, net_profit_growth_pct REAL,
          core_profit_growth_pct REAL, roe_pct REAL, parent_net_profit REAL, core_net_profit REAL,
          pledge_ratio_pct REAL CHECK(pledge_ratio_pct BETWEEN 0 AND 100), pledge_as_of INTEGER,
          pledge_state INTEGER NOT NULL CHECK(pledge_state IN (0,1,2)),
          unlock_checked_at INTEGER, unlock_coverage_end INTEGER,
          unlock_state INTEGER NOT NULL CHECK(unlock_state IN (0,1,2)),
          pending_document_count INTEGER NOT NULL CHECK(pending_document_count>=0),
          announcement_coverage_start INTEGER, announcement_coverage_end INTEGER,
          announcement_state INTEGER NOT NULL CHECK(announcement_state IN (0,1,2)),
          checked_at INTEGER NOT NULL, published_at INTEGER NOT NULL,
          CHECK((financial_report_date IS NULL)=(financial_disclosed_at IS NULL)),
          CHECK((pledge_ratio_pct IS NULL)=(pledge_as_of IS NULL))
        ) WITHOUT ROWID""",
        """CREATE TABLE company_risk_event (
          id INTEGER PRIMARY KEY, code TEXT NOT NULL REFERENCES company_facts(code) ON DELETE CASCADE,
          event_key TEXT NOT NULL CHECK(length(event_key) BETWEEN 1 AND 240),
          risk_type INTEGER NOT NULL CHECK(risk_type BETWEEN 1 AND 10),
          subject_type INTEGER NOT NULL CHECK(subject_type BETWEEN 1 AND 4),
          risk_level INTEGER NOT NULL CHECK(risk_level BETWEEN 1 AND 4),
          case_number TEXT CHECK(length(case_number) BETWEEN 1 AND 240),
          first_disclosed_at INTEGER NOT NULL, last_disclosed_at INTEGER NOT NULL,
          effective_at INTEGER NOT NULL, status INTEGER NOT NULL CHECK(status IN (1,2,3)),
          resolved_at INTEGER, expires_at INTEGER,
          UNIQUE(code,event_key),
          CHECK(last_disclosed_at>=first_disclosed_at AND effective_at>=first_disclosed_at),
          CHECK(status=1 OR resolved_at IS NOT NULL),
          CHECK(resolved_at IS NULL OR resolved_at>=first_disclosed_at),
          CHECK((risk_type<=3 AND risk_level<=3) OR (risk_type>=4 AND risk_level=4)),
          CHECK(risk_type<>1 OR risk_level=1)
        )""",
        """CREATE TABLE company_reduction_plan (
          risk_event_id INTEGER PRIMARY KEY REFERENCES company_risk_event(id) ON DELETE CASCADE,
          planned_start_date INTEGER, planned_end_date INTEGER,
          planned_ratio_pct REAL CHECK(planned_ratio_pct BETWEEN 0 AND 100),
          completion_disclosed_at INTEGER,
          CHECK(planned_start_date IS NULL OR planned_end_date IS NULL OR planned_start_date<=planned_end_date)
        )""",
        """CREATE TABLE company_positive_event (
          id INTEGER PRIMARY KEY, code TEXT NOT NULL REFERENCES company_facts(code) ON DELETE CASCADE,
          event_key TEXT NOT NULL CHECK(length(event_key) BETWEEN 1 AND 240),
          event_type INTEGER NOT NULL CHECK(event_type BETWEEN 1 AND 3),
          subject_type INTEGER NOT NULL CHECK(subject_type BETWEEN 1 AND 4),
          status INTEGER NOT NULL CHECK(status BETWEEN 1 AND 4),
          first_disclosed_at INTEGER NOT NULL, last_disclosed_at INTEGER NOT NULL, cancelled_at INTEGER,
          positive_level INTEGER NOT NULL CHECK(positive_level BETWEEN 0 AND 3),
          ineligible_reason INTEGER CHECK(ineligible_reason BETWEEN 1 AND 25),
          actual_ratio_pct REAL CHECK(actual_ratio_pct BETWEEN 0 AND 100),
          repurchase_purpose INTEGER CHECK(repurchase_purpose BETWEEN 0 AND 4),
          contract_amount_cny REAL CHECK(contract_amount_cny>=0),
          reference_annual_revenue_cny REAL CHECK(reference_annual_revenue_cny>=0),
          reference_report_date INTEGER, reference_disclosed_at INTEGER,
          amount_to_revenue_pct REAL CHECK(amount_to_revenue_pct>=0),
          binding_confirmed INTEGER CHECK(binding_confirmed IN (0,1)),
          amount_basis INTEGER CHECK(amount_basis IN (0,1)),
          UNIQUE(code,event_key), CHECK(last_disclosed_at>=first_disclosed_at),
          CHECK(status<>4 OR cancelled_at IS NOT NULL),
          CHECK((event_type=1 AND repurchase_purpose IS NOT NULL) OR
                (event_type<>1 AND repurchase_purpose IS NULL)),
          CHECK((event_type=3 AND actual_ratio_pct IS NULL AND binding_confirmed IS NOT NULL
                 AND amount_basis IS NOT NULL) OR
                (event_type<>3 AND contract_amount_cny IS NULL AND reference_annual_revenue_cny IS NULL
                 AND reference_report_date IS NULL AND reference_disclosed_at IS NULL
                 AND amount_to_revenue_pct IS NULL AND binding_confirmed IS NULL AND amount_basis IS NULL))
        )""",
        """CREATE TABLE company_evidence_ref (
          id INTEGER PRIMARY KEY, source INTEGER NOT NULL CHECK(source BETWEEN 1 AND 10),
          source_key TEXT NOT NULL CHECK(length(source_key) BETWEEN 1 AND 240), UNIQUE(source,source_key)
        )""",
        """CREATE TABLE company_positive_milestone (
          positive_event_id INTEGER NOT NULL REFERENCES company_positive_event(id) ON DELETE CASCADE,
          level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 3), qualified_at INTEGER NOT NULL,
          measurement_value_pct REAL NOT NULL CHECK(measurement_value_pct>=0),
          evidence_id INTEGER NOT NULL REFERENCES company_evidence_ref(id),
          PRIMARY KEY(positive_event_id,level)
        ) WITHOUT ROWID""",
        """CREATE TABLE company_unlock (
          code TEXT NOT NULL REFERENCES company_facts(code) ON DELETE CASCADE, unlock_date INTEGER NOT NULL,
          ratio_pct REAL NOT NULL CHECK(ratio_pct BETWEEN 0 AND 100), PRIMARY KEY(code,unlock_date)
        ) WITHOUT ROWID""",
        """CREATE TABLE company_review (
          id INTEGER PRIMARY KEY, code TEXT NOT NULL REFERENCES company_facts(code) ON DELETE CASCADE,
          scope INTEGER NOT NULL CHECK(scope BETWEEN 1 AND 5), reason INTEGER NOT NULL CHECK(reason BETWEEN 1 AND 25),
          evidence_id INTEGER REFERENCES company_evidence_ref(id), detected_at INTEGER NOT NULL
        )""",
        "CREATE UNIQUE INDEX company_review_identity ON company_review(code,scope,reason,COALESCE(evidence_id,-1))",
        """CREATE TABLE company_risk_evidence (
          risk_event_id INTEGER NOT NULL REFERENCES company_risk_event(id) ON DELETE CASCADE,
          evidence_id INTEGER NOT NULL REFERENCES company_evidence_ref(id), position INTEGER NOT NULL CHECK(position>=0),
          PRIMARY KEY(risk_event_id,evidence_id), UNIQUE(risk_event_id,position)
        ) WITHOUT ROWID""",
        """CREATE TABLE company_positive_evidence (
          positive_event_id INTEGER NOT NULL REFERENCES company_positive_event(id) ON DELETE CASCADE,
          evidence_id INTEGER NOT NULL REFERENCES company_evidence_ref(id), position INTEGER NOT NULL CHECK(position>=0),
          PRIMARY KEY(positive_event_id,evidence_id), UNIQUE(positive_event_id,position)
        ) WITHOUT ROWID""",
        """CREATE TABLE company_exclusion (
          id INTEGER PRIMARY KEY, code TEXT NOT NULL CHECK(length(code)=6 AND code NOT GLOB '*[^0-9]*'),
          reason INTEGER NOT NULL CHECK(reason IN (1,2)), effective_at INTEGER NOT NULL,
          source INTEGER NOT NULL CHECK(source BETWEEN 1 AND 3),
          source_key TEXT NOT NULL CHECK(length(source_key) BETWEEN 1 AND 240),
          evidence_hash BLOB NOT NULL CHECK(typeof(evidence_hash)='blob' AND length(evidence_hash)=32),
          UNIQUE(code,reason,source,source_key)
        )""",
        """CREATE TABLE company_publication (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL CHECK(schema_version=1),
          extraction_hash BLOB NOT NULL CHECK(typeof(extraction_hash)='blob' AND length(extraction_hash)=32),
          content_hash BLOB NOT NULL CHECK(typeof(content_hash)='blob' AND length(content_hash)=32),
          published_at INTEGER NOT NULL
        )""",
    ),
)

__all__ = ["COMPANY_FACTS_SCHEMA", "COMPANY_FACTS_VERSION"]
