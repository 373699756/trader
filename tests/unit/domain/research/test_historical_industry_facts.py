from datetime import date, datetime
from zoneinfo import ZoneInfo

from trader.domain.research.historical_industry_facts import (
    HistoricalIndustryFact,
    HistoricalIndustrySourceContract,
    HistoricalIndustryStockWindow,
    build_historical_industry_dataset_report,
    build_historical_industry_source_audit,
    merge_historical_industry_facts,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
QUERIED_AT = datetime(2026, 9, 9, 12, tzinfo=SHANGHAI)


def _fact(source: str, industry: str = "银行") -> HistoricalIndustryFact:
    return HistoricalIndustryFact(
        source=source,
        source_version="2026-09-09",
        code="000001",
        industry=industry,
        classification="申万一级行业",
        effective_from=date(2020, 1, 1),
        effective_to=None,
        queried_at=QUERIED_AT,
        evidence_hash=("a" if source == "source_a" else "b") * 64,
    )


def test_consistent_sources_merge_evidence_but_conflicts_fail_closed() -> None:
    consistent = merge_historical_industry_facts((_fact("source_a"), _fact("source_b")))

    assert consistent.conflict_count == 0
    assert len(consistent.facts) == 1
    assert consistent.facts[0].sources == ("source_a", "source_b")
    assert len(consistent.facts[0].evidence_hashes) == 2

    conflicting = merge_historical_industry_facts((_fact("source_a"), _fact("source_b", "房地产")))

    assert conflicting.facts == ()
    assert conflicting.conflict_count == 1


def test_source_source_audit_uses_actual_dates_and_stratifies_the_full_sample() -> None:
    facts = []
    windows = []
    for index in range(300):
        code = f"{index + 1:06d}"
        board = ("main", "chinext", "star")[index % 3]
        cohort = ("old", "new", "delisted")[index % 3]
        windows.append(HistoricalIndustryStockWindow(code, board, cohort, (date(2020, 1, 2), date(2020, 1, 3))))
        facts.append(
            HistoricalIndustryFact(
                "qualified_source",
                "2026-09-09",
                code,
                "银行",
                "申万一级行业",
                date(2020, 1, 1),
                None,
                QUERIED_AT,
                f"{index + 1:064x}",
            )
        )
    contract = HistoricalIndustrySourceContract(
        "qualified_source",
        "2026-09-09",
        code_available=True,
        industry_available=True,
        classification_available=True,
        effective_from_available=True,
        effective_to_available=True,
        queried_at_available=True,
        source_identity_available=True,
    )

    audit = build_historical_industry_source_audit(contract, tuple(facts), tuple(windows))

    assert audit.status == "qualified"
    assert audit.sampled_codes == 300
    assert audit.eligible_codes == tuple(f"{index + 1:06d}" for index in range(300))
    assert audit.actual_trading_dates == 600
    assert audit.covered_trading_dates == 600
    assert audit.missing_dates == 0
    assert audit.conflict_dates == 0
    assert audit.time_travel_dates == 0
    assert audit.board_counts == (("chinext", 100), ("main", 100), ("star", 100))
    assert audit.cohort_counts == (("delisted", 100), ("new", 100), ("old", 100))
    assert len(audit.fact_hashes) == 300
    assert len(audit.dataset_hash) == 64

    report = build_historical_industry_dataset_report(
        "c" * 64,
        (audit,),
        merge_historical_industry_facts(tuple(facts)),
    )
    assert report.status == "qualified"
    assert report.training_authority is True
    assert report.production_authority is False


def test_cross_source_conflict_keeps_an_otherwise_qualified_dataset_closed() -> None:
    facts = []
    windows = []
    for index in range(300):
        code = f"{index + 1:06d}"
        windows.append(HistoricalIndustryStockWindow(code, "main", "old", (date(2020, 1, 2),)))
        facts.append(
            HistoricalIndustryFact(
                "qualified_source",
                "2026-09-09",
                code,
                "银行",
                "申万一级行业",
                date(2020, 1, 1),
                None,
                QUERIED_AT,
                f"{index + 1:064x}",
            )
        )
    contract = HistoricalIndustrySourceContract(
        "qualified_source",
        "2026-09-09",
        True,
        True,
        True,
        True,
        True,
        True,
        True,
    )
    audit = build_historical_industry_source_audit(contract, tuple(facts), tuple(windows))
    conflict = _fact("source_b", "房地产")
    first = HistoricalIndustryFact(
        "source_a",
        "2026-09-09",
        conflict.code,
        "银行",
        conflict.classification,
        conflict.effective_from,
        conflict.effective_to,
        conflict.queried_at,
        "c" * 64,
    )

    report = build_historical_industry_dataset_report(
        "c" * 64,
        (audit,),
        merge_historical_industry_facts((first, conflict)),
    )

    assert report.status == "historical_data_insufficient"
    assert report.cross_source_conflicts == 1
    assert report.eligible_codes == ()
    assert report.training_authority is False
    assert "historical_industry_cross_source_conflict" in report.failure_reasons


def test_missing_query_time_keeps_archived_facts_out_of_training_readiness() -> None:
    fact = HistoricalIndustryFact(
        "baostock_archived_industry",
        "00.9.30",
        "000001",
        "银行",
        "证监会行业分类",
        date(2020, 1, 1),
        None,
        None,
        "a" * 64,
    )
    contract = HistoricalIndustrySourceContract(
        "baostock_archived_industry",
        "00.9.30",
        code_available=True,
        industry_available=True,
        classification_available=True,
        effective_from_available=True,
        effective_to_available=True,
        queried_at_available=False,
        source_identity_available=True,
    )
    window = HistoricalIndustryStockWindow("000001", "main", "old", (date(2020, 1, 2),))

    audit = build_historical_industry_source_audit(contract, (fact,), (window,))

    assert audit.status == "historical_data_insufficient"
    assert audit.eligible_codes == ()
    assert audit.covered_trading_dates == 1
    assert "industry_query_time_unavailable" in audit.failure_reasons


def test_claimed_query_time_capability_rejects_a_fact_without_query_time() -> None:
    fact = HistoricalIndustryFact(
        "candidate_source",
        "2026-09-09",
        "000001",
        "银行",
        "申万一级行业",
        date(2020, 1, 1),
        None,
        None,
        "a" * 64,
    )
    contract = HistoricalIndustrySourceContract(
        "candidate_source",
        "2026-09-09",
        code_available=True,
        industry_available=True,
        classification_available=True,
        effective_from_available=True,
        effective_to_available=True,
        queried_at_available=True,
        source_identity_available=True,
    )
    window = HistoricalIndustryStockWindow("000001", "main", "old", (date(2020, 1, 2),))

    audit = build_historical_industry_source_audit(contract, (fact,), (window,))

    assert audit.status == "historical_data_insufficient"
    assert audit.stocks[0].eligible is False
    assert "industry_query_time_unavailable" in audit.failure_reasons
