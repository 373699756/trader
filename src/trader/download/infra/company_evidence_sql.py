"""Fresh company evidence schema. No legacy database migration or recovery payloads."""

from trader.infra.sqlite_connection import SqliteSchema

SCHEMA = SqliteSchema(
    2,
    (
        "CREATE TABLE IF NOT EXISTS contract(schema_id TEXT PRIMARY KEY CHECK(schema_id='company_collection'))",
        """
        CREATE TABLE IF NOT EXISTS disclosures (
          id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_key TEXT NOT NULL,
          kind TEXT NOT NULL CHECK(kind IN ('announcement','news')), title TEXT NOT NULL,
          published_at TEXT NOT NULL, url TEXT NOT NULL, record_hash TEXT NOT NULL,
          body_hash BLOB CHECK(body_hash IS NULL OR length(body_hash)=32),
          body_checked INTEGER NOT NULL DEFAULT 0 CHECK(body_checked IN (0,1)),
          body_retained INTEGER NOT NULL DEFAULT 0 CHECK(body_retained IN (0,1)),
          UNIQUE(source, source_key)
        )
        """,
        "CREATE INDEX IF NOT EXISTS disclosures_date ON disclosures(kind,published_at)",
        """
        CREATE TABLE IF NOT EXISTS disclosure_codes (
          code TEXT NOT NULL, disclosure_id INTEGER NOT NULL REFERENCES disclosures(id) ON DELETE CASCADE,
          PRIMARY KEY(code,disclosure_id)
        ) WITHOUT ROWID
        """,
        "CREATE INDEX IF NOT EXISTS disclosure_links ON disclosure_codes(disclosure_id,code)",
        """
        CREATE TABLE IF NOT EXISTS partitions (
          market TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL, next_page INTEGER NOT NULL DEFAULT 1,
          expected_rows INTEGER, received_rows INTEGER NOT NULL DEFAULT 0, is_complete INTEGER NOT NULL DEFAULT 0,
          checked_at TEXT, failure TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(market,start_date,end_date)
        ) WITHOUT ROWID
        """,
        """
        CREATE TABLE IF NOT EXISTS partition_rows (
          market TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
          source_key TEXT NOT NULL, code TEXT NOT NULL,
          PRIMARY KEY(market,start_date,end_date,source_key,code)
        ) WITHOUT ROWID
        """,
        """
        CREATE TABLE IF NOT EXISTS coverage (
          market TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL, checked_at TEXT NOT NULL,
          PRIMARY KEY(market,start_date,end_date)
        ) WITHOUT ROWID
        """,
        "CREATE INDEX IF NOT EXISTS partition_source_rows ON partition_rows(source_key)",
        """
        CREATE TABLE IF NOT EXISTS financials (
          code TEXT NOT NULL, report_date TEXT NOT NULL, published_at TEXT NOT NULL,
          eps REAL, bps REAL, revenue_growth_pct REAL, profit_growth_pct REAL,
          core_profit_growth_pct REAL, roe_pct REAL, parent_profit REAL, core_profit REAL,
          PRIMARY KEY(code,report_date)
        ) WITHOUT ROWID
        """,
        """
        CREATE TABLE IF NOT EXISTS ratios (
          component TEXT NOT NULL CHECK(component IN ('pledge','unlock')), code TEXT NOT NULL,
          as_of_date TEXT NOT NULL, ratio_pct REAL NOT NULL CHECK(ratio_pct>=0 AND ratio_pct<=100),
          PRIMARY KEY(component,code,as_of_date)
        ) WITHOUT ROWID
        """,
        """
        CREATE TABLE IF NOT EXISTS component_checks (
          component TEXT NOT NULL, scope TEXT NOT NULL, checked_at TEXT, failure TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(component,scope)
        ) WITHOUT ROWID
        """,
        """
        CREATE TABLE IF NOT EXISTS structured_progress (
          component TEXT NOT NULL, scope TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
          next_page INTEGER NOT NULL, expected_rows INTEGER, received_rows INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(component,scope)
        ) WITHOUT ROWID
        """,
        """
        CREATE TABLE IF NOT EXISTS structured_members (
          component TEXT NOT NULL, code TEXT NOT NULL, as_of_date TEXT NOT NULL,
          ratio_pct REAL,
          PRIMARY KEY(component,code,as_of_date)
        ) WITHOUT ROWID
        """,
        """
        CREATE TABLE IF NOT EXISTS ratio_snapshots (
          component TEXT PRIMARY KEY, start_date TEXT NOT NULL, end_date TEXT NOT NULL, checked_at TEXT NOT NULL
        ) WITHOUT ROWID
        """,
        "CREATE TABLE IF NOT EXISTS company_population(code TEXT PRIMARY KEY) WITHOUT ROWID",
        """CREATE TABLE IF NOT EXISTS document_publications (
          disclosure_id INTEGER NOT NULL REFERENCES disclosures(id) ON DELETE CASCADE,
          code TEXT NOT NULL, record_hash TEXT NOT NULL,
          extraction_hash BLOB NOT NULL CHECK(length(extraction_hash)=32), published_at TEXT NOT NULL,
          PRIMARY KEY(disclosure_id,code)
        ) WITHOUT ROWID""",
    ),
)

