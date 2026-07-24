(function () {
  "use strict";

  const ACTION_LABELS = {
    executable: "可执行",
    observe: "观察",
    unavailable: "不可执行",
  };

  const ACTION_REASON_LABELS = {
    long_watch_only: "长期观察池",
    risk_veto: "风险事实触发限制",
    stale_quote: "行情已过期，仅供观察",
    insufficient_core_features: "核心数据不足，仅供观察",
    corporate_risk_history_unavailable: "公司风险历史暂不可核验",
    board_data_reliability_below_threshold: "板块数据可靠度不足",
    observation_window: "当前处于观察时段",
    outside_execution_window: "当前不在执行时段",
    score_threshold_met: "评分达到执行门槛",
    near_score_threshold: "接近执行门槛，继续观察",
    below_score_threshold: "评分未达到执行门槛",
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
    corporate_risk_history_unavailable: "公司风险历史暂不可核验",
  };

  const RISK_SEVERITY_LABELS = {
    low: "低",
    medium: "中",
    high: "高",
    critical: "严重",
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
    close_fallback_observation_floor_relaxed: "收盘补算已放宽观察展示门槛",
    deepseek_pending: "模型复核进行中",
    tomorrow_tail_data_incomplete: "尾盘量价数据不完整",
    d25_structured_research_incomplete: "2至5日结构化研究数据不完整",
    corporate_risk_history_unavailable: "公司风险历史暂不可核验",
    model_unavailable: "模型服务暂不可用",
    quote_fallback: "行情已使用备用数据",
  };

  const BOARD_LABELS = {
    main: "主板",
    chinext: "创业板",
    star: "科创板",
  };

  const FUSION_MODE_LABELS = {
    hybrid: "本地与模型融合",
    local_degraded: "本地评分模式",
    local_only: "仅本地评分",
  };

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
        '<col style="width:168px">',
        '<col style="width:92px">',
        '<col style="width:92px">',
        '<col style="width:150px">',
        '<col style="width:112px">',
        '<col style="width:220px">',
        '<col style="width:96px">',
        '<col style="width:190px">',
      ].join(""),
      head: "<tr><th>排名</th><th>股票</th><th>最新价</th><th>今日涨跌</th><th>成交 / 换手</th><th>总市值</th><th>本地 / 模型 / 扣分 / 最终</th><th>动作</th><th>原因</th></tr>",
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
      head: "<tr><th>排名</th><th>股票</th><th>最新价</th><th>今日涨跌</th><th>成交 / 换手</th><th>总市值</th><th>行情来源 / 时间</th></tr>",
    };
  }

  function historyTable() {
    return {
      columns: [
        '<col style="width:56px">',
        '<col style="width:168px">',
        '<col style="width:118px">',
        '<col style="width:118px">',
        '<col style="width:118px">',
        '<col style="width:150px">',
      ].join(""),
      head: "<tr><th>排名</th><th>股票</th><th>锚点价格</th><th>锚点涨跌</th><th>今日涨跌</th><th>锚点至今</th></tr>",
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
    return `<span class="stock-name">${escapeHtml(item.name || "-")}</span><span class="stock-code">${escapeHtml(item.code || "-")} · ${escapeHtml(item.industry || "未分类")}</span>`;
  }

  function currentRow(item) {
    const change = pct(item.pct_change);
    const scores = item.scores || {};
    const action = String(item.action || "unavailable");
    const rowClass = action === "unavailable" ? "is-unavailable" : "";
    const deepseek = scores.deepseek_score == null ? "未复核" : number(scores.deepseek_score, 2);
    const deepseekPenalty = scores.deepseek_score == null ? "未复核" : number(scores.deepseek_risk_penalty, 2);
    return `<tr class="${rowClass}" tabindex="0" data-code="${escapeHtml(item.code)}">
      <td>${number(item.rank, 0)}</td>
      <td>${stock(item)}</td>
      <td>${number(item.price, 2)}</td>
      <td class="${change.className}">${change.text}</td>
      <td>${compact(item.amount)}<span class="stock-code">换手 ${number(item.turnover_rate, 2)}%</span></td>
      <td>${compact(item.market_cap)}</td>
      <td><div class="score-stack"><span><b>${number(scores.local_score, 2)}</b>本地</span><span><b>${deepseek}</b>模型</span><span><b>${deepseekPenalty}</b>扣分</span><span><b>${number(scores.final_score, 2)}</b>最终</span></div></td>
      <td><span class="action-tag" data-action="${escapeHtml(action)}">${escapeHtml(ACTION_LABELS[action] || "动作状态未知")}</span></td>
      <td class="reason-cell"><span class="reason-tag">${escapeHtml(actionReason(item.action_reason))}</span></td>
    </tr>`;
  }

  function longRow(item) {
    const change = pct(item.pct_change);
    return `<tr tabindex="0" data-code="${escapeHtml(item.code)}">
      <td>${number(item.rank, 0)}</td>
      <td>${stock(item)}</td>
      <td>${number(item.price, 2)}</td>
      <td class="${change.className}">${change.text}</td>
      <td>${compact(item.amount)}<span class="stock-code">换手 ${number(item.turnover_rate, 2)}%</span></td>
      <td>${compact(item.market_cap)}</td>
      <td>${hasValue(item.source_time) ? formatDateTime(item.source_time) : "-"}<span class="stock-code">${escapeHtml(item.source || "来源未知")}</span></td>
    </tr>`;
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
    if (snapshot && snapshot.historical === true) return 6;
    if (snapshot && snapshot.strategy === "long") return 7;
    return 9;
  }

  function historyRow(item) {
    const anchorChange = pct(item.anchor_daily_return_pct);
    const todayChange = pct(item.pct_change);
    const anchorToNow = pct(item.anchor_to_now_pct);
    return `<tr tabindex="0" data-code="${escapeHtml(item.code)}">
      <td>${number(item.rank, 0)}</td>
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
    const action = String(item.action || "unavailable");
    const downside = item.downside || null;
    const conclusion = [
      detailGrid([
        ["推荐动作", ACTION_LABELS[action] || "动作状态未知"],
        ["最终评分", valueNumber(scores.final_score, 2)],
        ["当前排名", hasValue(item.rank) ? `第 ${number(item.rank, 0)} 名` : null],
        ["入场形态", SETUP_LABELS[item.setup_type] || "形态信息暂不可用"],
        ["下行保护", downside ? (downside.status === "pass" ? "通过" : "转观察") : null],
      ]),
      `<div class="detail-reason"><span>推荐原因</span><strong>${escapeHtml(actionReason(item.action_reason))}</strong></div>`,
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

    const scoreValues = [
      ["本地评分", valueNumber(scores.local_score, 2)],
      ["模型评分", valueNumber(scores.deepseek_score, 2)],
      ["模型风险扣分", Number(scores.deepseek_risk_penalty) > 0 ? number(scores.deepseek_risk_penalty, 2) : null],
    ];
    const scoreParts = [detailGrid(scoreValues)];
    if (!hasValue(scores.deepseek_score)) scoreParts.push(note(reviewResult(item.review), "muted"));
    else if (snapshot.fusion_mode !== "hybrid") scoreParts.push(note("模型评分未参与最终分，当前使用本地模式", "muted"));
    if (Array.isArray(item.risks) && item.risks.length > 0) {
      scoreParts.push('<h4 class="detail-subtitle">实际风险</h4>', riskList(item.risks));
    }
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
      const severity = RISK_SEVERITY_LABELS[risk.severity] || "未知";
      const assessment = risk.assessment ? `<p>${escapeHtml(risk.assessment)}</p>` : "";
      return `<li><b>${escapeHtml(label)}</b><span class="risk-meta">${escapeHtml(severity)}风险 · 扣分 ${number(risk.penalty, 2)}</span>${assessment}</li>`;
    }).join("")}</ul>`;
  }

  function actionReason(value) {
    const reason = String(value || "");
    if (ACTION_REASON_LABELS[reason]) return ACTION_REASON_LABELS[reason];
    if (reason.startsWith("market_data_observe_only:")) return "行情或交易规则受限，仅供观察";
    if (reason.startsWith("downside_guard:")) return "触发下行保护，仅进入观察池";
    return reason ? "推荐条件暂未满足" : "暂无补充说明";
  }

  function reviewResult(review) {
    if (!review || !review.outcome) return "未复核，最终评分使用本地结果";
    const error = String(review.error || "");
    if (REVIEW_ERROR_LABELS[error]) return REVIEW_ERROR_LABELS[error];
    if (review.outcome === "abstain") return "模型弃权：使用本地评分";
    if (review.outcome === "late") return "迟到：结果未参与评分";
    if (review.outcome !== "rejected") return "模型复核已完成";
    if (
      error.startsWith("http_")
      || error.startsWith("internal_")
      || ["timeout", "request_error", "request_failed", "empty_response", "invalid_response"].includes(error)
    ) return "调用失败：已回退本地评分";
    return "拒绝：响应未通过结构化校验";
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
      if (["eastmoney", "sina", "tencent", "tushare", "akshare"].includes(prefix)) {
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
    return parsed.toLocaleTimeString("zh-CN", { hour12: false });
  }

  window.TraderRender = {
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
    reasonLabel,
    reasonLabels,
    row,
    rows,
    tableColumnCount,
    tableDefinition,
    tableRows,
    statusErrorLabel,
  };
})();
