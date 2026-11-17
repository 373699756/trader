"""Legacy contract link payloads cannot bypass the current body subject guard."""

import hashlib
import sqlite3
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.download.company_resources import CompanyDocumentFilesFixture
from tests.helpers.download.configuration import CompanyEventPolicyFixture
from trader.download.domain.company_event_extraction import ExtractCompanyLinkCandidate
from trader.download.domain.company_evidence import CompanyDocument, Disclosure
from trader.download.infra.company_contract_link_rules import (
    ReadCompanyContractLinkRules,
    WriteCompanyContractLinkRules,
)
from trader.download.infra.company_link_codec import EncodeCompanyLink
from trader.download.infra.company_evidence_sql import CONTRACT_LINK_SCHEMA
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

_NOW = datetime(2026, 11, 5, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
_EXTRACTION = b"e" * 32
_REVENUE = "2025年度经审计的营业收入为人民币10000万元（不含税）。2025年年度报告于2026年4月20日披露。"
_ISSUER = "本公司就公告root披露进展，已签订正式合同。合同金额为人民币2000万元（不含税）。" + _REVENUE
_SUBSIDIARY = (
    "本公司及董事会全体成员保证信息披露的内容真实、准确、完整。"
    "公司全资子公司甲公司就公告root已签订正式合同。合同金额为人民币2000万元（不含税）。" + _REVENUE
)


@pytest.fixture
def connection():
    with SqliteSession(":memory:") as connection, SqliteTransaction(connection):
        connection.run(CONTRACT_LINK_SCHEMA)
        connection.run(
            "CREATE TABLE disclosures(id INTEGER PRIMARY KEY,source TEXT,source_key TEXT,record_hash TEXT,"
            "body_hash BLOB,body_retained INTEGER,body_checked INTEGER,title TEXT,published_at TEXT,url TEXT)"
        )
        connection.run("CREATE TABLE disclosure_codes(disclosure_id INTEGER,code TEXT)")
        connection.run(
            "CREATE TABLE document_link_candidates(source TEXT,source_key TEXT,record_hash TEXT,extraction_hash BLOB,payload TEXT,"
            "PRIMARY KEY(source,source_key,record_hash,extraction_hash))"
        )
        yield connection


def test_missing_proof_table_is_rejected_instead_of_being_recreated(connection):
    connection.run("DROP TABLE document_contract_link_rules")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        ReadCompanyContractLinkRules(connection, ("600001",), _EXTRACTION)
    assert connection.run(
        "SELECT 1 FROM sqlite_master WHERE name='document_contract_link_rules'"
    ).fetchone() is None


def _seed(connection, files, *, body=_ISSUER, codes=("600001",)):
    item = Disclosure("cninfo", "progress", codes, "关于签订重大合同的进展公告", _NOW, "", "record")
    # This typed issuer payload represents what the former parser could derive
    # even when the actual stored body described a subsidiary instead.
    candidate = ExtractCompanyLinkCandidate(CompanyDocument(item, _ISSUER, True), _NOW, CompanyEventPolicyFixture())
    assert candidate is not None
    digest = files.write(body)
    connection.run(
        "INSERT INTO disclosures VALUES(1,?,?,?,?,1,1,?,?,?)",
        (item.source, item.identity, item.digest, digest, item.title, item.published_at.isoformat(), item.url),
    )
    connection.run_many("INSERT INTO disclosure_codes VALUES(1,?)", ((code,) for code in codes))
    connection.run(
        "INSERT INTO document_link_candidates VALUES(?,?,?,?,?)",
        (item.source, item.identity, item.digest, _EXTRACTION, EncodeCompanyLink(candidate)),
    )
    return item, candidate, digest




def _missing(_digest):
    raise AssertionError("A proven cleaned candidate must not request material")


























def test_rule_writer_rejects_an_unassociated_code_and_wrong_body_hash(connection, tmp_path):
    files = CompanyDocumentFilesFixture(tmp_path / "text")
    item, candidate, digest = _seed(connection, files)
    WriteCompanyContractLinkRules(connection, replace(item, codes=("600002",)), digest, _EXTRACTION, candidate)
    WriteCompanyContractLinkRules(connection, item, hashlib.sha256(b"wrong body").digest(), _EXTRACTION, candidate)
    assert not ReadCompanyContractLinkRules(connection, ("600001", "600002"), _EXTRACTION)
