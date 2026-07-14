from __future__ import annotations

from stock_analyzer import config
from stock_analyzer.risk_blacklist import attach_risk_blacklist, load_risk_blacklist
from stock_analyzer.scoring import candidate_filter_report, prepare_candidates, score_today_candidates
from stock_analyzer.sentiment import score_news_items


def test_prepare_candidates_keeps_star_market_and_filters_st(quotes, codes):
    raw = quotes(
        [
            {"code": "688001", "name": "科创样本", "price": 20, "pct_chg": 6, "turnover": 90000000},
            {"code": "300001", "name": "创业样本", "price": 10, "pct_chg": 4, "turnover": 80000000},
            {"code": "430001", "name": "北交样本", "price": 10, "pct_chg": 4, "turnover": 80000000},
            {"code": "600001", "name": "ST样本", "price": 10, "pct_chg": 4, "turnover": 80000000},
        ]
    )

    result = prepare_candidates(raw)

    assert codes(result) == {"688001", "300001"}
    assert result[result["code"] == "688001"].iloc[0]["market"] == "star"


def test_prepare_candidates_filters_near_limit_up_unbuyable_names(quotes, codes):
    raw = quotes(
        [
            {"code": "600001", "name": "主板可买", "price": 10, "pct_chg": 6.5, "turnover": 90000000},
            {"code": "600002", "name": "主板过高", "price": 10, "pct_chg": 9.2, "turnover": 90000000},
            {"code": "300001", "name": "创业可买", "price": 10, "pct_chg": 9.5, "turnover": 90000000},
            {"code": "300002", "name": "创业过高", "price": 10, "pct_chg": 18.5, "turnover": 90000000},
        ]
    )

    result = prepare_candidates(raw)

    assert codes(result) == {"600001", "300001"}


def test_prepare_candidates_coerces_fundamental_fields(quotes):
    raw = quotes(
        [
            {
                "code": "600001",
                "name": "x",
                "price": 10,
                "pct_chg": 2,
                "turnover": 9e8,
                "float_market_cap": 5e9,
                "market_cap": 6e9,
                "pe_dynamic": 18,
                "pb": 2.1,
            }
        ]
    )

    result = prepare_candidates(raw)

    for column in ("float_market_cap", "market_cap", "pe_dynamic", "pb"):
        assert column in result.columns
    assert float(result.iloc[0]["pe_dynamic"]) == 18.0


def test_risk_blacklist_hard_filters_json_high_risk(quotes, codes, risk_blacklist_files):
    risk_blacklist_files["write_json"](
        risk_blacklist_files["json"],
        {
            "items": {
                "600001": {
                    "name": "风险样本",
                    "level": "critical",
                    "category": "financial_fraud",
                    "reason": "历史财务造假测试",
                    "hard_exclude": True,
                }
            }
        },
    )
    raw = quotes(
        [
            {"code": "600001", "name": "风险样本", "price": 10, "pct_chg": 3, "turnover": 100000000},
            {"code": "600002", "name": "正常样本", "price": 10, "pct_chg": 3, "turnover": 100000000},
        ]
    )

    payload = load_risk_blacklist()
    result = attach_risk_blacklist(prepare_candidates(raw), payload)

    assert codes(result) == {"600002"}
    assert payload["items"]["600001"]["flags"][0]["label"] == "历史财务造假测试"


def test_risk_blacklist_medium_marks_without_filtering(quotes, codes, risk_blacklist_files, monkeypatch):
    risk_blacklist_files["write_text"](
        risk_blacklist_files["csv"],
        "code,name,level,category,reason,hard_exclude\n"
        "600001,风险样本,medium,negative_history,历史负面测试,false\n",
    )
    monkeypatch.setattr(config, "RISK_BLACKLIST_PATH", "")
    monkeypatch.setattr(config, "RISK_BLACKLIST_CSV_PATH", str(risk_blacklist_files["csv"]))
    raw = quotes(
        [
            {"code": "600001", "name": "风险样本", "price": 10, "pct_chg": 3, "turnover": 100000000},
        ]
    )

    payload = load_risk_blacklist()
    result = attach_risk_blacklist(prepare_candidates(raw), payload)

    assert codes(result) == {"600001"}
    assert result.iloc[0]["blacklist_risk_level"] == "medium"
    assert not bool(result.iloc[0]["blacklist_hard_exclude"])


