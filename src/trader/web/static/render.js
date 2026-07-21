(function () {
  "use strict";

  const ACTION_LABELS = {
    executable: "可执行",
    observe: "观察",
    unavailable: "不可执行",
  };

  const DIMENSION_LABELS = {
    value_quality: "价值质量",
    financial_health: "财务健康",
    market_flow: "资金量价",
    industry_policy: "行业政策",
    risk_quality: "风险质量",
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

  const BOARD_LABELS = {
    main: "沪深主板",
    chinext: "创业板",
    star: "科创板",
    unsupported: "未支持板块",
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
    if (value == null || value === "") return "-";
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return "-";
    return parsed.toLocaleString("zh-CN", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

  function compact(value) {
    if (value == null || value === "") return "-";
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return "-";
    const absolute = Math.abs(parsed);
    if (absolute >= 100000000) return `${number(parsed / 100000000, 2)}亿`;
    if (absolute >= 10000) return `${number(parsed / 10000, 1)}万`;
    return number(parsed, 0);
  }

  function pct(value) {
    if (value == null || value === "") return { text: "-", className: "" };
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
      <td><span class="action-tag" data-action="${escapeHtml(action)}">${escapeHtml(ACTION_LABELS[action] || action)}</span></td>
      <td class="reason-cell"><span class="reason-tag">${escapeHtml(item.action_reason || "-")}</span></td>
    </tr>`;
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
    const review = item.review || {};
    const reviewed = item.review != null && scores.deepseek_score != null;
    const dimensions = review.dimensions || {};
    const risks = [...(item.local_risk_facts || []), ...(item.deepseek_risk_facts || [])];
    const components = scores.components || {};
    const features = item.features || {};
    const metadata = snapshot.metadata || {};
    const code = String(item.code || "");
    const fieldSources = objectValue(metadata.field_sources, code);
    const sourceVersions = plainObject(metadata.source_versions);
    const marketMissing = matchingEntries(metadata.market_missing_reasons, `${code}.`);
    const marketConflicts = matchingValues(metadata.market_conflicts, code);
    const degradedReasons = [
      ...(Array.isArray(snapshot.degraded_reasons) ? snapshot.degraded_reasons : []),
      ...(Array.isArray(metadata.market_degraded_reasons) ? metadata.market_degraded_reasons : []),
    ];
    return [
      section("评分", detailGrid([
        ["基础分", number(scores.base_score, 2)],
        ["本地风险扣分", number(scores.local_risk_penalty, 2)],
        ["本地分", number(scores.local_score, 2)],
        ["DeepSeek 分", reviewed ? number(scores.deepseek_score, 2) : "未复核"],
        ["DeepSeek 风险扣分", reviewed ? number(scores.deepseek_risk_penalty, 2) : "未复核"],
        ["最终分", number(scores.final_score, 2)],
        ["置信覆盖", reviewed ? `${number(scores.confidence_coverage * 100, 1)}%` : "未复核"],
        ["融合模式", snapshot.fusion_mode || scores.fusion_mode || "-"],
      ])),
      section("本地组件", keyValueList(components)),
      section("权重", nestedValueList(snapshot.weights || {})),
      section("分位与截尾", normalizationList(item.normalization || {})),
      section("风险事实", riskList(risks)),
      section("DeepSeek 审计", reviewAudit(review)),
      section("DeepSeek 五维", dimensionList(dimensions, review)),
      section("缺失字段", missingFieldList(item.missing_fields || [], item.missing_reasons || {})),
      section("原始指标", keyValueList(features)),
      section("证据", evidenceList(item.evidence || [])),
      section("板块与交易规则", detailGrid([
        ["板块", BOARD_LABELS[item.board] || item.board || "-"],
        ["板块来源", item.board_source || "-"],
        ["身份可靠度", item.board_reliability || "-"],
        ["交易所", item.exchange || "-"],
        ["上市日期", item.listing_date || "-"],
        ["上市交易日龄", number(item.listing_age_sessions, 0)],
        ["是否有价格限制", booleanText(item.has_price_limit)],
        ["交易所涨跌幅限制", percentText(item.exchange_limit_pct)],
        ["策略过热上限", percentText(item.strategy_hot_cap_pct)],
        ["规则版本", item.rule_version || "-"],
        ["规则生效日", item.rule_effective_date || "-"],
        ["执行限制", listText(item.execution_restrictions, "无")],
      ])),
      section("多源合并", [
        detailGrid([
          ["合并版本", metadata.merge_epoch || "-"],
          ["行情观察时间", formatDateTime(metadata.market_observed_at)],
          ["价格偏差", percentText(item.cross_source_deviation_pct)],
          ["定向复核", booleanText(item.cross_source_verified)],
          ["冲突", listText(marketConflicts, "无")],
          ["降级原因", listText([...new Set(degradedReasons)], "无")],
        ]),
        textValueList(sourceVersions, "暂无来源版本"),
        textValueList(fieldSources, "暂无逐字段来源"),
        textValueList(marketMissing, "暂无合并缺失"),
      ].join("")),
      section("快照", detailGrid([
        ["策略版本", snapshot.strategy_version || "-"],
        ["融合版本", snapshot.fusion_version || "-"],
        ["数据版本", snapshot.data_version || "-"],
        ["发布时间", formatDateTime(snapshot.published_at)],
      ])),
    ].join("");
  }

  function section(title, body) {
    return `<section class="detail-section"><h3>${escapeHtml(title)}</h3>${body}</section>`;
  }

  function detailGrid(values) {
    return `<div class="detail-grid">${values.map(([label, value]) => `<div class="detail-value"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join("")}</div>`;
  }

  function keyValueList(values) {
    const entries = Object.entries(values || {}).sort(([left], [right]) => left.localeCompare(right));
    if (entries.length === 0) return '<div class="detail-value"><span>状态</span><strong>无数据</strong></div>';
    return `<ul class="detail-list">${entries.map(([key, value]) => `<li><b>${escapeHtml(key)}</b> · ${escapeHtml(number(value, 2))}</li>`).join("")}</ul>`;
  }

  function nestedValueList(values) {
    const rows = [];
    for (const [group, entries] of Object.entries(values || {})) {
      if (!entries || typeof entries !== "object") continue;
      for (const [name, value] of Object.entries(entries)) rows.push([`${group}.${name}`, value]);
    }
    return keyValueList(Object.fromEntries(rows));
  }

  function textValueList(values, emptyText) {
    const entries = Object.entries(plainObject(values)).sort(([left], [right]) => left.localeCompare(right));
    if (entries.length === 0) {
      return `<div class="detail-value"><span>状态</span><strong>${escapeHtml(emptyText)}</strong></div>`;
    }
    return `<ul class="detail-list">${entries.map(([key, value]) => `<li><b>${escapeHtml(key)}</b> · ${escapeHtml(displayText(value))}</li>`).join("")}</ul>`;
  }

  function plainObject(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  }

  function objectValue(value, key) {
    return plainObject(plainObject(value)[key]);
  }

  function matchingEntries(value, prefix) {
    return Object.fromEntries(Object.entries(plainObject(value)).filter(([key]) => key.startsWith(prefix)));
  }

  function matchingValues(value, code) {
    if (!Array.isArray(value)) return [];
    return value.filter((entry) => String(entry).includes(code));
  }

  function displayText(value) {
    if (value == null || value === "") return "-";
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  function booleanText(value) {
    if (value == null) return "-";
    return value === true ? "是" : "否";
  }

  function percentText(value) {
    return value == null || value === "" ? "-" : `${number(value, 2)}%`;
  }

  function listText(values, emptyText) {
    return Array.isArray(values) && values.length > 0 ? values.map(displayText).join("、") : emptyText;
  }

  function normalizationList(values) {
    const entries = Object.entries(values || {}).sort(([left], [right]) => left.localeCompare(right));
    if (entries.length === 0) return '<div class="detail-value"><span>状态</span><strong>无分位数据</strong></div>';
    return `<ul class="detail-list">${entries.map(([name, value]) => `<li><b>${escapeHtml(name)}</b> · 截尾 ${number(value.lower_bound, 4)} / ${number(value.upper_bound, 4)} · 样本 ${number(value.sample_size, 0)} · 缺失 ${number(value.missing_count, 0)}<br>分位 ${number((value.lower_quantile || 0) * 100, 1)}% / ${number((value.upper_quantile || 0) * 100, 1)}% · 版本 ${escapeHtml(value.population_data_version || "-")}</li>`).join("")}</ul>`;
  }

  function missingFieldList(fields, reasons) {
    if (!Array.isArray(fields) || fields.length === 0) {
      return '<div class="detail-value"><span>状态</span><strong>无缺失字段</strong></div>';
    }
    return `<ul class="detail-list">${fields.map((field) => `<li><b>${escapeHtml(field)}</b> · 未获取：${escapeHtml(reasons[field] || "当前快照缺少上游输入")}</li>`).join("")}</ul>`;
  }

  function riskList(risks) {
    if (!Array.isArray(risks) || risks.length === 0) return '<div class="detail-value"><span>状态</span><strong>未识别风险事实</strong></div>';
    const seen = new Set();
    return `<ul class="detail-list">${risks.map((risk) => {
      const duplicate = seen.has(risk.risk_fact_id);
      seen.add(risk.risk_fact_id);
      const suffix = duplicate ? " · 已跨来源去重" : "";
      const actual = risk.actual == null ? "-" : number(risk.actual, 4);
      return `<li><b>${escapeHtml(risk.risk_code)}</b> · ${escapeHtml(risk.severity)} · 扣分 ${number(risk.penalty, 2)}${escapeHtml(suffix)}<br>${escapeHtml(risk.assessment || "-")}<br>实际 ${escapeHtml(actual)} · 阈值 ${escapeHtml(risk.threshold || "-")} · 来源 ${escapeHtml(risk.source || "-")} · 证据时间 ${escapeHtml(formatDateTime(risk.observed_at))} · 置信 ${number((risk.confidence || 0) * 100, 0)}%</li>`;
    }).join("")}</ul>`;
  }

  function dimensionList(dimensions, review) {
    const entries = Object.entries(dimensions || {});
    if (entries.length === 0) {
      return `<div class="detail-value"><span>结果</span><strong>${escapeHtml(reviewResult(review))}</strong></div>`;
    }
    return `<ul class="detail-list">${entries.map(([name, value]) => `<li><b>${escapeHtml(DIMENSION_LABELS[name] || name)}</b> · ${number(value.score, 2)} / 置信 ${number((value.confidence || 0) * 100, 0)}%<br>${escapeHtml(value.assessment || "-")}</li>`).join("")}</ul>`;
  }

  function reviewAudit(review) {
    if (!review || !review.outcome) return detailGrid([["结果", "未复核"]]);
    const primaryMode = [review.thinking_mode, review.reasoning_effort].filter(Boolean).join(" / ") || "-";
    const challengerMode = [review.challenger_thinking_mode, review.challenger_reasoning_effort].filter(Boolean).join(" / ") || "-";
    return detailGrid([
      ["结果", reviewResult(review)],
      ["复核阶段", review.review_stage || "primary"],
      ["挑战者状态", review.challenger_status || "not_run"],
      ["主审请求模型", review.requested_model || "-"],
      ["主审实际模型", review.actual_model || "-"],
      ["主审模式", primaryMode],
      ["主审指纹", review.system_fingerprint || "-"],
      ["主审缓存命中 / 未命中", tokenPair(review.prompt_cache_hit_tokens, review.prompt_cache_miss_tokens)],
      ["挑战者请求模型", review.challenger_requested_model || "-"],
      ["挑战者实际模型", review.challenger_actual_model || "-"],
      ["挑战者模式", challengerMode],
      ["挑战者指纹", review.challenger_system_fingerprint || "-"],
      ["挑战者缓存命中 / 未命中", tokenPair(review.challenger_prompt_cache_hit_tokens, review.challenger_prompt_cache_miss_tokens)],
      ["原始置信", confidence(review.raw_confidence)],
      ["校准置信", confidence(review.calibrated_confidence)],
      ["校准版本", review.calibration_version || "-"],
      ["证据清单", review.evidence_manifest_hash || "-"],
    ]);
  }

  function tokenPair(hit, miss) {
    if (hit == null && miss == null) return "-";
    return `${number(hit, 0)} / ${number(miss, 0)}`;
  }

  function confidence(value) {
    return value == null ? "-" : `${number(Number(value) * 100, 1)}%`;
  }

  function reviewResult(review) {
    if (!review || !review.outcome) return "未复核";
    const error = String(review.error || "");
    if (REVIEW_ERROR_LABELS[error]) return REVIEW_ERROR_LABELS[error];
    if (review.outcome === "abstain") return "模型弃权：使用本地评分";
    if (review.outcome === "late") return "迟到：结果未参与评分";
    if (review.outcome !== "rejected") return String(review.outcome);
    if (
      error.startsWith("http_") ||
      error.startsWith("internal_") ||
      ["timeout", "request_error", "request_failed", "empty_response", "invalid_response"].includes(error)
    ) {
      return "调用失败：已回退本地评分";
    }
    return "拒绝：响应未通过结构化校验";
  }

  function evidenceList(items) {
    if (!Array.isArray(items) || items.length === 0) return '<div class="detail-value"><span>状态</span><strong>无结构化证据</strong></div>';
    return `<ul class="detail-list">${items.map((item) => `<li><b>${escapeHtml(item.type || "evidence")}</b> · ${escapeHtml(item.title || "-")}<br>${escapeHtml(item.source || "-")} · ${escapeHtml(formatDateTime(item.published_at))}</li>`).join("")}</ul>`;
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
    number,
    pct,
    row,
    rows,
  };
})();
