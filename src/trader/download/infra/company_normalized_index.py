"""Persist normalized inputs before a pure formal commit and exact acknowledgement."""

from contextlib import contextmanager
from collections.abc import Iterator, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.download.infra.company_sync_io import CompanySyncIoBudget
from trader.download.domain.company_evidence import CompanyInputValidationFailure
from trader.download.domain.company_evidence import StructuredCompanyQuery
from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_document_links import CompanyPublishedEvidenceLink
from trader.download.infra.company_document_link_rows import ReadCompanyDocumentLinks
from trader.download.domain.company_field_results import CompanyFieldConclusion
from trader.download.domain.company_normalization import CompanyNormalizationCommit, NormalizeCompanyFinancialFacts
from trader.download.domain.company_source_records import CompanyFinancialSourceObservation, CompanyFinancialSourcePage
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults
from trader.download.infra.company_facts_index import (
    CompanyInputReceipt, CompanyPublicationIdentity, ReadCompanyPublicationIdentity, SQLiteCompanyFactsIndex,
)
from trader.download.infra.company_facts_protocol import DecodeCompanyLabel, EncodeCompanyTime, EVIDENCE_SOURCES, RequireCompanyTime
from trader.download.infra.company_facts_schema import COMPANY_FACTS_SCHEMA
from trader.download.infra.company_financial_observations import ReadCompanyFinancialObservations, WriteCompanyFinancialObservations
from trader.download.infra.company_fact_inputs import BindCompanyEvidenceSource
from trader.download.infra.company_field_publication import CompanyFieldProofRulesHash
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_connection import (
    EnsureSqliteSchema, SqliteConnection, SqliteConnectionOptions, SqliteSchema, SqliteSession, SqliteTransaction,
)
from trader.infra.sqlite_policy import SqliteIoPolicy