def test_risk_blacklist_ignores_entries_without_code(risk_blacklist_files):
    risk_blacklist_files["write_json"](
        risk_blacklist_files["json"],
        {
            "items": [
                {
                    "name": "缺代码样本",
                    "level": "critical",
                    "category": "financial_fraud",
                    "reason": "缺少股票代码",
                }
            ]
        },
    )

    payload = load_risk_blacklist()

    assert payload["items"] == {}
    assert payload["status"] == "empty"


def test_risk_blacklist_noops_when_code_column_missing(quotes):
    raw = quotes([{"name": "无代码样本", "price": 10}])

    result = attach_risk_blacklist(raw, {"status": "ok", "items": {}})

    assert result.to_dict("records") == raw.to_dict("records")
    assert result is not raw


def test_candidate_filter_report_matches_prepare_candidates(quotes, codes):
    raw = quotes(
        [
            {"code": "600001", "name": "正常样本", "price": 10, "pct_chg": 3, "turnover": 100000000},
            {"code": "430001", "name": "北交样本", "price": 10, "pct_chg": 3, "turnover": 100000000},
            {"code": "600002", "name": "ST样本", "price": 10, "pct_chg": 3, "turnover": 100000000},
            {"code": "600003", "name": "低流动", "price": 10, "pct_chg": 3, "turnover": 1000},
            {"code": "600004", "name": "涨幅过高", "price": 10, "pct_chg": 13, "turnover": 100000000},
            {"code": "600005", "name": "接近涨停", "price": 10, "pct_chg": 9.5, "turnover": 100000000},
        ]
    )

    prepared = prepare_candidates(raw)
    report = candidate_filter_report(raw)

    assert report["raw_count"] == len(raw)
    assert report["passed_count"] == len(prepared)
    assert report["rejected_count"] == len(raw) - len(prepared)
    assert codes(prepared) == {"600001"}
    reason_keys = {item["key"] for item in report["reasons"]}
    assert "unsupported_code" in reason_keys
    assert "special_treatment" in reason_keys
    assert "min_turnover" in reason_keys
    assert "max_gain" in reason_keys
    assert "buyable_gain" in reason_keys


def test_sentiment_scores_positive_and_negative_words():
    positive = score_news_items([{"title": "公司中标大订单", "content": "", "publish_time": ""}])
    negative = score_news_items([{"title": "公司被立案调查并收到处罚", "content": "", "publish_time": ""}])

    assert positive["score"] > 50
    assert negative["score"] < 50
    assert "立案" in negative["risk_words"]


def test_score_candidates_orders_by_combined_signal(quotes):
    raw = quotes(
        [
            {
                "code": "600001",
                "name": "强势样本",
                "price": 12,
                "pct_chg": 6,
                "speed": 2,
                "volume_ratio": 3,
                "turnover_rate": 8,
                "turnover": 300000000,
                "industry": "半导体",
            },
            {
                "code": "600002",
                "name": "普通样本",
                "price": 10,
                "pct_chg": 2,
                "speed": 0.2,
                "volume_ratio": 1,
                "turnover_rate": 2,
                "turnover": 60000000,
                "industry": "银行",
            },
        ]
    )
    candidates = prepare_candidates(raw)

    result, _ = score_today_candidates(
        candidates,
        hot_ranks={"600001": 10},
        industry_strength={"半导体": 2.5, "银行": -0.2},
        sentiment_lookup={"600001": {"score": 70, "summary": "舆情偏正面"}},
        top_n=2,
    )

    rows = result["short_term"]
    assert rows[0]["code"] == "600001"
    assert rows[0]["score"] > 0
