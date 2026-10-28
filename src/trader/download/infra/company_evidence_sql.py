"""Fresh company evidence schema. No legacy database migration or recovery payloads."""

from trader.infra.sqlite_connection import SqliteSchema

SCHEMA = SqliteSchema(
    None,
    (
        "CREATE TABLE IF NOT EXISTS contract(identity TEXT PRIMARY KEY CHECK(identity='company_collection'))",
        """
        CREATE TABLE IF NOT EXISTS disclosures (
          id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
          kind TEXT NOT NULL CHECK(kind IN ('announcement','news')), title TEXT NOT NULL,
          published_at TEXT NOT NULL, url TEXT NOT NULL, digest TEXT NOT NULL,
          body_hash BLOB CHECK(body_hash IS NULL OR length(body_hash)=32),
          body_checked INTEGER NOT NULL DEFAULT 0 CHECK(body_checked IN (0,1)),
          body_retained INTEGER NOT NULL DEFAULT 0 CHECK(body_retained IN (0,1)),
          UNIQUE(source, source_id)
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
          market TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, next_page INTEGER NOT NULL DEFAULT 1,
          expected_rows INTEGER, received_rows INTEGER NOT NULL DEFAULT 0, complete INTEGER NOT NULL DEFAULT 0,
          checked_at TEXT, failure TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(market,start,end)
        ) WITHOUT ROWID
        """,
        """
        CREATE TABLE IF NOT EXISTS partition_rows (
          market TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
          source_id TEXT NOT NULL, code TEXT NOT NULL,
          PRIMARY KEY(market,start,end,source_id,code)
        ) WITHOUT ROWID
        """,
        """
        CREATE TABLE IF NOT EXISTS coverage (
          market TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, checked_at TEXT NOT NULL,
          PRIMARY KEY(market,start,end)
        ) WITHOUT ROWID
        """,
        "CREATE INDEX IF NOT EXISTS partition_source_rows ON partition_rows(source_id)",
        """
        CREATE TABLE IF NOT EXISTS financials (
          code TEXT NOT NULL, report_date TEXT NOT NULL, published_at TEXT NOT NULL,
          basic_eps REAL, book_value_per_share REAL, revenue_growth_pct REAL, net_profit_growth_pct REAL,
          core_profit_growth_pct REAL, roe_pct REAL, parent_net_profit REAL, core_net_profit REAL,
          PRIMARY KEY(code,report_date)
        ) WITHOUT ROWID
        """,
        """
        CREATE TABLE IF NOT EXISTS ratios (
          component TEXT NOT NULL CHECK(component IN ('pledge','unlock')), code TEXT NOT NULL,
          day TEXT NOT NULL, ratio_pct REAL NOT NULL CHECK(ratio_pct>=0 AND ratio_pct<=100),
          PRIMARY KEY(component,code,day)
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
          component TEXT NOT NULL, scope TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
          next_page INTEGER NOT NULL, expected_rows INTEGER, received_rows INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(component,scope)
        ) WITHOUT ROWID
        """,
        """
        CREATE TABLE IF NOT EXISTS structured_members (
          component TEXT NOT NULL, code TEXT NOT NULL, day TEXT NOT NULL,
          ratio_pct REAL,
          PRIMARY KEY(component,code,day)
        ) WITHOUT ROWID
        """,
        """
        CREATE TABLE IF NOT EXISTS ratio_snapshots (
          component TEXT PRIMARY KEY, start TEXT NOT NULL, end TEXT NOT NULL, checked_at TEXT NOT NULL
        ) WITHOUT ROWID
        """,
        "CREATE TABLE IF NOT EXISTS company_population(code TEXT PRIMARY KEY) WITHOUT ROWID",
        """CREATE TABLE IF NOT EXISTS document_publications (
          disclosure_id INTEGER NOT NULL REFERENCES disclosures(id) ON DELETE CASCADE,
          code TEXT NOT NULL, digest TEXT NOT NULL,
          extraction_hash BLOB NOT NULL CHECK(length(extraction_hash)=32), published_at TEXT NOT NULL,
          PRIMARY KEY(disclosure_id,code)
        ) WITHOUT ROWID""",
    ),
)

# Additive operational state; existing facts, cursors and published bytes stay intact.
DOCUMENT_RETRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS document_retries (
  disclosure_id INTEGER PRIMARY KEY REFERENCES disclosures(id) ON DELETE CASCADE,
  digest TEXT NOT NULL, policy TEXT NOT NULL, failure TEXT NOT NULL, attempted_at TEXT NOT NULL,
  retry_after TEXT NOT NULL, attempts INTEGER NOT NULL
)
"""

DISCLOSURE_QUERY_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS disclosure_query_policy (
      singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS disclosure_queries (
      market TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, scope TEXT NOT NULL,
      plate TEXT NOT NULL, securities TEXT NOT NULL, next_page INTEGER NOT NULL DEFAULT 1,
      expected_rows INTEGER, received_rows INTEGER NOT NULL DEFAULT 0,
      complete INTEGER NOT NULL DEFAULT 0, failure TEXT NOT NULL DEFAULT '',
      PRIMARY KEY(market,start,end,scope),
      FOREIGN KEY(market,start,end) REFERENCES partitions(market,start,end) ON DELETE CASCADE
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS disclosure_query_rows (
      market TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, scope TEXT NOT NULL,
      source_id TEXT NOT NULL, code TEXT NOT NULL, record_key TEXT NOT NULL,
      PRIMARY KEY(market,start,end,scope,source_id,code),
      FOREIGN KEY(market,start,end,scope) REFERENCES disclosure_queries(market,start,end,scope) ON DELETE CASCADE
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS partition_retries (
      market TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, scope TEXT NOT NULL,
      retry_after TEXT NOT NULL,
      PRIMARY KEY(market,start,end,scope),
      FOREIGN KEY(market,start,end) REFERENCES partitions(market,start,end) ON DELETE CASCADE
    ) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS disclosure_query_source_rows ON disclosure_query_rows(source_id)",
)

__all__ = ["SCHEMA", "DOCUMENT_RETRY_SCHEMA", "DISCLOSURE_QUERY_SCHEMA"]