_SCHEMA = SqliteSchema((*COMPANY_FACTS_SCHEMA.statements,
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
      PRIMARY KEY(scope,code,report_date)
    ) WITHOUT ROWID""",
    """CREATE TABLE company_round_intent (
      scope TEXT PRIMARY KEY,generation BLOB NOT NULL,parent_content_hash BLOB,
      parent_extraction_hash BLOB,parent_published_at INTEGER,confirmed_content_hash BLOB
    ) WITHOUT ROWID""",
    """CREATE TABLE company_round_generation (
      scope TEXT PRIMARY KEY,generation BLOB NOT NULL,normalized_hash BLOB NOT NULL
    ) WITHOUT ROWID""",
))


@dataclass(frozen=True, slots=True)
class CompanyNormalizationBaseline:
    facts: tuple[CompanyFacts, ...]
    fields: tuple[CompanyFieldConclusion, ...]
    receipts: tuple[CompanyInputReceipt, ...]
    sources: tuple[tuple[str, str], ...]
    identity: CompanyPublicationIdentity
    field_rules_hash: bytes
    document_links: tuple[CompanyPublishedEvidenceLink, ...] = ()
    financial_observations: tuple[CompanyFinancialSourceObservation, ...] = ()


class SQLiteCompanyNormalizedIndex:
    def __init__(
        self,
        path: Path,
        facts: SQLiteCompanyFactsIndex,
        *,
        event_policy: CompanyEventPolicy,
        sqlite_policy: SqliteIoPolicy,
        clean_material: Callable[[], None],
        cancel_requested: Callable[[], bool] | None = None,
        io_budget: CompanySyncIoBudget | None = None,
    ) -> None:
        if path.resolve() == facts.path.resolve() or path.exists() and facts.path.exists() and path.samefile(facts.path):
            raise ValueError("company_normalization_paths_must_differ")
        self.path, self._facts, self._sqlite_policy = path, facts, sqlite_policy
        self._clean_material, self._cancel_requested = clean_material, cancel_requested
        self._normalized = SQLiteCompanyFactsIndex(path, event_policy=event_policy, sqlite_policy=sqlite_policy, io_budget=io_budget, cancel_requested=cancel_requested)
        self._io_budget = io_budget

    @contextmanager
    def _session(self, path: Path, options: SqliteConnectionOptions | None = None, *,
        sqlite_policy: SqliteIoPolicy, cancel_requested: Callable[[], bool] | None = None) -> Iterator[SqliteConnection]:
        actual_options = SqliteConnectionOptions() if options is None else options
        if self._io_budget is not None:
            with self._io_budget.session(path, actual_options, sqlite_policy=sqlite_policy, cancel_requested=cancel_requested) as connection:
                yield connection
        else:
            with SqliteSession(path, actual_options, sqlite_policy=sqlite_policy, cancel_requested=cancel_requested) as connection:
                yield connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._session(self.path, SqliteConnectionOptions(foreign_keys=True), sqlite_policy=self._sqlite_policy) as connection:
            EnsureSqliteSchema(connection, _SCHEMA, allow_create=True, exact=True)
        self._facts.initialize()

    def validate_existing(self) -> None:
        if self.path.exists():
            with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
                EnsureSqliteSchema(connection, _SCHEMA, exact=True)

    def seed(self, baseline: CompanyNormalizationBaseline) -> None:
        """Accept an explicit immutable baseline once; never read or migrate its source database."""
        codes = tuple(fact.code for fact in baseline.facts)
        if (not codes or len(set(codes)) != len(codes) or any(field.code not in codes for field in baseline.fields)
            or baseline.field_rules_hash != CompanyFieldProofRulesHash()):
            raise ValueError("company_normalization_baseline_invalid")
        digest = CompanyNormalizationBaselineDigest(baseline)
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection:
            seeded = connection.run("SELECT baseline_digest,complete FROM company_normalization_seed WHERE singleton=1").fetchone()
        if seeded is not None:
            if seeded[0] != digest:
                raise ValueError("company_normalization_baseline_changed")
            if seeded[1]:
                return
        else:
            for path in (self.path, self._facts.path):
                with self._session(path, sqlite_policy=self._sqlite_policy) as connection:
                    if ReadCompanyPublicationIdentity(connection) is not None:
                        raise ValueError("company_normalization_nonempty_seed_target")
            with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection, immediate=True):
                connection.run("INSERT INTO company_normalization_seed VALUES(1,?,?,?,?,0,?)",
                               (baseline.identity.content_hash, baseline.identity.extraction_hash,
                                EncodeCompanyTime(baseline.identity.published_at), digest, baseline.field_rules_hash))
        for writer in (self._normalized, self._facts):
            with self._session(writer.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection):
                parent = ReadCompanyPublicationIdentity(connection)
                if parent is not None and (
                    ReadCompanyFactRows(connection, codes) != baseline.facts
                    or ReadCompanyFieldResults(connection, codes) != tuple(sorted(baseline.fields, key=lambda item: (item.code, item.event_id, item.field)))
                    or _receipts(connection) != tuple(sorted(baseline.receipts, key=lambda item: (
                        item.source, item.source_key, item.code, item.record_hash, item.extraction_hash,
                    )))
                    or ReadCompanyDocumentLinks(connection, codes) != tuple(sorted(baseline.document_links, key=lambda item: (item.code, item.source, item.link.evidence_id)))
                    or ReadCompanyFinancialObservations(connection, codes) != tuple(sorted(baseline.financial_observations, key=lambda item: item.business_key))
                ):
                    raise ValueError("company_normalization_partial_seed_changed")
            if parent is None:
                writer.publish(baseline.facts, dict(baseline.sources), None, max(baseline.identity.published_at, *(fact.published_at for fact in baseline.facts)),
                               receipts=baseline.receipts, field_results=baseline.fields, field_rules_hash=baseline.field_rules_hash,
                               document_links=baseline.document_links, financial_observations=baseline.financial_observations)
            else:
                writer.publish_file()
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection, immediate=True):
            connection.run("UPDATE company_normalization_seed SET complete=1 WHERE singleton=1")

    def _scope(self, query: StructuredCompanyQuery, codes: tuple[str, ...], cutoff: datetime) -> str:
        if query.component != "financial" or not codes or codes != tuple(sorted(set(codes))) or cutoff.utcoffset() is None:
            raise ValueError("company_round_scope_invalid")
        today = cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date()
        if query.start < today - timedelta(days=365) or query.end > today:
            raise ValueError("company_round_window_invalid")
        return CanonicalDigest((query.component, query.start, query.end, codes, cutoff))

    def cursor(self, query: StructuredCompanyQuery, codes: tuple[str, ...], cutoff: datetime) -> StructuredCompanyQuery | None:
        scope = self._scope(query, codes, cutoff)
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection:
            if connection.run("SELECT 1 FROM company_normalization_seed WHERE singleton=1 AND complete=1").fetchone() is None:
                raise ValueError("company_normalization_baseline_missing")
            row = connection.run("SELECT MAX(page),pages,total,SUM(raw_count) FROM company_round_page WHERE scope=?", (scope,)).require_one()
        if row[0] is None:
            return replace(query, page=1, received_rows=0, expected_rows=None)
        if row[0] == row[1]:
            if row[3] != row[2]:
                raise CompanyInputValidationFailure("company_round_count_mismatch")
            return None
        return replace(query, page=row[0] + 1, expected_rows=row[2], received_rows=row[3])

    def accept(self, query: StructuredCompanyQuery, page: CompanyFinancialSourcePage, codes: tuple[str, ...], cutoff: datetime, *, eligible_codes: tuple[str, ...] | None = None) -> None:
        scope = self._scope(query, codes, cutoff)
        if (query.page != page.page or query.expected_rows is not None and page.total != query.expected_rows
            or any(item.observed_at != cutoff or not query.start <= item.source_notice_at.date() <= query.end for item in page.observations)):
            raise CompanyInputValidationFailure("company_round_page_identity_mismatch")
        self._check_cancelled()
        with self._session(self.path, SqliteConnectionOptions(foreign_keys=True), sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection, immediate=True):
            previous = connection.run("SELECT MAX(page),pages,total FROM company_round_page WHERE scope=?", (scope,)).require_one()
            if (page.page != (previous[0] or 0) + 1 or previous[0] is not None and (page.pages, page.total) != (previous[1], previous[2])):
                raise CompanyInputValidationFailure("company_round_pagination_changed")
            selected = tuple(item for item in page.observations if item.financial.code in codes and (eligible_codes is None or item.financial.code in eligible_codes))
            for item in page.observations:
                if connection.run("SELECT 1 FROM company_round_member WHERE scope=? AND code=? AND report_date=?",
                                  (scope, item.financial.code, item.financial.report_date.isoformat())).fetchone() is not None:
                    raise CompanyInputValidationFailure("company_round_duplicate_report")
            WriteCompanyFinancialObservations(connection, selected)
            connection.run_many(
                "INSERT OR IGNORE INTO company_input_receipt VALUES(?,?,?,?,?,?,?,?,?)",
                (("eastmoney_financial", f"{item.source_security_identity}:{item.financial.report_date}", item.financial.code,
                  item.record_hash, self._facts.extraction_hash, None, EncodeCompanyTime(item.observed_at), "valid", "")
                 for item in selected),
            )
            connection.run_many("INSERT INTO company_round_member VALUES(?,?,?)",
                                ((scope, item.financial.code, item.financial.report_date.isoformat()) for item in page.observations))
            connection.run("INSERT INTO company_round_page VALUES(?,?,?,?,?,?)", (scope, page.page, page.pages, page.total, page.raw_count, page.response_hash))

    def normalize(self, query: StructuredCompanyQuery, codes: tuple[str, ...], cutoff: datetime) -> tuple[str, ...]:
        scope = self._scope(query, codes, cutoff)
        if self.cursor(query, codes, cutoff) is not None:
            raise ValueError("company_round_inputs_incomplete")
        self._check_cancelled()
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection):
            _require_current_field_rules(connection)
            previous = ReadCompanyFactRows(connection, codes)
            if tuple(item.code for item in previous) != codes:
                raise ValueError("company_round_population_missing")
            members = {(row[0], row[1]) for row in connection.run("SELECT code,report_date FROM company_round_member WHERE scope=?", (scope,))}
            observations = tuple(item for item in ReadCompanyFinancialObservations(connection, codes)
                                 if (item.financial.code, item.financial.report_date.isoformat()) in members)
            changed = tuple(candidate for fact in previous if (candidate := NormalizeCompanyFinancialFacts(
                fact, tuple(item for item in observations if item.financial.code == fact.code), cutoff,
            )) is not fact)
            fields = ReadCompanyFieldResults(connection, tuple(item.code for item in changed))
            sources = _sources(connection)
            parent = ReadCompanyPublicationIdentity(connection)
        normalized_identity = self._normalized.publish(changed, dict(sources), parent, cutoff, field_results=fields, field_rules_hash=CompanyFieldProofRulesHash()).identity if changed else parent
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection, immediate=True):
            identity = ReadCompanyPublicationIdentity(connection)
            if identity is None or identity != normalized_identity:
                raise ValueError("company_normalization_identity_missing")
            generation = bytes.fromhex(CanonicalDigest((scope, _identity_values(identity), observations)))
            prior = connection.run("SELECT generation FROM company_round_generation WHERE scope=?", (scope,)).fetchone()
            if prior is not None and prior[0] != generation:
                raise ValueError("company_normalization_generation_changed")
            connection.run("INSERT OR IGNORE INTO company_round_generation VALUES(?,?,?)", (scope, generation, identity.content_hash))
        return tuple(item.code for item in changed)

    def publish_normalized(self, query: StructuredCompanyQuery, codes: tuple[str, ...], cutoff: datetime) -> tuple[str, ...]:
        """Read normalized rows only; no PDF, source, event extraction or material cleanup."""
        return self._publish_scope(self._scope(query, codes, cutoff), codes, cutoff)

    def _publish_scope(self, scope: str, codes: tuple[str, ...], cutoff: datetime, *, restore_only: bool = False) -> tuple[str, ...]:
        self._check_cancelled()
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as source, SqliteTransaction(source, immediate=True):
            if not restore_only:
                _require_current_field_rules(source)
            row = source.run("SELECT generation,normalized_hash FROM company_round_generation WHERE scope=?", (scope,)).fetchone()
            if row is None:
                raise ValueError("company_normalization_missing")
            generation = row[0]
            normalized_identity = ReadCompanyPublicationIdentity(source)
            if normalized_identity is None or normalized_identity.content_hash != row[1]:
                raise ValueError("company_normalization_scope_changed")
            with self._session(self._facts.path, sqlite_policy=self._sqlite_policy) as formal, SqliteTransaction(formal):
                parent = ReadCompanyPublicationIdentity(formal)
                marker = formal.run("SELECT generation,content_hash,extraction_hash,published_at FROM company_normalization_commit WHERE scope=?", (scope,)).fetchone()
                intent = source.run("SELECT generation,parent_content_hash,parent_extraction_hash,parent_published_at,confirmed_content_hash FROM company_round_intent WHERE scope=?", (scope,)).fetchone()
                if marker is not None:
                    if (intent is None or intent[0] != generation or marker[0] != generation or parent is None
                        or (marker[1], marker[2], marker[3]) != (parent.content_hash, parent.extraction_hash, EncodeCompanyTime(parent.published_at))):
                        raise ValueError("company_normalization_confirmation_changed")
                if restore_only and marker is None:
                    raise ValueError("company_normalization_restore_marker_missing")
                if marker is None and intent is not None and _intent_parent(tuple(intent)) != parent:
                    raise ValueError("company_normalization_parent_changed")
                facts = ReadCompanyFactRows(source, codes)
                previous = {item.code: item for item in ReadCompanyFactRows(formal, codes)}
                changed = tuple(item for item in facts if item != previous.get(item.code))
                fields = ReadCompanyFieldResults(source, codes)
                members = {(row[0], row[1]) for row in source.run("SELECT code,report_date FROM company_round_member WHERE scope=?", (scope,))}
                observations = tuple(item for item in ReadCompanyFinancialObservations(source, codes)
                                     if (item.financial.code, item.financial.report_date.isoformat()) in members)
                financial_keys = {(item.financial.code, f"{item.source_security_identity}:{item.financial.report_date}", item.record_hash)
                                  for item in observations}
                receipts = tuple(item for item in _receipts(source) if item.source != "eastmoney_financial"
                                 or (item.code, item.source_key, item.record_hash) in financial_keys)
                sources = _sources(source)
                links = ReadCompanyDocumentLinks(source,codes)
                recovery_matches = False
                if marker is not None:
                    try:
                        recovery_matches = (
                            not changed and fields == ReadCompanyFieldResults(formal, codes)
                            and (_same_field_rules(source, formal) if restore_only else _has_current_field_rules(formal))
                            and links == ReadCompanyDocumentLinks(formal,codes)
                            and set(observations).issubset(ReadCompanyFinancialObservations(formal, codes))
                            and set(receipts).issubset(_receipts(formal))
                        )
                    except ValueError as exc:
                        raise ValueError("company_normalization_confirmation_changed") from exc
            if marker is not None:
                if parent is None or not recovery_matches:
                    raise ValueError("company_normalization_confirmation_changed")
                self._facts.publish_file()
                with self._session(self._facts.path, sqlite_policy=self._sqlite_policy) as formal, SqliteTransaction(formal):
                    if ReadCompanyPublicationIdentity(formal) != parent:
                        raise ValueError("company_normalization_confirmation_changed")
                source.run("UPDATE company_round_intent SET confirmed_content_hash=? WHERE scope=?", (parent.content_hash, scope))
                return ()
            if intent is None:
                source.run("INSERT INTO company_round_intent VALUES(?,?,?,?,?,NULL)",
                           (scope, generation, parent.content_hash if parent else None,
                            parent.extraction_hash if parent else None, EncodeCompanyTime(parent.published_at) if parent else None))
        # The durable intent precedes the formal transaction. Re-open a collection
        # write lock and hold it across formal commit and acknowledgement.
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as source, SqliteTransaction(source, immediate=True):
            current = source.run("SELECT generation FROM company_round_generation WHERE scope=?", (scope,)).require_one()[0]
            if current != generation or ReadCompanyPublicationIdentity(source) != normalized_identity:
                raise ValueError("company_normalization_generation_changed")
            publication = self._facts.publish(changed, dict(sources), parent, cutoff, receipts=receipts,
                                              field_results=fields, financial_observations=observations,
                                              normalization=CompanyNormalizationCommit(scope, generation), field_rules_hash=CompanyFieldProofRulesHash(),
                                              document_links=links)
            with self._session(self._facts.path, sqlite_policy=self._sqlite_policy) as formal, SqliteTransaction(formal):
                if ReadCompanyPublicationIdentity(formal) != publication.identity:
                    raise ValueError("company_normalization_confirmation_changed")
            source.run("UPDATE company_round_intent SET confirmed_content_hash=? WHERE scope=?",
                       (publication.identity.content_hash, scope))
            return publication.changed_codes

    def recover_publication_seal(self) -> bool:
        """Restore only an exact committed head with an unacknowledged intent.

        This never starts a new publication or treats the mutable normalized
        file's checksum as authoritative. A bad ordinary formal head is refused.
        """
        if not self.path.exists() or not self._facts.path.exists():
            return False
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as source, SqliteTransaction(source):
            scopes = tuple(row[0] for row in source.run("SELECT scope FROM company_round_intent WHERE confirmed_content_hash IS NULL ORDER BY scope"))
            codes = tuple(row[0] for row in source.run("SELECT code FROM company_facts ORDER BY code"))
        if len(scopes) != 1 or not codes:
            return False
        with self._session(self._facts.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as formal:
            identity = ReadCompanyPublicationIdentity(formal)
            marker = formal.run("SELECT 1 FROM company_normalization_commit WHERE scope=?", (scopes[0],)).fetchone()
        if identity is None or marker is None:
            return False
        self._publish_scope(scopes[0], codes, identity.published_at, restore_only=True)
        return True

    def publish_fields(
        self,
        codes: tuple[str, ...],
        fields: tuple[CompanyFieldConclusion, ...],
        receipts: tuple[CompanyInputReceipt, ...],
        cutoff: datetime,
        *,
        field_rules_hash: bytes,
    ) -> tuple[str, ...]:
        """Publish each requested issuer's complete prepared field snapshot; never repair a document."""
        if (not codes or codes != tuple(sorted(set(codes))) or cutoff.utcoffset() is None
            or any(item.code not in codes for item in fields) or any(item.code not in codes for item in receipts)
            or field_rules_hash != CompanyFieldProofRulesHash()):
            raise ValueError("company_normalization_fields_invalid")
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection):
            _require_current_field_rules(connection)
            if tuple(fact.code for fact in ReadCompanyFactRows(connection, codes)) != codes:
                raise ValueError("company_round_population_missing")
            previous = ReadCompanyFieldResults(connection, codes)
            ordered = tuple(sorted(fields, key=lambda item: (item.code, item.event_id, item.field)))
            parent, sources = ReadCompanyPublicationIdentity(connection), _sources(connection)
            if parent is None:
                raise ValueError("company_normalization_identity_missing")
            matches = previous == ordered and set(receipts).issubset(_receipts(connection))
            scope = CanonicalDigest(("field_proof", codes, cutoff, field_rules_hash.hex(), _prepared_digest(ordered, receipts)))
            generation_row = connection.run("SELECT generation FROM company_round_generation WHERE scope=?", (scope,)).fetchone()
            if matches and generation_row is None:
                with self._session(self._facts.path, sqlite_policy=self._sqlite_policy) as formal, SqliteTransaction(formal):
                    if _has_current_field_rules(formal) and ordered == ReadCompanyFieldResults(formal, codes) and set(receipts).issubset(_receipts(formal)):
                        return ()
        if generation_row is not None:
            return self._publish_scope(scope, codes, cutoff)
        self._check_cancelled()
        identity = parent if matches else self._normalized.publish((), dict(sources), parent, cutoff,
                                            receipts=receipts, field_results=ordered, field_rules_hash=field_rules_hash).identity
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection, immediate=True):
            if ReadCompanyPublicationIdentity(connection) != identity:
                raise ValueError("company_normalization_generation_changed")
            generation = bytes.fromhex(CanonicalDigest((scope, _identity_values(identity))))
            connection.run("INSERT OR IGNORE INTO company_round_generation VALUES(?,?,?)",
                           (scope, generation, identity.content_hash))
        return self._publish_scope(scope, codes, cutoff)

    def clean(self) -> None:
        self._clean_material()

    def publish_prepared(
        self, codes: tuple[str, ...], facts: tuple[CompanyFacts, ...],
        fields: tuple[CompanyFieldConclusion, ...], receipts: tuple[CompanyInputReceipt, ...],
        links: tuple[CompanyPublishedEvidenceLink, ...], cutoff: datetime, *,
        field_rules_hash: bytes, sources: tuple[tuple[str, str], ...] = (),
    ) -> tuple[str, ...]:
        """Stage complete typed issuer snapshots, then use the sole formal publisher.

        Parsing, source qualification and proof preparation finish before this
        call. No document recovery, network or maintenance belongs to commit.
        """
        if (not codes or codes != tuple(sorted(set(codes))) or cutoff.utcoffset() is None
            or any(item.code not in codes for item in facts)
            or any(item.code not in codes for item in fields)
            or any(item.code not in codes for item in receipts)
            or any(item.code not in codes for item in links)
            or field_rules_hash != CompanyFieldProofRulesHash()):
            raise ValueError("company_normalization_prepared_invalid")
        ordered = tuple(sorted(fields, key=lambda item: (item.code, item.event_id, item.field)))
        scope = CanonicalDigest(("prepared", codes, cutoff, facts, _prepared_digest(ordered, receipts), links, sources, field_rules_hash.hex()))
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection):
            parent = ReadCompanyPublicationIdentity(connection)
            if parent is None:
                raise ValueError("company_normalization_identity_missing")
            generation = connection.run("SELECT generation FROM company_round_generation WHERE scope=?", (scope,)).fetchone()
            previous = ReadCompanyFactRows(connection, codes)
            changed = tuple(item for item in facts if item not in previous)
            source_pairs = _sources(connection)
        if generation is not None:
            return self._publish_scope(scope, codes, cutoff)
        self._check_cancelled()
        bound_sources = dict(source_pairs)
        for key, supplier in sources:
            BindCompanyEvidenceSource(bound_sources, key, supplier)
        identity = self._normalized.publish(changed, bound_sources, parent, cutoff,
            receipts=receipts, field_results=ordered, field_rules_hash=field_rules_hash, document_links=links).identity
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection, immediate=True):
            if ReadCompanyPublicationIdentity(connection) != identity:
                raise ValueError("company_normalization_generation_changed")
            # The sole writer has checked the complete field key set whenever
            # the global rule identity changed. Only that commit renews seed's
            # prepared-field identity; a partial caller cannot self-sign it.
            connection.run("UPDATE company_normalization_seed SET field_rules_hash=? WHERE singleton=1 AND complete=1", (field_rules_hash,))
            connection.run("INSERT INTO company_round_generation VALUES(?,?,?)",
                (scope, bytes.fromhex(CanonicalDigest((scope, _identity_values(identity)))), identity.content_hash))
        return self._publish_scope(scope, codes, cutoff)

    def _check_cancelled(self) -> None:
        if self._io_budget is not None:
            self._io_budget.check()
        if self._cancel_requested is not None and self._cancel_requested():
            raise InterruptedError("company_round_cancelled")


