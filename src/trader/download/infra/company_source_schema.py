"""Exact schema for the Company source and working facts database."""

from trader.download.infra.company_evidence_sql import COMPANY_MATERIAL_STATEMENTS
from trader.download.infra.company_facts_schema import COMPANY_FACTS_SCHEMA
from trader.infra.sqlite_connection import SqliteSchema


COMPANY_SOURCE_SCHEMA = SqliteSchema((
    *COMPANY_MATERIAL_STATEMENTS,
    *COMPANY_FACTS_SCHEMA.statements,
    """CREATE TABLE company_normalization_seed (
      singleton INTEGER PRIMARY KEY CHECK(singleton=1),source_content_hash BLOB NOT NULL,
      source_extraction_hash BLOB NOT NULL,source_published_at INTEGER NOT NULL,baseline_digest TEXT NOT NULL,
      complete INTEGER NOT NULL CHECK(complete IN (0,1)),field_rules_hash BLOB NOT NULL
    )""",
    """CREATE TABLE company_round_page (
      scope TEXT NOT NULL,page INTEGER NOT NULL,pages INTEGER NOT NULL,total INTEGER NOT NULL,
      raw_count INTEGER NOT NULL,response_hash TEXT NOT NULL,PRIMARY KEY(scope,page)
    ) WITHOUT ROWID""",
    """CREATE TABLE company_round_member (
      scope TEXT NOT NULL,code TEXT NOT NULL,report_date TEXT NOT NULL,
      record_hash TEXT,
      PRIMARY KEY(scope,code,report_date),
      FOREIGN KEY(code,report_date,record_hash) REFERENCES company_financial_observation(code,report_date,record_hash)
    ) WITHOUT ROWID""",
    """CREATE TABLE company_round_intent (
      scope TEXT PRIMARY KEY,generation BLOB NOT NULL,parent_content_hash BLOB,
      parent_extraction_hash BLOB,parent_published_at INTEGER,confirmed_content_hash BLOB
    ) WITHOUT ROWID""",
    """CREATE TABLE company_round_generation (
      scope TEXT PRIMARY KEY,generation BLOB NOT NULL,normalized_hash BLOB NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE company_recovered_input (
      scope TEXT NOT NULL,code TEXT NOT NULL,source TEXT NOT NULL,source_key TEXT NOT NULL,
      record_hash TEXT NOT NULL,body_hash BLOB NOT NULL CHECK(length(body_hash)=32),prepared INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(scope,code,source,source_key,record_hash,body_hash)
    ) WITHOUT ROWID""",
    "CREATE TABLE company_pledge_day(scope TEXT PRIMARY KEY,day TEXT NOT NULL) WITHOUT ROWID",
    """CREATE TABLE company_source_page (
      scope TEXT NOT NULL,component TEXT NOT NULL,page INTEGER NOT NULL,pages INTEGER NOT NULL,
      total INTEGER NOT NULL,raw_count INTEGER NOT NULL,decoded_hash TEXT NOT NULL,
      PRIMARY KEY(scope,component,page)
    ) WITHOUT ROWID""",
    """CREATE TABLE company_ratio_observation (
      scope TEXT NOT NULL,component TEXT NOT NULL,code TEXT NOT NULL,day TEXT NOT NULL,ratio REAL NOT NULL,
      PRIMARY KEY(scope,component,code,day)
    ) WITHOUT ROWID""",
    """CREATE TABLE company_source_task (
      scope TEXT NOT NULL,family TEXT NOT NULL,task TEXT NOT NULL,accepted INTEGER NOT NULL,input_hash TEXT NOT NULL,
      PRIMARY KEY(scope,family,task)
    ) WITHOUT ROWID""",
    """CREATE TABLE company_annual_income_observation (
      scope TEXT NOT NULL,code TEXT NOT NULL,report_date TEXT NOT NULL,disclosed_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,observed_at TEXT NOT NULL,currency TEXT NOT NULL,revenue REAL,record_hash TEXT NOT NULL,
      PRIMARY KEY(scope,code,report_date,disclosed_at,updated_at)
    ) WITHOUT ROWID""",
))


__all__ = ["COMPANY_SOURCE_SCHEMA"]