# Operational tables share the collection schema version; old formats are rejected.
DOCUMENT_RETRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS document_retries (
  disclosure_id INTEGER PRIMARY KEY REFERENCES disclosures(id) ON DELETE CASCADE,
  record_hash TEXT NOT NULL, retry_policy_id TEXT NOT NULL, failure TEXT NOT NULL, attempted_at TEXT NOT NULL,
  retry_at TEXT NOT NULL, attempts INTEGER NOT NULL,
  payload_hash BLOB CHECK(payload_hash IS NULL OR length(payload_hash)=32)
)
"""

DOCUMENT_EXTRACTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS document_extractions (
  disclosure_id INTEGER PRIMARY KEY REFERENCES disclosures(id) ON DELETE CASCADE,
  record_hash TEXT NOT NULL, payload_hash BLOB NOT NULL CHECK(length(payload_hash)=32),
  method TEXT NOT NULL CHECK(method IN ('text','ocr')), policy_id TEXT NOT NULL,
  quality REAL CHECK(quality IS NULL OR (quality>=0 AND quality<=100)), extracted_at TEXT NOT NULL
)
"""

DISCLOSURE_QUERY_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS disclosure_query_policy (
      singleton INTEGER PRIMARY KEY CHECK(singleton=1), query_policy_id TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS disclosure_queries (
      market TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL, scope TEXT NOT NULL,
      plate TEXT NOT NULL, securities TEXT NOT NULL, next_page INTEGER NOT NULL DEFAULT 1,
      expected_rows INTEGER, received_rows INTEGER NOT NULL DEFAULT 0,
      is_complete INTEGER NOT NULL DEFAULT 0, failure TEXT NOT NULL DEFAULT '',
      PRIMARY KEY(market,start_date,end_date,scope),
      FOREIGN KEY(market,start_date,end_date) REFERENCES partitions(market,start_date,end_date) ON DELETE CASCADE
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS disclosure_query_rows (
      market TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL, scope TEXT NOT NULL,
      source_key TEXT NOT NULL, code TEXT NOT NULL, record_key TEXT NOT NULL,
      PRIMARY KEY(market,start_date,end_date,scope,source_key,code),
      FOREIGN KEY(market,start_date,end_date,scope) REFERENCES disclosure_queries(market,start_date,end_date,scope) ON DELETE CASCADE
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS partition_retries (
      market TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL, scope TEXT NOT NULL,
      retry_at TEXT NOT NULL,
      PRIMARY KEY(market,start_date,end_date,scope),
      FOREIGN KEY(market,start_date,end_date) REFERENCES partitions(market,start_date,end_date) ON DELETE CASCADE
    ) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS disclosure_query_source_rows ON disclosure_query_rows(source_key)",
)

__all__ = ["SCHEMA", "DOCUMENT_EXTRACTION_SCHEMA", "DOCUMENT_RETRY_SCHEMA", "DISCLOSURE_QUERY_SCHEMA"]