def _intent_parent(row: tuple[object, ...]) -> CompanyPublicationIdentity | None:
    if row[1] is None:
        return None
    if not isinstance(row[1], bytes) or not isinstance(row[2], bytes):
        raise ValueError("company_normalization_intent_invalid")
    return CompanyPublicationIdentity(row[1], row[2], RequireCompanyTime(row[3]))


def _has_current_field_rules(connection: SqliteConnection) -> bool:
    row = connection.run("SELECT rules_hash FROM company_field_proof_rule WHERE singleton=1").fetchone()
    return row is not None and row[0] == CompanyFieldProofRulesHash()


def _same_field_rules(source: SqliteConnection, formal: SqliteConnection) -> bool:
    source_rule = source.run("SELECT rules_hash FROM company_field_proof_rule WHERE singleton=1").fetchone()
    formal_rule = formal.run("SELECT rules_hash FROM company_field_proof_rule WHERE singleton=1").fetchone()
    seed = source.run("SELECT field_rules_hash FROM company_normalization_seed WHERE singleton=1 AND complete=1").fetchone()
    return source_rule is not None and formal_rule is not None and seed is not None and source_rule[0] == formal_rule[0] == seed[0]


def _require_current_field_rules(connection: SqliteConnection) -> None:
    row = connection.run("SELECT field_rules_hash FROM company_normalization_seed WHERE singleton=1 AND complete=1").fetchone()
    if row is None or row[0] != CompanyFieldProofRulesHash() or not _has_current_field_rules(connection):
        raise ValueError("company_normalization_field_rules_changed")


