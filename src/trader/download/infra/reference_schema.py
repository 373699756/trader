"""Versioned securities, ST evidence and atomic reference publications."""

from trader.infra.sqlite_connection import SqliteSchema

REFERENCE_SCHEMA = SqliteSchema(
    1,
    (
        "CREATE TABLE schema_contract(schema_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL) WITHOUT ROWID",
        """CREATE TABLE security(
            security_id BLOB PRIMARY KEY CHECK(length(security_id)=32),
            code TEXT NOT NULL CHECK(length(code)=6 AND code NOT GLOB '*[^0-9]*'),
            name TEXT NOT NULL, board TEXT NOT NULL CHECK(board IN ('main','chinext','star')),
            listed_date INTEGER NOT NULL, delisted_date INTEGER, source_version TEXT NOT NULL,
            content_hash BLOB NOT NULL UNIQUE CHECK(length(content_hash)=32),
            UNIQUE(code,security_id), CHECK(delisted_date IS NULL OR delisted_date>listed_date)
        ) WITHOUT ROWID""",
        """CREATE TABLE security_st_evidence(
            st_id BLOB PRIMARY KEY CHECK(length(st_id)=32),
            code TEXT NOT NULL CHECK(length(code)=6 AND code NOT GLOB '*[^0-9]*'),
            checked_date INTEGER NOT NULL,
            st_status TEXT NOT NULL CHECK(st_status IN ('ever_st','clear','unknown')),
            content_hash BLOB NOT NULL UNIQUE CHECK(length(content_hash)=32), UNIQUE(code,st_id)
        ) WITHOUT ROWID""",
        """CREATE TABLE security_st_current(
            code TEXT PRIMARY KEY, st_id BLOB NOT NULL,
            FOREIGN KEY(code,st_id) REFERENCES security_st_evidence(code,st_id)
        ) WITHOUT ROWID""",
        """CREATE TABLE calendar_version(
            calendar_id BLOB PRIMARY KEY CHECK(length(calendar_id)=32),
            content_hash BLOB NOT NULL UNIQUE CHECK(length(content_hash)=32),
            first_trade_date INTEGER NOT NULL, last_trade_date INTEGER NOT NULL,
            session_count INTEGER NOT NULL CHECK(session_count BETWEEN 1 AND 20000),
            CHECK(first_trade_date<=last_trade_date)
        ) WITHOUT ROWID""",
        """CREATE TABLE trading_calendar(
            calendar_id BLOB NOT NULL REFERENCES calendar_version(calendar_id),
            trade_date INTEGER NOT NULL, PRIMARY KEY(calendar_id,trade_date)
        ) WITHOUT ROWID""",
        """CREATE TABLE reference_publication(
            publication_id TEXT PRIMARY KEY, content_hash BLOB NOT NULL UNIQUE CHECK(length(content_hash)=32),
            checked_at INTEGER NOT NULL, calendar_id BLOB NOT NULL REFERENCES calendar_version(calendar_id),
            industry_version TEXT, security_count INTEGER NOT NULL CHECK(security_count BETWEEN 1 AND 12000)
        ) WITHOUT ROWID""",
        """CREATE TABLE reference_member(
            publication_id TEXT NOT NULL REFERENCES reference_publication(publication_id),
            code TEXT NOT NULL, security_id BLOB NOT NULL, st_id BLOB NOT NULL,
            PRIMARY KEY(publication_id,code),
            FOREIGN KEY(code,security_id) REFERENCES security(code,security_id),
            FOREIGN KEY(code,st_id) REFERENCES security_st_evidence(code,st_id)
        ) WITHOUT ROWID""",
        """CREATE TABLE reference_current(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            publication_id TEXT NOT NULL REFERENCES reference_publication(publication_id)
        )""",
    ),
)

__all__ = ["REFERENCE_SCHEMA"]
