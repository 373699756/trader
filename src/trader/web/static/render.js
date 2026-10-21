(function () {
  "use strict";

  const ACTION_LABELS = {
    executable: "可执行",
    observe: "观察",
    unavailable: "不可执行",
  };

  const SOURCE_LABELS = {
    unavailable: "行情暂不可用",
    long_watchlist: "长期观察名单",
    tencent: "腾讯行情",
    tencent_market: "腾讯行情",
    eastmoney: "东方财富",
    sina: "新浪行情",
    tushare: "专业行情",
    akshare: "公开行情",
    company_evidence: "本地公司证据",
  };

  const ACTION_REASON_LABELS = {
    long_watch_only: "长期观察池",
    fixed_long_watchlist: "固定长期观察名单",
    risk_veto: "风险事实触发限制",
    stale_quote: "行情已过期，仅供观察",
    future_quote: "行情时间待校验",
    invalid_price: "行情价格待更新",
    invalid_amount: "成交额待更新",
    invalid_quote_time: "行情时间待校验",
    invalid_quote_structure: "行情结构待校验",
    invalid_pct_change: "涨跌幅待更新",
    invalid_cross_source_deviation: "跨源行情待复核",
    missing_liquidity_history: "流动性历史待下载/更新",
    invalid_liquidity_history: "流动性历史待校验",
    insufficient_core_features: "核心数据不足，仅供观察",
    corporate_risk_window_unavailable: "近一年公司风险覆盖不足",
    corporate_risk_history_unavailable: "公司风险历史暂不可核验",
    board_data_reliability_below_threshold: "板块数据可靠度不足",
    observation_window: "当前处于观察时段",
    outside_execution_window: "当前不在执行时段",
    score_threshold_met: "评分达到执行门槛",
    near_score_threshold: "接近执行门槛，继续观察",
    below_score_threshold: "评分未达到执行门槛",
    model_net_utility_non_positive: "成本后净超额未转正",
    board_concentration_limit: "超过板块集中度上限",
    industry_limit: "超过行业集中度上限",
    top_k_limit: "位于正式候选池名额之外",
    observation_limit: "位于观察候选池名额之外",
    pending_merge: "等待评分合并",
  };

  const SETUP_LABELS = {
    shrink_pullback: "缩量回踩",
    volume_breakout: "放量突破",
    trend_unconfirmed: "趋势成立，入场待确认",
    none: "形态未成立",
  };

  const DOWNSIDE_REASON_LABELS = {
    downside_inputs_missing: "下行保护输入不完整",
    downside_inputs_invalid: "下行保护输入数值非法",
    downside_inputs_stale: "下行保护输入已过期",
    intraday_reversal_atr: "日内回撤超过 1 ATR",
    trend_breakdown: "趋势结构破位",
    low_stability_tail: "波动与历史回撤均处尾部",
    risk_off_weak_close: "弱市且尾盘/收盘偏弱",
  };

  const RISK_LABELS = {
    near_limit_crowding: "接近涨跌幅限制",
    price_volume_divergence: "价格与量能背离",
    high_volatility: "波动率偏高",
    short_term_overheat: "短期过热",
    intraday_reversal: "日内冲高回落",
    liquidity_contraction: "流动性收缩",
    trend_breakdown: "趋势破位",
    financial_deterioration: "财务恶化",
    pledge_risk: "质押风险",
    reduction_or_unlock: "减持或解禁风险",
    negative_announcement_level: "负面公告风险",
    major_shareholder_reduction: "大股东减持风险",
    financial_fraud_history: "财务造假历史",
    official_investigation_history: "立案调查历史",
    major_illegal_history: "重大违法历史",
    fund_occupation_history: "资金占用历史",
    illegal_guarantee_history: "违规担保历史",
    forced_delisting_risk: "强制退市风险",
    unlock_risk: "限售股解禁风险",
    corporate_risk_window_unavailable: "近一年公司风险覆盖不足",
    corporate_risk_history_unavailable: "公司风险历史暂不可核验",
  };

  const REVIEW_ERROR_LABELS = {
    api_key_missing: "不可用：未配置 API 密钥",
    disabled: "不可用：DeepSeek 已禁用",
    budget_exhausted: "未复核：调用额度已用尽",
    bucket_limit: "未复核：策略额度已用尽",
    stage_limit: "未复核：阶段额度已用尽",
    daily_hard_limit: "未复核：每日额度已用尽",
    deadline_reached: "未复核：已到复核截止时间",
    completed_after_deadline: "迟到：结果未参与评分",
  };

  const DEGRADED_REASON_LABELS = {
    snapshot_not_ready: "荐股快照尚未就绪",
    board_data_reliability_below_threshold: "板块数据可靠度不足",
    board_population_insufficient: "板块有效样本不足",
    deepseek_skipped_no_eligible_candidates: "没有符合模型复核条件的候选",
    deepseek_incomplete: "模型复核结果不完整",
    deepseek_deferred_until_afternoon: "午后开始增强模型复核",
    close_fallback_observe_floor: "收盘补算已放宽观察展示门槛",
    deepseek_pending: "模型复核进行中",
    tomorrow_tail_data_incomplete: "尾盘量价数据不完整",
    d25_structured_research_incomplete: "2至5日结构化研究数据不完整",
    corporate_risk_window_unavailable: "近一年公司风险覆盖不足",
    corporate_risk_history_unavailable: "公司风险历史暂不可核验",
    strategy_history_coverage_partial: "候选历史数据覆盖不完整",
    structured_risk_unavailable: "结构化风险数据尚未就绪",
    cross_source_deviation: "跨源价格校验存在偏差",
    history_data_degraded: "历史行情数据可靠度不足",
    model_unavailable: "模型服务暂不可用",
    no_positive_net_utility: "模型预测成本后净超额均未转正",
    quote_fallback: "行情已使用备用数据",
  };

  const BOARD_LABELS = {
    main: "主板",
    chinext: "创业板",
    star: "科创板",
  };

  const FUSION_MODE_LABELS = {
    hybrid: "本地评分 · 已完成风险复核",
    local_degraded: "本地评分模式",
    local_only: "仅本地评分",
  };

  const PIPELINE_REASON_LABELS = {
    local_only: "仅使用本地评分", hybrid: "本地评分与 DeepSeek 风险复核已合并",
    intraday_reversal_atr: "日内回撤超过近期正常波动幅度",
    static_baseline_loaded: "已加载证券基础资料", static_baseline_reused: "沿用已加载的证券基础资料",
    static_identity_pending: "证券基础资料待补充",
    static_reference_unavailable: "官方证券总体尚未取得", static_reference_degraded: "官方证券总体刷新失败或已过期",
    data_pending: "数据待补充", refresh_pending: "行情待刷新", quality_pending: "评分输入待补充",
    quality_invalid: "评分输入无效", history_projection_loading: "正在加载本地历史数据",
    historical_st: "历史记录存在特别处理风险", historical_audited_loss: "历史规则：曾有正式年度亏损",
    latest_disclosed_report_loss: "最新一份正式财报亏损",
    historical_delisting_warning: "历史记录存在退市预警", confirmed_financial_fraud: "已确认财务造假",
    confirmed_major_illegal: "已确认重大违法", confirmed_fund_occupation: "已确认资金占用",
    confirmed_illegal_guarantee: "已确认违规担保", confirmed_forced_delisting: "已确认强制退市风险",
    manual_permanent_blacklist: "人工列入长期排除名单", permanent_excluded: "不符合长期资格要求",
    st_or_delisting: "特别处理或退市风险", blacklisted: "命中排除名单",
    permanent_structured_negative_risk: "已确认长期负面风险", dynamic_structured_negative_risk: "已确认当前负面风险",
    unsupported_code: "不属于支持的沪深股票范围", suspended: "停牌",
    insufficient_liquidity: "成交活跃度不足", one_price_limit: "一字涨跌停",
    new_listing_session: "新股上市交易日不足", relisted_first_session: "重新上市首个交易日",
    delisting_period_first_session: "退市整理期首个交易日", major_regulatory_risk: "重大监管风险",
    main_board_too_hot: "主板涨幅超过限制", chinext_board_too_hot: "创业板涨幅超过限制",
    star_board_too_hot: "科创板涨幅超过限制", board_classification_conflict: "板块分类存在冲突",
    board_identity_degraded: "板块分类资料不完整", missing_listing_date: "上市日期缺失",
    missing_listing_age_sessions: "上市交易日数缺失", invalid_pct_change_value: "涨跌幅数据无效",
    board_limit: "超过板块候选名额", board_candidate_limit: "超过板块候选名额",
    deepseek_eligible: "席内可复核", deepseek_submitted: "提交复核", deepseek_completed: "复核完成", deepseek_cache_reused: "复用已有证据", deepseek_veto: "证据否决", deepseek_source_failed: "复核来源失败",
    strategy_history_insufficient: "策略所需历史交易日不足", production_model_features_missing: "模型输入字段缺失",
    candidate_quotes_pending: "候选行情待补充", security_master_coverage_incomplete: "证券基础资料覆盖不完整",
    below_score_threshold: "最终分低于观察线", filter_observe_only: "数据或风险限制，仅供观察",
    observation_phase: "当前时段仅供观察", downside_guard: "下行保护触发，仅供观察",
    not_scored: "尚未完成评分", deepseek_applied: "DeepSeek 复核已应用",
    deepseek_abstain: "DeepSeek 未给出可用结论，保留本地推荐", deepseek_failed: "DeepSeek 复核失败，保留本地推荐",
    deepseek_rejected: "DeepSeek 复核未通过校验", deepseek_late: "DeepSeek 复核迟到，未影响推荐",
    deepseek_review_unavailable: "DeepSeek 复核暂不可用", deepseek_manifest_mismatch: "复核证据与当前输入不一致",
    deepseek_manifest_validation_failed: "复核证据校验失败", deepseek_review_time_invalid: "复核时间无效",
    completed: "操作已完成", accepted: "已接纳当前结果", enqueued: "已加入待处理队列",
    observer_capacity_rejected: "研究观察队列已满", formal_found: "已找到当日正式记录",
    formal_missing: "尚无当日正式记录", checkpoint_found: "已找到恢复检查点",
    checkpoint_missing: "尚无恢复检查点", checkpoint_ineligible: "检查点不符合恢复条件",
    already_frozen: "已有冻结结果，沿用原记录", already_sealed: "已经封存，沿用原结果",
    sealed: "结果已封存", frozen: "冻结结果已发布", scheduled_freeze_pending: "等待规定冻结时点",
    index_restore_conflict: "恢复结果与当前版本冲突", index_commit_conflict: "冻结结果与当前版本冲突",
    cas_mismatch: "结果版本已变化，本次未接纳", stale_trade_date: "交易日期已过期",
    stale_sequence: "结果已被更新版本替代", sequence_conflict: "同一批次结果版本冲突",
    hybrid_parent_mismatch: "融合结果对应的本地评分版本不一致", quote_anchor_missing: "原始报价缺失",
    parent_mismatch: "当前结果与原评分版本不一致", trade_date_mismatch: "交易日期不一致",
    stale_overlay: "报价更新已过期", quote_scope_mismatch: "报价股票范围不一致",
    overlay_cas_mismatch: "报价所对应的评分版本已变化", overlay_conflict: "报价更新版本冲突",
    quote_identity_mismatch: "报价版本不一致",
    io_oserror: "文件或数据库读写失败", io_timeouterror: "读写操作超时",
    io_runtimeerror: "发布或恢复操作失败", io_valueerror: "操作数据校验失败",
  };

  const PIPELINE_FACET_LABELS = {
    filter_pass: "通过过滤", filter_observe: "仅供观察", filter_reject: "被过滤",
    board_main: "主板候选", board_chinext: "创业板候选", board_star: "科创板候选",
    candidate_features: "候选行情与特征", security_master: "证券基础资料", history: "历史行情",
    review_completed: "已完成复核", local_only: "仅本地评分", hybrid: "已完成风险复核",
    executable_threshold_met: "达到执行线", observation_threshold_met: "达到观察线",
    action_executable: "可执行", action_observe: "仅观察", action_unavailable: "不可入选",
    selected_executable: "正式入选", selected_observe: "观察入选",
  };

  function pipelineReasonLabel(value) {
    const reason = String(value || "").trim();
    const label = PIPELINE_REASON_LABELS[reason] || ACTION_REASON_LABELS[reason]
      || DEGRADED_REASON_LABELS[reason] || RISK_LABELS[reason] || DOWNSIDE_REASON_LABELS[reason]
      || REVIEW_ERROR_LABELS[reason];
    if (label) return label;
    if (reason.startsWith("downside_guard:")) return "下行保护触发，仅供观察";
    if (reason.startsWith("io_")) return "读写或发布操作失败";
    if (/[\u3400-\u9fff]/u.test(reason)) return reason;
    return "原因说明暂未提供";
  }

  function pipelineFacetLabel(value) {
    return PIPELINE_FACET_LABELS[value] || "其他处理结果";
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function number(value, digits) {
    if (!hasValue(value)) return "-";
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return "-";
    return parsed.toLocaleString("zh-CN", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

  function compact(value) {
    if (!hasValue(value)) return "-";
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return "-";
    const absolute = Math.abs(parsed);
    if (absolute >= 100000000) return `${number(parsed / 100000000, 2)}亿`;
    if (absolute >= 10000) return `${number(parsed / 10000, 1)}万`;
    return number(parsed, 0);
  }

  function pct(value) {
    if (!hasValue(value)) return { text: "-", className: "" };
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return { text: "-", className: "" };
    return {
      text: `${parsed > 0 ? "+" : ""}${number(parsed, 2)}%`,
      className: parsed > 0 ? "positive" : parsed < 0 ? "negative" : "",
    };
  }

  function currentTable() {
    return {
      columns: [
        '<col style="width:56px">',
        '<col style="width:86px">',
        '<col style="width:230px">',
        '<col style="width:80px">',
        '<col style="width:88px">',
        '<col style="width:85px">',
        '<col style="width:95px">',
        '<col style="width:100px">',
        '<col style="width:165px">',
        '<col style="width:95px">',
        '<col style="width:185px">',
      ].join(""),
      head: "<tr><th>排名</th><th>状态</th><th>股票</th><th>最新价</th><th>今日涨跌</th><th>换手率</th><th>成交额</th><th>总市值</th><th>本地 / 风险复核 / 最终</th><th class=\"action-heading\">动作</th><th class=\"reason-heading\">原因</th></tr>",
    };
  }

  function longTable() {
    return {
      columns: [
        '<col style="width:56px">',
        '<col style="width:220px">',
        '<col style="width:110px">',
        '<col style="width:110px">',
        '<col style="width:170px">',
        '<col style="width:130px">',
        '<col style="width:168px">',
      ].join(""),
      head: "<tr><th>类别</th><th>股票</th><th>最新价</th><th>今日涨跌</th><th>成交 / 换手</th><th>总市值</th><th>行情来源 / 时间</th></tr>",
    };
  }

  function historyTable() {
    return {
      columns: [
        '<col style="width:56px">',
        '<col style="width:86px">',
        '<col style="width:168px">',
        '<col style="width:118px">',
        '<col style="width:118px">',
        '<col style="width:118px">',
        '<col style="width:150px">',
      ].join(""),
      head: "<tr><th>排名</th><th>状态</th><th>股票</th><th>锚点价格</th><th>锚点涨跌</th><th>今日涨跌</th><th>锚点至今</th></tr>",
    };
  }

  function rows(items, historical) {
    if (!Array.isArray(items) || items.length === 0) return "";
    return items.map((item) => row(item, historical)).join("");
  }

  function longRows(items) {
    if (!Array.isArray(items) || items.length === 0) return "";
    return items.map(longRow).join("");
  }

  function row(item, historical) {
    return historical ? historyRow(item) : currentRow(item);
  }

  function stock(item) {
    const industry = String(item.industry || "").trim()
      || (item.classified_industry ? `证监会行业 ${item.classified_industry}` : "");
    const context = [BOARD_LABELS[item.board], industry].filter(Boolean).join(" · ");
    return `<span class="stock-name">${escapeHtml(item.name || "-")}</span><span class="stock-code">${escapeHtml([item.code || "-", context].filter(Boolean).join(" · "))}</span>`;
  }

  function tierTag(item) {
    const tier = ["recommend", "watch", "avoid"].includes(item.tier) ? item.tier : "avoid";
    const label = ({ recommend: "推荐", watch: "关注", avoid: "回避" })[tier];
    return `<span class="tier-tag" data-tier="${tier}">${label}</span>`;
  }

  function currentRow(item) {
    const change = pct(item.pct_change);
    const scores = item.scores || {};
    const action = String(item.action || "unavailable");
    const rowClass = action === "unavailable" ? "is-unavailable" : "";
    const review = reviewStatus(item);
    return `<tr class="${rowClass}" tabindex="0" data-code="${escapeHtml(item.code)}">
      <td>${number(item.rank, 0)}</td>
      <td>${tierTag(item)}</td>
      <td>${stock(item)}</td>
      <td>${number(item.price, 2)}</td>
      <td class="${change.className}">${change.text}</td>
      <td>${valuePercent(item.turnover_rate) || "-"}</td>
      <td>${compact(item.amount)}</td>
      <td>${compact(item.market_cap)}</td>
      <td><div class="score-stack"><span aria-label="本地评分"><b>${number(scores.local_score, 2)}</b></span><span aria-label="风险复核"><b>${review}</b></span><span aria-label="最终评分"><b>${number(scores.final_score, 2)}</b></span></div></td>
      <td class="action-cell"><span class="action-tag" data-action="${escapeHtml(action)}">${escapeHtml(ACTION_LABELS[action] || "动作状态未知")}</span></td>
      <td class="reason-cell"><span class="reason-tag">${escapeHtml(actionReason(item.action_reason))}</span></td>
    </tr>`;
  }

  function longRow(item) {
    const change = pct(item.pct_change);
    const rowClass = item.long_section_divider ? "has-long-section-divider" : "";
    return `<tr class="${rowClass}" tabindex="0" data-code="${escapeHtml(item.code)}">
      <td>${number(item.rank, 0)}</td>
      <td>${stock(item)}</td>
      <td>${number(item.price, 2)}</td>
      <td class="${change.className}">${change.text}</td>
      <td>${compact(item.amount)}<span class="stock-code">换手 ${number(item.turnover_rate, 2)}%</span></td>
      <td>${compact(item.market_cap)}</td>
      <td>${hasValue(item.source_time) ? formatDateTime(item.source_time) : "-"}<span class="stock-code">${escapeHtml(sourceLabel(item.source))}</span></td>
    </tr>`;
  }

  function sourceLabel(value) {
    if (!hasValue(value)) return "来源未知";
    const raw = String(value).trim();
    if (SOURCE_LABELS[raw]) return SOURCE_LABELS[raw];
    const parts = raw.split("+").map((part) => part.trim()).filter(Boolean);
    if (parts.length > 1 && parts.every((part) => SOURCE_LABELS[part])) {
      return parts.map((part) => SOURCE_LABELS[part]).join(" · ");
    }
    return /[A-Za-z_]/.test(raw) ? "行情来源待确认" : raw;
  }

  function tableDefinition(snapshot) {
    if (snapshot && snapshot.historical === true) return historyTable();
    if (snapshot && snapshot.strategy === "long") return longTable();
    return currentTable();
  }

  function tableRows(items, snapshot) {
    if (snapshot && snapshot.historical === true) return rows(items, true);
    if (snapshot && snapshot.strategy === "long") return longRows(items);
    return rows(items, false);
  }

  function tableColumnCount(snapshot) {
    if (snapshot && snapshot.historical === true) return 7;
    if (snapshot && snapshot.strategy === "long") return 7;
    return 11;
  }

  function historyRow(item) {
    const anchorChange = pct(item.anchor_daily_return_pct);
    const todayChange = pct(item.pct_change);
    const anchorToNow = pct(item.anchor_to_now_pct);
    return `<tr tabindex="0" data-code="${escapeHtml(item.code)}">
      <td>${number(item.rank, 0)}</td>
      <td>${tierTag(item)}</td>
      <td>${stock(item)}</td>
      <td>${number(item.anchor_price, 2)}</td>
      <td class="${anchorChange.className}">${anchorChange.text}</td>
      <td class="${todayChange.className}">${todayChange.text}</td>
      <td class="${anchorToNow.className}">${anchorToNow.text}</td>
    </tr>`;
  }

  function drawer(item, snapshot) {
    const scores = item.scores || {};
    const historical = snapshot.historical === true;
    const long = snapshot.strategy === "long";
    const action = String(item.action || "unavailable");
    const downside = item.downside || null;
    const conclusionValues = long
      ? [
        ["观察状态", ACTION_LABELS[action] || "观察"],
        ["观察类别", item.group || null],
      ]
      : [
        ["推荐动作", ACTION_LABELS[action] || "动作状态未知"],
        ["入池状态", item.selected === false ? "未入池" : "已入池"],
        ["最终评分", valueNumber(scores.final_score, 2)],
        ["当前排名", hasValue(item.rank) ? `第 ${number(item.rank, 0)} 名` : null],
        ["入场形态", SETUP_LABELS[item.setup && item.setup.type] || "形态信息暂不可用"],
        ["下行保护", downside ? (downside.status === "pass" ? "通过" : "转观察") : null],
      ];
    const conclusion = [
      detailGrid(conclusionValues),
      `<div class="detail-reason"><span>${long ? "观察依据" : "推荐原因"}</span><strong>${escapeHtml(actionReason(item.action_reason))}</strong></div>`,
    ].join("");

    const marketValues = historical
      ? [
        ["锚点价", valueNumber(item.anchor_price, 2)],
        ["锚点当日涨跌", valuePct(item.anchor_daily_return_pct)],
        ["当前价", valueNumber(item.price, 2)],
        ["今日涨跌", valuePct(item.pct_change)],
        ["锚点至今", valuePct(item.anchor_to_now_pct)],
      ]
      : [
        ["最新价", valueNumber(item.price, 2)],
        ["今日涨跌", valuePct(item.pct_change)],
        ["换手率", valuePercent(item.turnover_rate)],
        ["成交额", valueCompact(item.amount)],
        ["总市值", valueCompact(item.market_cap)],
      ];
    marketValues.push(
      ["报价来源", hasValue(item.source) ? item.source : null],
      ["行情时间", hasValue(item.source_time) ? formatDateTime(item.source_time) : null],
    );
    const requiredMarket = historical
      ? [item.anchor_price, item.anchor_daily_return_pct, item.price, item.pct_change, item.anchor_to_now_pct]
      : [item.price, item.pct_change, item.turnover_rate, item.amount, item.market_cap];
    const marketNotes = [];
    if (requiredMarket.some((value) => !hasValue(value))) marketNotes.push(note("部分核心行情暂缺", "warn"));
    if (Array.isArray(snapshot.degraded_reasons) && snapshot.degraded_reasons.length > 0) {
      marketNotes.push(note(`数据降级：${reasonLabels(snapshot.degraded_reasons).join("、")}`, "warn"));
    }
    if (long) {
      return [
        section("观察结论", conclusion),
        section("核心行情", detailGrid(marketValues) + marketNotes.join("")),
      ].join("");
    }

    const inputVersions = snapshot.input_versions || {};
    const scoreVersion = inputVersions.score_model || snapshot.strategy_version;
    const scoreValues = [
      ["评分版本", hasValue(scoreVersion) ? scoreVersion : null],
      ["本地评分", valueNumber(scores.local_score, 2)],
      ["模型相对信号分", valueNumber(scores.model_signal_score, 2)],
      ["预测超额收益", valuePct(scores.predicted_excess_return_pct)],
      ["估算交易成本", valuePct(scores.estimated_cost_pct)],
      ["预测成本后净超额", valuePct(scores.predicted_net_excess_pct)],
      ["模型分歧", valuePct(scores.model_disagreement_pct)],
      ["旧版DeepSeek评分", valueNumber(scores.deepseek_score, 2)],
      ["旧版DeepSeek风险扣分", Number(scores.deepseek_risk_penalty) > 0 ? number(scores.deepseek_risk_penalty, 2) : null],
    ];
    const scoreParts = [detailGrid(scoreValues)];
    scoreParts.push(note(reviewResult(item), "muted"));
    if (Array.isArray(item.risks) && item.risks.length > 0) {
      scoreParts.push('<h4 class="detail-subtitle">实际风险</h4>', riskList(item.risks));
    }
    const research = item.research_coverage;
    const researchParts = hasResearchCoverage(research) ? detailGrid([
      ["总体状态", researchStatus(research)],
      ["研究证据", `${number(research.evidence_count, 0)} 条`],
      ["结构化风险事实", `${number(research.structured_risk_fact_count, 0)} 条`],
      ["财务数据", researchComponentStatus(research, "financial")],
      ["公司公告 / 减持", researchComponentStatus(research, "announcements")],
      ["股权质押", researchComponentStatus(research, "pledge")],
      ["限售解禁", researchComponentStatus(research, "unlock")],
    ]) : "";
    if (downside) {
      scoreParts.push(
        '<h4 class="detail-subtitle">下行保护</h4>',
        detailGrid([
          ["ATR20", valuePercent(downside.atr20_pct)],
          ["日内回撤 / ATR", hasValue(downside.intraday_reversal_atr) ? `${number(downside.intraday_reversal_atr, 2)} 倍` : null],
          ["历史最大回撤", valuePercent(downside.historical_drawdown_pct)],
        ]),
      );
      if (Array.isArray(downside.reasons) && downside.reasons.length > 0) {
        scoreParts.push(note(downside.reasons.map((reason) => DOWNSIDE_REASON_LABELS[reason] || "下行保护条件触发").join("、"), "warn"));
      }
    }

    return [
      section("推荐结论", conclusion),
      section("核心行情", detailGrid(marketValues) + marketNotes.join("")),
      section("公司风险研究", researchParts),
      section("评分与风险", scoreParts.join("")),
    ].join("");
  }

  function section(title, body) {
    if (!body) return "";
    return `<section class="detail-section"><h3>${escapeHtml(title)}</h3>${body}</section>`;
  }

  function detailGrid(values) {
    const present = values.filter((entry) => hasValue(entry[1]));
    if (present.length === 0) return "";
    return `<div class="detail-grid">${present.map(([label, value]) => `<div class="detail-value"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join("")}</div>`;
  }

  function note(message, level) {
    return `<div class="detail-note" data-level="${escapeHtml(level)}">${escapeHtml(message)}</div>`;
  }

  function riskList(risks) {
    return `<ul class="detail-list detail-risk-list">${risks.map((risk) => {
      const label = RISK_LABELS[risk.risk_code] || "其他风险提示";
      const origin = risk.origin === "local" ? "本地" : risk.origin === "deepseek" ? "DeepSeek" : "来源未保存";
      const veto = risk.veto_applied === true ? " · 已生效否决" : "";
      return `<li><b>${escapeHtml(label)}</b><span>${escapeHtml(` · ${origin}${veto}`)}</span></li>`;
    }).join("")}</ul>`;
  }

  function actionReason(value) {
    const reason = String(value || "");
    if (ACTION_REASON_LABELS[reason]) return ACTION_REASON_LABELS[reason];
    if (reason.startsWith("market_data_observe_only:")) return "行情或交易规则受限，仅供观察";
    if (reason.startsWith("downside_guard:")) {
      const reasons = reason.slice("downside_guard:".length)
        .split(",")
        .map((item) => DOWNSIDE_REASON_LABELS[item] || "下行保护条件触发");
      return `下行保护：${reasons.join("、")}，仅供观察`;
    }
    return reason ? "推荐条件暂未满足" : "暂无补充说明";
  }

  function reviewStatus(item) {
    const outcome = item.review_outcome;
    if (outcome) return ({ applied: "已复核", abstain: "已弃权", late: "已迟到", rejected: "未通过" })[outcome] || "结果未知";
    if (item.research_coverage && item.research_coverage.review_eligible === false) return "未入复核";
    return item.research_coverage && item.research_coverage.review_eligible === true ? "无结果" : "无记录";
  }

  function reviewResult(item) {
    const outcome = item.review_outcome;
    if (outcome) return ({
      applied: "结构化风险复核已完成；最终评分仍为本地分",
      abstain: "风险复核弃权；最终评分仍为本地分",
      late: "风险复核结果迟到，未参与本次决策",
      rejected: "风险复核未生效，保留本地决策",
    })[outcome] || "复核结果未知；最终评分为本地分";
    if (item.research_coverage && item.research_coverage.review_eligible === false) {
      return "未进入本次 DeepSeek 复核候选；最终评分为本地分";
    }
    return item.research_coverage && item.research_coverage.review_eligible === true
      ? "本次 DeepSeek 复核无结果；最终评分为本地分"
      : "未保存 DeepSeek 复核结果；最终评分为本地分";
  }

  function hasResearchCoverage(research) {
    if (!research || !research.components) return false;
    return ["financial", "announcements", "pledge", "unlock"]
      .some((key) => ["known_clear", "known_risk", "stale"].includes(research.components[key]));
  }

  function researchStatus(research) {
    const values = ["financial", "announcements", "pledge", "unlock"].map((key) => research.components[key]);
    if (values.every((value) => value === "known_clear" || value === "known_risk")) return "风险研究：已核";
    return "风险研究：部分";
  }

  function researchComponentStatus(research, key) {
    const status = research && research.components && research.components[key];
    return ({ known_clear: "已核：未见风险", known_risk: "已核：有风险", stale: "已过期", unknown: "未获取" })[status]
      || "历史状态未知";
  }

  function reasonLabel(value) {
    const reason = String(value || "").trim();
    if (!reason) return "部分数据暂不可用";
    if (DEGRADED_REASON_LABELS[reason]) return DEGRADED_REASON_LABELS[reason];
    const separator = reason.indexOf(":");
    if (separator > 0) {
      const prefix = reason.slice(0, separator);
      const suffix = reason.slice(separator + 1);
      if (BOARD_LABELS[prefix] && DEGRADED_REASON_LABELS[suffix]) {
        return `${BOARD_LABELS[prefix]}：${DEGRADED_REASON_LABELS[suffix]}`;
      }
      if (["eastmoney", "sina", "tencent", "tencent_market", "tushare", "akshare", "company_evidence"].includes(prefix)) {
        return "行情数据源暂不可用";
      }
    }
    if (/[\u3400-\u9fff]/u.test(reason)) return reason;
    return "部分数据暂不可用";
  }

  function reasonLabels(values) {
    if (!Array.isArray(values)) return [];
    return [...new Set(values.map(reasonLabel))];
  }

  function statusErrorLabel(value) {
    const error = String(value || "").trim();
    if (!error) return "无";
    if (error.includes("TopK live overlay degraded")) return "TopK 行情刷新暂时降级";
    if (error.includes("batch deadline") || error.includes("deadline expired")) return "数据处理超过本批截止时间";
    if (error.includes("after-close") || error.includes("close rebuild")) return "收盘荐股恢复暂时降级";
    if (error.includes("board scoring degraded")) return "分板评分暂时降级，已保留最近有效结果";
    if (error.includes("DeepSeek review degraded")) return "模型复核暂时降级，继续使用本地评分";
    if (error.includes("market data degraded")) return "行情数据暂时降级";
    if (DEGRADED_REASON_LABELS[error] || error.includes(":board_")) return reasonLabel(error);
    if (/^[\u3400-\u9fff，。：；、\s]+$/u.test(error)) return error;
    return "运行链路暂时降级";
  }

  function fusionModeLabel(value) {
    return FUSION_MODE_LABELS[String(value || "")] || "评分模式";
  }

  function rememberDiagnostic(values, value) {
    const raw = String(value || "").trim();
    if (!raw || values.includes(raw)) return;
    values.push(raw.slice(0, 300));
    if (values.length > 20) values.shift();
  }

  function hasValue(value) {
    return value !== null && value !== undefined && value !== "";
  }

  function valueNumber(value, digits) {
    return hasValue(value) ? number(value, digits) : null;
  }

  function valuePct(value) {
    return hasValue(value) ? pct(value).text : null;
  }

  function valuePercent(value) {
    return hasValue(value) ? `${number(value, 2)}%` : null;
  }

  function valueCompact(value) {
    return hasValue(value) ? compact(value) : null;
  }

  function formatDateTime(value) {
    const parsed = new Date(value);
    if (!Number.isFinite(parsed.getTime())) return "-";
    return parsed.toLocaleString("zh-CN", { hour12: false });
  }

  function formatTime(value) {
    const parsed = new Date(value);
    if (!Number.isFinite(parsed.getTime())) return "-";
    return parsed.toLocaleTimeString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" });
  }

  window.TraderRender = {
    actionReason,
    currentTable,
    drawer,
    escapeHtml,
    formatDateTime,
    formatTime,
    historyTable,
    longTable,
    fusionModeLabel,
    rememberDiagnostic,
    number,
    pct,
    pipelineReasonLabel,
    pipelineFacetLabel,
    reasonLabel,
    reasonLabels,
    row,
    rows,
    tableColumnCount,
    tableDefinition,
    tableRows,
    statusErrorLabel,
    sourceLabel,
  };
})();