def _sources(connection: SqliteConnection) -> tuple[tuple[str, str], ...]:
    return tuple((row[0], DecodeCompanyLabel(EVIDENCE_SOURCES, row[1])) for row in connection.run(
        "SELECT source_key,source FROM company_evidence_ref ORDER BY source_key",
    ))


def _receipts(connection: SqliteConnection) -> tuple[CompanyInputReceipt, ...]:
    return tuple(CompanyInputReceipt(row[0], row[1], row[2], row[3], row[4], row[5], RequireCompanyTime(row[6]), row[7], row[8])
                 for row in connection.run("SELECT * FROM company_input_receipt ORDER BY source,source_key,code,record_hash,extraction_hash"))


def _identity_values(identity: CompanyPublicationIdentity) -> tuple[str, str, datetime]:
    return identity.content_hash.hex(), identity.extraction_hash.hex(), identity.published_at


def _prepared_digest(
    conclusions: tuple[CompanyFieldConclusion, ...], inputs: tuple[CompanyInputReceipt, ...],
) -> str:
    fields = tuple((item.code, item.event_id, item.field, item.value, item.state, item.applicable, item.reason,
                    tuple((proof.source, proof.source_key, proof.record_hash, proof.body_hash.hex(), proof.rule_id,
                           proof.published_at, proof.start_line, proof.end_line, proof.quote, proof.unit, proof.subject.value)
                          for proof in item.evidence)) for item in conclusions)
    receipts = tuple((item.source, item.source_key, item.code, item.record_hash, item.extraction_hash.hex(),
                      item.body_hash.hex() if item.body_hash is not None else None, item.published_at, item.outcome, item.policy_id)
                     for item in inputs)
    return CanonicalDigest((fields, receipts))


def CompanyNormalizationBaselineDigest(baseline: CompanyNormalizationBaseline) -> str:
    return CanonicalDigest((baseline.facts, _prepared_digest(baseline.fields, baseline.receipts), baseline.sources,
                            _identity_values(baseline.identity), baseline.field_rules_hash.hex(), baseline.document_links, baseline.financial_observations))


def ReadCompanyNormalizationBaseline(connection: SqliteConnection) -> CompanyNormalizationBaseline:
    """Decode only the current complete schema and its actual current proof stamp."""
    identity = ReadCompanyPublicationIdentity(connection)
    if identity is None or not _has_current_field_rules(connection):
        raise ValueError("company_normalization_current_baseline_required")
    codes = tuple(row[0] for row in connection.run("SELECT code FROM company_facts ORDER BY code"))
    if not 1 <= len(codes) <= 12000:
        raise ValueError("company_normalization_baseline_population_invalid")
    return CompanyNormalizationBaseline(ReadCompanyFactRows(connection, codes), ReadCompanyFieldResults(connection, codes),
        _receipts(connection), _sources(connection), identity, CompanyFieldProofRulesHash(),
        ReadCompanyDocumentLinks(connection, codes), ReadCompanyFinancialObservations(connection, codes))


__all__ = ["CompanyNormalizationBaseline", "CompanyNormalizationBaselineDigest", "ReadCompanyNormalizationBaseline", "SQLiteCompanyNormalizedIndex"]
