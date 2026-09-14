(function () {
  "use strict";

  const STRATEGIES = new Set(["today", "tomorrow", "d25", "long"]);

  const STAGE_LABELS = {
    refresh: "数据刷新",
    decision: "策略构建",
    review: "模型复核",
    publish: "结果发布",
    freeze: "快照冻结",
    settlement: "收盘结算",
    snapshot: "快照质量",
    runtime: "运行状态",
  };

  const ERROR_LABELS = {
    "refresh:source_unavailable": "行情刷新暂时不可用",
    "refresh:refresh_unavailable": "行情刷新暂时不可用",
    "decision:decision_unavailable": "策略结果暂时无法构建",
    "review:review_unavailable": "模型复核暂时不可用",
    "review:review_identity_mismatch": "模型复核身份不一致",
    "freeze:freeze_unavailable": "正式快照冻结失败",
    "freeze:close_fallback_unavailable": "收盘补算冻结失败",
    "freeze:freeze_capacity_rejected": "冻结任务队列已满",
    "settlement:settlement_unavailable": "收盘结算暂时不可用",
    "settlement:settlement_capacity_rejected": "结算任务队列已满",
    runtime_status_unavailable: "运行状态暂时不可用",
  };

  function healthView(statusPayload, snapshotReasons, strategy) {
    const payload = statusPayload && typeof statusPayload === "object" ? statusPayload : {};
    const runtimeIssues = Array.isArray(payload.recent_errors)
      ? payload.recent_errors.map(normalizeRuntimeIssue).filter(Boolean)
      : [];
    const snapshotIssues = Array.isArray(snapshotReasons)
      ? snapshotReasons.map((code) => normalizeSnapshotIssue(code, strategy)).filter(Boolean)
      : [];
    const issues = deduplicateIssues([...runtimeIssues, ...snapshotIssues]).sort(compareIssues);
    const activeIssues = issues.filter((issue) => issue.recoveryStatus === "active");
    const declaredCount = finiteNonNegativeInteger(payload.health && payload.health.issue_count);
    const issueCount = Math.max(activeIssues.length, declaredCount == null ? 0 : declaredCount);
    const declaredLevel = payload.health && payload.health.level;
    const level = declaredLevel === "error" || activeIssues.some((issue) => issue.severity === "error")
      ? "error"
      : declaredLevel === "degraded" || issueCount > 0
        ? "degraded"
        : "normal";
    return {
      level,
      issueCount,
      badge: level === "normal"
        ? issues.length ? "正常 · 最近已恢复" : "正常 · 无最近错误"
        : `${level === "error" ? "错误" : "降级"} · ${issueCount}项`,
      primary: activeIssues.length ? presentIssue(activeIssues[0]) : null,
      issues,
    };
  }

  function normalizeRuntimeIssue(raw) {
    if (!raw || typeof raw !== "object") return null;
    const code = cleanString(raw.code);
    if (!code) return null;
    const severity = raw.severity === "error" ? "error" : "degraded";
    const recoveryStatus = raw.recovery_status === "recovered" ? "recovered" : "active";
    return {
      code,
      severity,
      strategy: cleanString(raw.strategy),
      stage: cleanString(raw.stage) || stageFromCode(code),
      occurredAt: cleanString(raw.occurred_at),
      lastOccurredAt: cleanString(raw.last_occurred_at) || cleanString(raw.occurred_at),
      resolvedAt: cleanString(raw.resolved_at),
      count: finitePositiveInteger(raw.count) || 1,
      recoveryStatus,
    };
  }

  function normalizeSnapshotIssue(rawCode, strategy) {
    const code = cleanString(rawCode);
    if (!code) return null;
    const split = splitStrategyPrefix(code, strategy);
    return {
      code: split.code,
      severity: "degraded",
      strategy: split.strategy,
      stage: "snapshot",
      occurredAt: null,
      lastOccurredAt: null,
      resolvedAt: null,
      count: 1,
      recoveryStatus: "active",
    };
  }

  function deduplicateIssues(values) {
    const result = new Map();
    values.forEach((issue) => {
      const key = `${issue.strategy || "system"}:${issue.code}`;
      const previous = result.get(key);
      if (!previous || compareIssues(issue, previous) < 0) result.set(key, issue);
    });
    return [...result.values()];
  }

  function compareIssues(left, right) {
    const severity = severityRank(left.severity) - severityRank(right.severity);
    if (severity) return severity;
    const recovery = recoveryRank(left.recoveryStatus) - recoveryRank(right.recoveryStatus);
    if (recovery) return recovery;
    return timestamp(right.lastOccurredAt) - timestamp(left.lastOccurredAt);
  }

  function presentIssue(issue) {
    const strategy = STRATEGIES.has(issue.strategy)
      ? strategyLabel(issue.strategy)
      : issue.strategy
        ? "其他策略"
        : "系统";
    const stage = STAGE_LABELS[issue.stage] || "运行链路";
    const time = issue.lastOccurredAt ? formatTime(issue.lastOccurredAt) : "时间待确认";
    const recovery = issue.recoveryStatus === "recovered" ? "已恢复" : "处理中";
    return {
      ...issue,
      message: issueLabel(issue.code, issue.stage),
      meta: `${strategy} · ${stage} · ${time} · ${recovery}`,
    };
  }

  function runtimeErrorRows(issues) {
    if (!Array.isArray(issues) || issues.length === 0) {
      return '<div class="error-detail-empty">暂无错误记录</div>';
    }
    return issues.map((issue) => {
      const presented = presentIssue(issue);
      const status = issue.recoveryStatus === "recovered" ? "已恢复" : "活动中";
      const statusLevel = issue.recoveryStatus === "recovered" ? "recovered" : issue.severity;
      const count = issue.count > 1 ? ` · 累计 ${issue.count} 次` : "";
      const resolved = issue.resolvedAt ? ` · 恢复于 ${formatDateTime(issue.resolvedAt)}` : "";
      return `<article class="error-detail-item" data-level="${escapeHtml(statusLevel)}">
        <header><div><span class="error-state-tag">${escapeHtml(status)}</span><strong>${escapeHtml(presented.message)}</strong></div><time>${escapeHtml(formatDateTime(issue.lastOccurredAt))}</time></header>
        <p>${escapeHtml(presented.meta)}${escapeHtml(count)}${escapeHtml(resolved)}</p>
        <div class="error-code-row"><code>${escapeHtml(issue.code)}</code><button type="button" data-copy-code="${escapeHtml(issue.code)}">复制代码</button></div>
      </article>`;
    }).join("");
  }

  function issueSummaryTitle(issues) {
    const values = Array.isArray(issues) ? issues : [];
    const active = values.filter((issue) => issue && issue.recoveryStatus !== "recovered").length;
    const recovered = values.length - active;
    if (active && recovered) return `最近错误 · 活动${active}项 / 已恢复${recovered}项`;
    if (active) return `最近错误 · 活动${active}项`;
    if (recovered) return `最近错误 · 已恢复${recovered}项`;
    return "最近错误";
  }

  function createDashboardStateRenderer(els, state, selection, render) {
    const renderTableState = (message, columns, body) => {
      (body || els.tableBody).innerHTML = `<tr><td class="table-state" colspan="${columns || 9}">${render.escapeHtml(message)}</td></tr>`;
    };
    const setLongLayout = (enabled) => {
      if (els.resultLayout) els.resultLayout.classList.toggle("is-long", Boolean(enabled));
    };
    const setLongControls = (enabled) => {
      if (els.longScopeTabs) els.longScopeTabs.hidden = !enabled;
    };
    const setNotice = (message, level) => {
      // Recommendation conclusions are rendered with the list and funnel, not as runtime errors.
    };
    const renderLoadingState = () => {
      els.inputQualityStatus.textContent = "-";
      els.inputQualityMeta.textContent = "正在读取评分输入质量";
      els.inputQualityBlockers.textContent = "本轮阻断：待计算";
      els.inputQualityDegradations.textContent = "仅降级，不代表股票存在风险：待计算";
      els.inputQualityStages.textContent = "正在读取评分输入链路";
      els.funnelStatus.textContent = "-";
      els.funnelStages.textContent = "正在读取评分与决策链路";
      els.funnelScoreRange.textContent = "评分范围 —";
      els.funnelMeta.textContent = "正在读取推荐漏斗";
      els.quoteTime.textContent = "-";
      els.quoteAge.textContent = "-";
      els.quoteSource.textContent = "来源不可用";
      els.inputQualityStrategy.textContent = selection.strategyLabel(state.strategy);
      els.inputQualityScoreTime.textContent = "等待本轮评分完成";
      els.publicationStatus.textContent = "未就绪";
      els.publicationMeta.textContent = "等待当前策略快照";
      renderTopScores(els, null, []);
      els.recommendationTable.classList.remove("is-history", "is-anchor-table", "is-long-table");
      els.observationPool.hidden = true;
      setLongControls(state.strategy === "long");
      setLongLayout(false);
      const definition = render.currentTable();
      els.tableColumns.innerHTML = definition.columns;
      els.tableHead.innerHTML = definition.head;
      if (els.longSidebar) els.longSidebar.hidden = true;
      renderTableState("正在读取推荐快照");
      setNotice("正在读取推荐快照", "idle");
    };
    const renderMissingHistoricalDate = (strategy, selectedDate) => {
      state.payload = null;
      state.projectionVersion = "";
      els.inputQualityStatus.textContent = "—";
      els.inputQualityMeta.textContent = "所选历史快照不重算评分输入";
      els.inputQualityBlockers.textContent = "本轮阻断：历史快照不适用";
      els.inputQualityDegradations.textContent = "仅降级，不代表股票存在风险：历史快照不适用";
      els.inputQualityStages.textContent = "历史快照不重算评分输入链路";
      els.funnelStatus.textContent = "— → — → 0";
      els.funnelStages.textContent = "历史快照不重算逐层推荐漏斗";
      els.funnelScoreRange.textContent = "评分范围 —";
      els.funnelMeta.textContent = "正式 0 · 观察 不保存";
      els.quoteTime.textContent = "-";
      els.quoteAge.textContent = "-";
      els.quoteSource.textContent = "来源不可用";
      els.inputQualityStrategy.textContent = selection.strategyLabel(strategy);
      els.inputQualityScoreTime.textContent = "所选历史日期不重算评分";
      els.publicationStatus.textContent = "历史只读";
      els.publicationMeta.textContent = "所选日期无正式快照";
      renderTopScores(els, null, []);
      els.recommendationTable.classList.add("is-history");
      els.recommendationTable.classList.remove("is-anchor-table", "is-long-table");
      els.observationPool.hidden = true;
      setLongControls(false);
      setLongLayout(false);
      if (els.longSidebar) els.longSidebar.hidden = true;
      const definition = render.historyTable();
      els.tableColumns.innerHTML = definition.columns;
      els.tableHead.innerHTML = definition.head;
      renderTableState(`${selection.strategyLabel(strategy)}策略在 ${selectedDate} 没有荐股数据`, 6);
      setNotice("已保留所选历史日期", "idle");
    };
    return { renderLoadingState, renderMissingHistoricalDate, renderTableState, setLongControls, setLongLayout, setNotice };
  }

  function renderSummary(els, payload, items, observationState, firstVisible, selection, render, statusPayload) {
    const coverage = payload && payload.coverage || {};
    const strategyQuality = strategyInputQuality(payload, statusPayload);
    const inputQuality = strategyQuality;
    const marketWarmup = payload && payload.status === "not_ready" && !inputQuality
      ? marketWarmupStatus(statusPayload)
      : null;
    const pipeline = inputQuality && inputQuality.pipeline
      || marketWarmup && marketWarmup.pipeline
      || null;
    const runtimeSummary = inputQuality && inputQuality.summary
      || marketWarmup && marketWarmup.summary
      || {};
    const strategySummary = strategyQuality && strategyQuality.summary
      || marketWarmup && marketWarmup.summary
      || {};
    const evidence = pipelineStage(pipeline, "evidence_score");
    const action = pipelineStage(pipeline, "action_gate");
    const concentration = pipelineStage(pipeline, "concentration");
    const evaluated = pipeline
      ? finiteNonNegativeInteger(evidence && evidence.output_count)
      : finiteNonNegativeInteger(coverage.evaluated_count);
    const executableCount = finiteNonNegativeInteger(
      concentration && pipelineFacet(concentration, "selected_executable")?.count,
    ) ?? items.filter((item) => item.action === "executable").length;
    const observedCount = finiteNonNegativeInteger(
      concentration && pipelineFacet(concentration, "selected_observe")?.count,
    ) ?? items.filter((item) => item.action === "observe").length;
    const observed = observationSummary(payload, observationState, observedCount);
    const scoreSummary = selection.recommendationSummary(payload, items);
    const highestRuntimeScore = finiteNumber(runtimeSummary.highest_final_score);
    const topScore = highestRuntimeScore == null
      ? visibleScore(scoreSummary.topScore)
      : highestRuntimeScore.toFixed(2);
    renderInputQuality(els, payload, items, strategyQuality, marketWarmup);
    if (payload.strategy === "long") {
      els.funnelStatus.textContent = "不适用";
      els.funnelStages.textContent = "长期固定观察池不经过短线过滤、评分与正式推荐链路";
      els.funnelScoreRange.textContent = "评分范围 不适用";
      els.funnelMeta.textContent = "长期固定观察池不评分、不产生推荐";
    } else {
      const actionEligible = finiteNonNegativeInteger(action && action.output_count);
      const selected = finiteNonNegativeInteger(concentration && concentration.output_count)
        ?? (payload.status === "not_ready" ? null : executableCount + observedCount);
      els.funnelStatus.textContent = `${displayCount(evaluated)} → ${displayCount(actionEligible)} → ${displayCount(selected)}`;
      els.funnelStages.textContent = pipeline
        ? decisionPipelineDetails(pipeline)
        : "当前快照未提供逐阶段运行观测";
      els.funnelScoreRange.textContent = finalScoreRange(pipeline);
      els.funnelMeta.textContent = `完整评分 → 动作合格 → 最终入池 · 正式 ${executableCount} · 观察 ${observed} · 最高 ${topScore}`;
    }
    const marketFreshness = currentMarketFreshness(payload, statusPayload, strategySummary, firstVisible);
    const runtimeSource = marketFreshness.source;
    els.quoteSource.textContent = runtimeSource
      ? render.sourceLabel(runtimeSource)
      : firstVisible && firstVisible.source
        ? render.sourceLabel(firstVisible.source)
        : visibleText(strategySummary.latest_quote_source)
          ? render.sourceLabel(strategySummary.latest_quote_source)
          : "来源不可用";
    renderBudgetSummary(els, statusPayload && statusPayload.deepseek_budget, payload);
    renderTopScores(els, payload, items);
    els.inputQualityStrategy.textContent = selection.strategyLabel(payload.strategy);
    els.inputQualityScoreTime.textContent = payload.strategy === "long"
      ? "长期策略不评分"
      : payload.observed_at
        ? `评分于 ${render.formatTime(payload.observed_at)} 完成`
        : payload.status === "not_ready" ? "等待本轮评分完成" : "评分时间不可用";
    renderPublicationStatus(els, payload, statusPayload);
  }

  function renderInputQuality(els, payload, items, inputQuality, marketWarmup) {
    if (payload && payload.strategy === "long") {
      els.inputQualityStatus.textContent = "不适用";
      els.inputQualityStages.textContent = "长期固定观察池不经过短线评分输入链路";
      els.inputQualityMeta.textContent = "长期固定观察池不评分";
      els.inputQualityBlockers.textContent = "本轮阻断：不适用";
      els.inputQualityDegradations.textContent = "仅降级，不代表股票存在风险：不适用";
      return;
    }
    const quoteAvailability = quoteAvailabilitySummary(items);
    const runtimeSummary = inputQuality && inputQuality.summary || marketWarmup && marketWarmup.summary || {};
    const runtimeTotal = finiteNonNegativeInteger(runtimeSummary.quote_total_count);
    const runtimeAvailable = finiteNonNegativeInteger(runtimeSummary.quote_covered_count);
    const pipeline = inputQuality && inputQuality.pipeline || marketWarmup && marketWarmup.pipeline;
    if (inputQuality || marketWarmup) {
      const readiness = inputQuality || marketWarmup;
      const acquisitionPending = ["candidate_quotes_pending", "scoring_pending"].includes(readiness.primary_blocker);
      const coverageStage = pipelineStage(pipeline, "input_coverage");
      const securityMaster = finiteNonNegativeInteger(pipelineFacet(coverageStage, "security_master")?.count);
      const history = finiteNonNegativeInteger(pipelineFacet(coverageStage, "history")?.count);
      els.inputQualityStages.textContent = inputPipelineDetails(pipeline);
      if (runtimeTotal == null || runtimeAvailable == null || acquisitionPending || securityMaster == null || history == null) {
        els.inputQualityStatus.textContent = "评分输入准备中";
        els.inputQualityMeta.textContent = runtimeTotal != null && runtimeAvailable != null
          ? `行情 ${runtimeAvailable} / ${runtimeTotal} · 基础资料与历史待计算`
          : "行情、基础资料与历史待计算";
        els.inputQualityBlockers.textContent = "本轮阻断：评分输入尚未完成";
        els.inputQualityDegradations.textContent = "仅降级，不代表股票存在风险：待评分后核验";
        return;
      }
      const candidate = finiteNonNegativeInteger(inputQuality.candidate_count) ?? runtimeTotal;
      const scored = finiteNonNegativeInteger(inputQuality.candidate_scored_count)
        ?? finiteNonNegativeInteger(pipelineStage(pipeline, "evidence_score")?.output_count) ?? 0;
      els.inputQualityStatus.textContent = `可评分 ${scored} / 候选 ${candidate}`;
      els.inputQualityMeta.textContent = `历史 ${history} / ${candidate} · ${percent(history, candidate)} · 证券资料 ${securityMaster} / ${candidate}`;
      renderInputQualityReasons(els, inputQuality, candidate, history, securityMaster);
      return;
    }
    els.inputQualityStatus.textContent = inputQuality ? "评分输入待更新" : "评分输入待更新";
    els.inputQualityStages.textContent = "当前快照未提供评分输入阶段观测";
    els.inputQualityMeta.textContent = quoteAvailability.total
      ? `当前名单行情 ${quoteAvailability.available} / ${quoteAvailability.total}`
      : "当前无评分输入数据";
    els.inputQualityBlockers.textContent = "本轮阻断：等待评分输入质量";
    els.inputQualityDegradations.textContent = "仅降级，不代表股票存在风险：待计算";
  }

  function renderInputQualityReasons(els, inputQuality, candidate, history, securityMaster) {
    const blockers = [];
    const historyMissing = Math.max(0, candidate - history);
    const securityMissing = Math.max(0, candidate - securityMaster);
    if (historyMissing) blockers.push(`历史不足 ${historyMissing} 只`);
    if (securityMissing) blockers.push(`必要资料缺失 ${securityMissing} 只`);
    els.inputQualityBlockers.textContent = `本轮阻断：${blockers.length ? blockers.join(" · ") : "无"}`;
    const optional = inputQuality && inputQuality.candidate_optional_reason_counts || {};
    const labels = [
      ["corporate_risk_history_unavailable", "风险历史未核验"],
      ["structured_risk_unavailable", "结构化风险未就绪"],
      ["cross_source_deviation", "跨源价格偏差"],
      ["board_data_reliability_below_threshold", "板块资料可靠度不足"],
      ["board_identity_degraded", "板块资料可靠度不足"],
    ];
    const degradations = labels.map(([code, label]) => {
      const count = finitePositiveInteger(optional[code]);
      return count == null ? null : `${label} ${count} 只`;
    }).filter(Boolean);
    els.inputQualityDegradations.textContent = `仅降级，不代表股票存在风险：${degradations.length ? degradations.join(" · ") : "无"}`;
  }

  function percent(value, total) {
    return total > 0 ? `${(value / total * 100).toFixed(1)}%` : "—";
  }

  function quoteAvailabilitySummary(items) {
    const values = Array.isArray(items) ? items : [];
    let available = 0;
    values.forEach((item) => {
      if (quoteAvailable(item)) available += 1;
    });
    return {
      total: values.length,
      available,
      quoteMissing: values.length - available,
    };
  }

  function quoteAvailable(item) {
    if (!item || ["missing", "decision_anchor"].includes(cleanString(item.quote_status))) return false;
    const price = Number(item.price);
    const change = Number(item.pct_change);
    const sourceTime = new Date(item.source_time || "").getTime();
    return Number.isFinite(price)
      && price > 0
      && item.pct_change !== null
      && item.pct_change !== ""
      && Number.isFinite(change)
      && Boolean(visibleText(item.source))
      && Number.isFinite(sourceTime);
  }

  function visibleText(value) {
    const valueText = cleanString(value);
    return valueText && !["-", "—"].includes(valueText) ? valueText : null;
  }

  function cleanDate(value) {
    return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : "—";
  }

  function renderHealth(els, statusPayload, snapshotReasons, strategy, rememberDiagnostic) {
    const health = healthView(statusPayload, snapshotReasons, strategy);
    const activeIssues = health.issues.filter((issue) => issue.recoveryStatus !== "recovered");
    els.errorDetailsButton.hidden = activeIssues.length === 0;
    els.errorDetailsButton.dataset.level = health.level;
    els.healthBadge.textContent = `${health.level === "error" ? "错误" : "异常"} ${activeIssues.length}`;
    if (typeof rememberDiagnostic === "function") {
      health.issues.forEach((issue) => rememberDiagnostic(issue.code));
    }
    return { ...health, visibleIssues: activeIssues };
  }

  function renderBudgetSummary(els, budget, payload) {
    const available = budget && budget.available !== false;
    const used = available ? displayCount(budget.used) : "—";
    const remaining = available ? displayCount(budget.remaining) : "—";
    const explicitLimit = available ? finiteNonNegativeInteger(budget.limit) : null;
    const usedValue = available ? finiteNonNegativeInteger(budget.used) : null;
    const remainingValue = available ? finiteNonNegativeInteger(budget.remaining) : null;
    const limit = explicitLimit == null && usedValue != null && remainingValue != null
      ? String(usedValue + remainingValue)
      : explicitLimit == null ? "—" : String(explicitLimit);
    els.budgetStatus.textContent = available ? `${used} / ${remaining}` : "不可用";
    const items = payload && Array.isArray(payload.items) ? payload.items : [];
    const executable = items.filter((item) => item.action === "executable").length;
    const reviewed = items.filter((item) => item.scores && item.scores.deepseek_score != null).length;
    els.budgetMeta.textContent = payload && payload.score_status === "not_applicable"
      ? "长期策略不使用模型预算"
      : `已用 / 剩余 · 上限 ${limit} · 复核 ${reviewed}/${executable}`;
  }

  function renderPublicationStatus(els, payload, statusPayload) {
    if (!payload || payload.status === "not_ready") {
      const collecting = strategyLaneCollecting(payload, statusPayload);
      els.publicationStatus.textContent = collecting ? "采集中" : "未就绪";
      els.publicationMeta.textContent = collecting ? "等待本轮正式结果" : "等待当前策略快照";
      return;
    }
    if (payload.view === "history" || payload.historical === true) {
      els.publicationStatus.textContent = "历史只读";
      els.publicationMeta.textContent = "不会改变正式记录";
      return;
    }
    if (payload.strategy === "long") {
      els.publicationStatus.textContent = "不适用";
      els.publicationMeta.textContent = "长期固定观察池，不评分、不冻结";
      return;
    }
    const cutoff = payload.strategy === "today" ? "11:20" : "14:50";
    const strategy = STRATEGIES.has(payload.strategy) ? strategyLabel(payload.strategy) : "当前策略";
    if (payload.frozen) {
      els.publicationStatus.textContent = "已冻结";
      els.publicationMeta.textContent = `${strategy} ${cutoff} 已固化`;
      return;
    }
    els.publicationStatus.textContent = "实时滚动";
    els.publicationMeta.textContent = `${strategy} ${cutoff} 固化`;
  }

  function renderTopScores(els, payload, items) {
    if (!els.topScoresStatus) return;
    const scoredItems = topScoredStocks(payload, items);
    const diagnostics = payload && payload.selection_diagnostics || {};
    const tomorrowCostBlocked = payload && payload.strategy === "tomorrow"
      && diagnostics.empty_reason === "no_positive_net_utility";
    const scoreIdentity = payload && payload.input_versions && payload.input_versions.score_scale
      ? payload.input_versions
      : payload && payload.draft && payload.draft.input_versions;
    const unifiedScale = scoreIdentity
      && scoreIdentity.score_scale === "weighted_evidence_quality_0_100";
    const evaluated = Number(payload && payload.coverage && payload.coverage.evaluated_count);
    const scoreEvidenceMissing = tomorrowCostBlocked && Number.isInteger(evaluated) && evaluated === 0;
    const maximum = scoreEvidenceMissing ? null : finiteNumber(diagnostics.maximum_final_score);
    els.topScoresStatus.textContent = scoredItems.length
      ? scoredItems.map((item) => `${item.score.toFixed(2)} - ${item.code} - ${item.name}`).join("\n")
      : scoreEvidenceMissing
        ? "暂无可核验评分"
        : maximum == null
          ? "暂无评分数据"
          : tomorrowCostBlocked
            ? unifiedScale
              ? `最高统一评分 ${maximum.toFixed(2)}`
              : `最高相对信号分 ${maximum.toFixed(2)}`
            : `最高分 ${maximum.toFixed(2)}`;
    if (els.topScoresMeta) {
      els.topScoresMeta.textContent = scoreEvidenceMissing
        ? "明日冻结记录 · 已评分证据不完整"
        : tomorrowCostBlocked
          ? unifiedScale
            ? "统一 0–100 质量分 · 未通过成本门"
            : "旧明日相对排名 · 未通过成本门"
          : scoredItems.length
            ? unifiedScale
              ? `统一最终评分 · ${scoredItems.length} 只`
              : `策略内最终评分 · ${scoredItems.length} 只`
            : maximum == null
              ? unifiedScale
                ? "统一最终评分 · 当前无可用数据"
                : "策略内最终评分 · 当前无可用数据"
              : unifiedScale
                ? "统一最高最终分 · 当前无达到观察门槛的股票"
                : "策略内最高最终分 · 当前无达到观察门槛的股票";
    }
  }

  function topScoredStocks(payload, items) {
    if (!payload || payload.strategy === "long" || payload.score_status === "not_applicable") {
      return [];
    }
    const source = Array.isArray(payload.top_scores) && payload.top_scores.length
      ? payload.top_scores
      : Array.isArray(items) && items.length
        ? items
        : payload.status === "not_ready" && payload.draft && Array.isArray(payload.draft.top_scores)
          && payload.draft.top_scores.length
          ? payload.draft.top_scores
          : payload.status === "not_ready" && payload.draft && Array.isArray(payload.draft.items)
            ? payload.draft.items
            : [];
    return source
      .map((item, index) => {
        const scores = item && item.scores;
        const score = finiteNumber(scores && (scores.final_score ?? scores.final));
        const code = item && typeof item.code === "string" ? item.code.trim() : "";
        const name = item && typeof item.name === "string" ? item.name.trim() : "";
        return score == null || !code || !name ? null : { score, code, name, index };
      })
      .filter(Boolean)
      .sort((left, right) => right.score - left.score || left.code.localeCompare(right.code) || left.index - right.index)
      .slice(0, 3);
  }

  function updateQuoteAge(els, payload, render, statusPayload) {
    const freshness = currentMarketFreshness(payload, statusPayload, null, null);
    const sourceTime = freshness.sourceTime;
    const timestamp = new Date(sourceTime || "").getTime();
    if (!Number.isFinite(timestamp)) {
      els.quoteAge.textContent = "-";
      els.quoteTime.textContent = "-";
      if (els.quoteFreshness) els.quoteFreshness.dataset.ageState = "unknown";
      return;
    }
    const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
    els.quoteAge.textContent = formatDurationHms(seconds);
    els.quoteTime.textContent = render.formatTime(sourceTime);
    if (els.quoteFreshness) els.quoteFreshness.dataset.ageState = quoteAgeState(seconds);
  }

  function currentMarketFreshness(payload, statusPayload, strategySummary, firstVisible) {
    const market = statusPayload && statusPayload.market_data;
    const age = market && market.candidate_quote_age;
    if (payload && payload.view !== "history" && age && age.latest_source_time) {
      return {
        source: visibleText(market.candidate_quote_latest_source) || visibleText(market.active_source),
        sourceTime: age.latest_source_time,
      };
    }
    const quality = strategySummary || strategyInputQuality(payload, statusPayload) && strategyInputQuality(payload, statusPayload).summary;
    return {
      source: quality && visibleText(quality.latest_quote_source)
        || firstVisible && visibleText(firstVisible.source)
        || null,
      sourceTime: quality && quality.latest_quote_source_time
        || firstVisible && firstVisible.source_time
        || payload && payload.published_at
        || null,
    };
  }

  function quoteAgeState(seconds) {
    if (seconds <= 60) return "fresh";
    if (seconds <= 180) return "aging";
    return "stale";
  }

  function formatDurationHms(totalSeconds) {
    const numeric = Number(totalSeconds);
    const duration = Number.isFinite(numeric) ? Math.max(0, Math.floor(numeric)) : 0;
    const hours = Math.floor(duration / 3600);
    const minutes = Math.floor((duration % 3600) / 60);
    const seconds = duration % 60;
    if (hours > 0) return `${hours}时 ${minutes}分 ${seconds}秒`;
    if (minutes > 0) return `${minutes}分 ${seconds}秒`;
    return `${seconds}秒`;
  }

  function createErrorDrawer(els, beforeOpen, onVisibilityChange) {
    let issues = [];
    let returnFocus = null;
    const notify = () => {
      if (typeof onVisibilityChange === "function") onVisibilityChange();
    };
    const close = (restoreFocus) => {
      const wasOpen = els.errorDrawer.classList.contains("is-open");
      els.errorDrawer.classList.remove("is-open");
      els.errorDrawer.setAttribute("aria-hidden", "true");
      els.errorDetailsButton.setAttribute("aria-expanded", "false");
      notify();
      if (wasOpen && restoreFocus && returnFocus && typeof returnFocus.focus === "function") returnFocus.focus();
      returnFocus = null;
    };
    const open = () => {
      if (issues.length === 0) return;
      if (typeof beforeOpen === "function") beforeOpen();
      returnFocus = document.activeElement;
      els.errorDrawerContent.innerHTML = runtimeErrorRows(issues);
      els.errorDrawerTitle.textContent = issueSummaryTitle(issues);
      els.errorDrawer.classList.add("is-open");
      els.errorDrawer.setAttribute("aria-hidden", "false");
      els.errorDetailsButton.setAttribute("aria-expanded", "true");
      notify();
      els.errorDrawerClose.focus();
    };
    els.errorDetailsButton.addEventListener("click", open);
    els.errorDrawerClose.addEventListener("click", () => close(true));
    els.errorDrawerContent.addEventListener("click", copyRuntimeCode);
    return {
      close,
      isOpen: () => els.errorDrawer.classList.contains("is-open"),
      setIssues: (nextIssues) => {
        issues = Array.isArray(nextIssues) ? nextIssues : [];
        if (!els.errorDrawer.classList.contains("is-open")) return;
        els.errorDrawerContent.innerHTML = runtimeErrorRows(issues);
        els.errorDrawerTitle.textContent = issueSummaryTitle(issues);
      },
    };
  }

  async function copyRuntimeCode(event) {
    const button = event.target.closest("button[data-copy-code]");
    if (!button) return;
    const code = button.dataset.copyCode || "";
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        await navigator.clipboard.writeText(code);
      } else {
        copyTextFallback(code);
      }
      button.textContent = "已复制";
    } catch (_error) {
      selectRuntimeCode(button.previousElementSibling);
      button.textContent = "已选中，请复制";
    }
  }

  function copyTextFallback(value) {
    const input = document.createElement("textarea");
    input.value = value;
    input.setAttribute("readonly", "");
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.append(input);
    input.select();
    const copied = document.execCommand("copy");
    input.remove();
    if (!copied) throw new Error("copy_unavailable");
  }

  function selectRuntimeCode(codeElement) {
    if (!codeElement || typeof document.createRange !== "function" || typeof window.getSelection !== "function") return;
    const range = document.createRange();
    range.selectNodeContents(codeElement);
    const selectionRange = window.getSelection();
    if (!selectionRange) return;
    selectionRange.removeAllRanges();
    selectionRange.addRange(range);
  }

  function observationSummary(payload, observationState, count) {
    if (payload && payload.strategy === "long") return String(count);
    if (observationState === "open" || observationState === "empty") return String(count);
    if (observationState === "hidden_history") return "不保存";
    if (observationState === "unknown") return "状态未知";
    return "已关闭";
  }

  function displayCount(value) {
    const parsed = finiteNonNegativeInteger(value);
    return parsed == null ? "—" : String(parsed);
  }

  function visibleScore(value) {
    const parsed = finiteNumber(value);
    return parsed == null ? "—" : parsed.toFixed(2);
  }

  function inputPipelineDetails(pipeline) {
    const dynamic = pipelineStage(pipeline, "dynamic_filter");
    const board = pipelineStage(pipeline, "board_cross_section");
    const history = pipelineStage(pipeline, "strategy_history");
    const model = pipelineStage(pipeline, "model_input");
    const candidate = pipelineStage(pipeline, "candidate_score");
    const limit = pipelineStage(pipeline, "board_limit");
    const refresh = pipelineStage(pipeline, "candidate_refresh");
    const coverage = pipelineStage(pipeline, "input_coverage");
    const evidence = pipelineStage(pipeline, "evidence_score");
    const segments = [];
    segments.push(`${stageTransition("动态过滤", dynamic)}${pipelineReasonSuffix(dynamic)}`);
    if (board) {
      const observe = pipelineFacet(board, "filter_observe");
      segments.push(`板内总体 ${stageAvailableCount(board)}${metricSuffix(board, "board_reliability", "可靠度", 2)}${observe ? `（仅观察${displayCount(observe.count)}）` : ""}${pipelineReasonSuffix(board)}`);
    } else segments.push("板内总体 —");
    segments.push(`${stageTransition("策略历史", history)}${metricSuffix(history, "history_sessions", "", 0, "日")}${pipelineReasonSuffix(history)}`);
    segments.push(`${stageTransition("模型输入", model)}${percentageRangeSuffix(model, "input_completeness", "完整率")}${pipelineReasonSuffix(model)}`);
    if (candidate) {
      const range = metricRange(candidate, "candidate_score");
      const details = [];
      if (range) details.push(formatRange(range, 2));
      const threshold = finiteNumber(candidate.threshold);
      if (threshold != null) details.push(`门槛${threshold.toFixed(2)}`);
      segments.push(`${stageTransition("候选分", candidate)}${details.length ? `（${details.join("，")}）` : ""}${pipelineReasonSuffix(candidate)}`);
    } else segments.push(stageTransition("候选分", null));
    segments.push(`${stageTransition("板内限额", limit)}${boardFacetSuffix(limit)}${pipelineReasonSuffix(limit)}`);
    segments.push(`${stageTransition("定向行情", refresh)}${metricSuffix(refresh, "quote_age_seconds", "年龄", 1, "秒")}${pipelineReasonSuffix(refresh)}`);
    if (coverage) {
      const quotes = pipelineFacet(coverage, "candidate_features");
      const master = pipelineFacet(coverage, "security_master");
      const histories = pipelineFacet(coverage, "history");
      segments.push(`输入完整性 行情${facetRatio(quotes)} · 证券资料${facetRatio(master)} · 历史${facetRatio(histories)}${pipelineReasonSuffix(coverage)}`);
    } else segments.push("输入完整性 行情—/— · 证券资料—/— · 历史—/—");
    segments.push(`${stageTransition("完整评分", evidence)}${metricSuffix(evidence, "base_score", "基础分", 2)}${pipelineReasonSuffix(evidence)}`);
    return segments.join(" ｜ ");
  }

  function decisionPipelineDetails(pipeline) {
    const model = pipelineStage(pipeline, "model_cost_gate");
    const local = pipelineStage(pipeline, "local_score");
    const deepseek = pipelineStage(pipeline, "deepseek_review");
    const fusion = pipelineStage(pipeline, "fusion");
    const action = pipelineStage(pipeline, "action_gate");
    const concentration = pipelineStage(pipeline, "concentration");
    const segments = [];
    segments.push(`${stageTransition("模型成本门", model)}${modelDiagnosticsSuffix(model)}${pipelineReasonSuffix(model)}`);
    segments.push(`本地评分 ${stageAvailableCount(local)}${localScoreSuffix(local)}${pipelineReasonSuffix(local)}`);
    if (deepseek) {
      if (deepseek.state === "not_applicable") segments.push("DeepSeek 不适用");
      else segments.push(`${stageTransition("DeepSeek", deepseek)}${deepseekScoreSuffix(deepseek)}${pipelineReasonSuffix(deepseek)}`);
    } else segments.push("DeepSeek —→—");
    segments.push(`融合评分 ${stageAvailableCount(fusion)}${metricSuffix(fusion, "final_score", "", 2)}${pipelineReasonSuffix(fusion)}`);
    if (action) {
      segments.push(`动作门 达观察线${facetCount(action, "observation_threshold_met")} · 达正式线${facetCount(action, "executable_threshold_met")} · 可执行${facetCount(action, "action_executable")} · 观察${facetCount(action, "action_observe")} · 不可用${facetCount(action, "action_unavailable")}${pipelineReasonSuffix(action)}`);
    } else segments.push("动作门 达观察线— · 达正式线— · 可执行— · 观察— · 不可用—");
    if (concentration) {
      segments.push(`最终入池 正式${facetCount(concentration, "selected_executable")} · 观察${facetCount(concentration, "selected_observe")}${pipelineReasonSuffix(concentration)}`);
    } else segments.push("最终入池 正式— · 观察—");
    return segments.join(" ｜ ");
  }

  function finalScoreRange(pipeline) {
    const fusion = pipelineStage(pipeline, "fusion");
    if (!fusion || !["completed", "degraded"].includes(fusion.state)) return "评分范围 —";
    const range = metricRange(fusion, "final_score");
    return range ? `评分范围 ${formatRange(range, 2)} · 最高 ${range.maximum.toFixed(2)}` : "评分范围 无样本";
  }

  function pipelineStage(pipeline, key) {
    const stages = pipeline && Array.isArray(pipeline.stages) ? pipeline.stages : [];
    return stages.find((stage) => stage && stage.key === key) || null;
  }

  function pipelineFacet(stage, key) {
    const facets = stage && Array.isArray(stage.facets) ? stage.facets : [];
    return facets.find((facet) => facet && facet.key === key) || null;
  }

  function metricRange(stage, metric) {
    const ranges = stage && Array.isArray(stage.metric_ranges) ? stage.metric_ranges : [];
    const value = ranges.find((candidate) => candidate && candidate.metric === metric);
    const minimum = finiteNumber(value && value.minimum);
    const maximum = finiteNumber(value && value.maximum);
    return minimum == null || maximum == null ? null : { minimum, maximum };
  }

  function stageTransition(label, stage) {
    if (stage && stage.state === "not_applicable") return `${label} 不适用`;
    const output = stage && stage.state === "running" && stage.output_count == null
      ? "采集中"
      : displayCount(stage && stage.output_count);
    return `${label} ${displayCount(stage && stage.input_count)}→${output}`;
  }

  function stageAvailableCount(stage) {
    const output = finiteNonNegativeInteger(stage && stage.output_count);
    return displayCount(output == null ? stage && stage.input_count : output);
  }

  function metricSuffix(stage, metric, label, precision, unit) {
    const range = metricRange(stage, metric);
    return range ? `（${label}${formatRange(range, precision)}${unit || ""}）` : "";
  }

  function percentageRangeSuffix(stage, metric, label) {
    const range = metricRange(stage, metric);
    return range ? `（${label}${(range.minimum * 100).toFixed(1)}%–${(range.maximum * 100).toFixed(1)}%）` : "";
  }

  function modelDiagnosticsSuffix(stage) {
    const definitions = [
      ["model_signal_score", "信号分", 2, false],
      ["predicted_excess_return_pct", "预测超额", 2, true],
      ["estimated_cost_pct", "成本", 2, true],
      ["predicted_net_excess_pct", "净效用", 2, true],
      ["model_disagreement_pct", "分歧", 2, true],
    ];
    const values = definitions.flatMap(([metric, label, precision, percentPoint]) => {
      const range = metricRange(stage, metric);
      if (!range) return [];
      const formatted = percentPoint
        ? `${range.minimum.toFixed(precision)}%–${range.maximum.toFixed(precision)}%`
        : formatRange(range, precision);
      return [`${label}${formatted}`];
    });
    return values.length ? `（${values.join("，")}）` : "";
  }

  function localScoreSuffix(stage) {
    const score = metricRange(stage, "local_score");
    const risk = metricRange(stage, "local_risk_penalty");
    const values = [];
    if (score) values.push(formatRange(score, 2));
    if (risk) values.push(`风险扣分${formatRange(risk, 2)}（只扣一次）`);
    return values.length ? `（${values.join("，")}）` : "";
  }

  function deepseekScoreSuffix(stage) {
    const score = metricRange(stage, "deepseek_score");
    const risk = metricRange(stage, "deepseek_risk_penalty");
    const values = [];
    if (score) values.push(`评分${formatRange(score, 2)}`);
    if (risk) values.push(`风险扣分${formatRange(risk, 2)}`);
    return values.length ? `（${values.join("，")}）` : "";
  }

  function boardFacetSuffix(stage) {
    const labels = { board_main: "主板", board_chinext: "创业板", board_star: "科创板" };
    const values = Object.entries(labels).flatMap(([key, label]) => {
      const facet = pipelineFacet(stage, key);
      return facet ? [`${label}${displayCount(facet.count)}`] : [];
    });
    return values.length ? `（${values.join(" · ")}）` : "";
  }

  function formatRange(range, precision) {
    return `${range.minimum.toFixed(precision)}–${range.maximum.toFixed(precision)}`;
  }

  function facetRatio(facet) {
    return facet ? `${displayCount(facet.count)}/${displayCount(facet.total)}` : "—/—";
  }

  function facetCount(stage, key) {
    const facet = pipelineFacet(stage, key);
    return displayCount(facet && facet.count);
  }

  function pipelineReasonSuffix(stage) {
    const reasons = stage && Array.isArray(stage.reason_counts) ? stage.reason_counts : [];
    const visible = reasons
      .filter((item) => item && finitePositiveInteger(item.count) != null)
      .sort((left, right) => right.count - left.count || String(left.reason).localeCompare(String(right.reason)))
      .slice(0, 2)
      .map((item) => {
        const reason = cleanString(item.reason);
        const actionLabel = window.TraderRender && typeof window.TraderRender.actionReason === "function"
          ? window.TraderRender.actionReason(reason)
          : null;
        const label = actionLabel && actionLabel !== "推荐条件暂未满足"
          ? actionLabel
          : window.TraderRender && typeof window.TraderRender.reasonLabel === "function"
            ? window.TraderRender.reasonLabel(reason)
            : reason;
        return `${label}${item.count}`;
      });
    return visible.length ? `〔主要原因 ${visible.join(" · ")}〕` : "";
  }

  function finiteNumber(value) {
    if (value == null || value === "" || typeof value === "boolean") return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function strategyInputQuality(payload, statusPayload) {
    if (!payload || payload.historical === true) return null;
    const inputQuality = statusPayload
      && statusPayload.scheduler
      && statusPayload.scheduler.input_quality;
    const value = inputQuality && inputQuality[payload.strategy];
    if (!value || typeof value !== "object") return null;
    const summary = value.summary;
    return summary && summary.trade_date === payload.trade_date ? value : null;
  }

  function recommendationReadinessStatus(payload, statusPayload) {
    const inputQuality = strategyInputQuality(payload, statusPayload);
    if (inputQuality) return inputQuality;
    return payload && payload.status === "not_ready" ? marketWarmupStatus(statusPayload) : null;
  }

  function marketWarmupStatus(statusPayload) {
    const market = statusPayload && statusPayload.market_data;
    if (!market || typeof market !== "object") return null;
    const total = finiteNonNegativeInteger(market.candidate_quote_cache_entries);
    const covered = finiteNonNegativeInteger(
      market.candidate_quote_age && market.candidate_quote_age.sample_count,
    );
    if (total == null && covered == null) return null;
    const normalizedTotal = total == null ? covered : total;
    const normalizedCovered = covered == null ? 0 : Math.min(covered, normalizedTotal);
    return {
      primary_blocker: "candidate_quotes_pending",
      pipeline: {
        current_stage: "candidate_refresh",
        stages: [{
          key: "candidate_refresh",
          state: normalizedCovered < normalizedTotal ? "running" : "completed",
          input_count: normalizedTotal,
          output_count: normalizedCovered,
          metric_ranges: [],
          facets: [],
          reason_counts: [],
        }],
      },
      summary: {
        quote_total_count: normalizedTotal,
        quote_covered_count: normalizedCovered,
        quote_missing_count: Math.max(0, normalizedTotal - normalizedCovered),
        security_identity_missing_count: null,
        security_identity_pending: true,
        latest_quote_source: visibleText(market.candidate_quote_latest_source)
          || visibleText(market.active_source),
        latest_quote_source_time: market.candidate_quote_age
          && market.candidate_quote_age.latest_source_time,
      },
    };
  }

  function strategyLaneCollecting(payload, statusPayload) {
    const lanes = statusPayload && statusPayload.scheduler && statusPayload.scheduler.lanes;
    if (!payload || !Array.isArray(lanes)) return false;
    const lane = lanes.find((candidate) => candidate && candidate.strategy === payload.strategy);
    return Boolean(lane && (lane.running === true || lane.pending === true));
  }

  function issueLabel(code, stage) {
    if (ERROR_LABELS[code]) return ERROR_LABELS[code];
    if (window.TraderRender && typeof window.TraderRender.reasonLabel === "function") {
      const label = window.TraderRender.reasonLabel(code);
      if (label !== "部分数据暂不可用") return label;
    }
    return ({
      refresh: "行情刷新暂时不可用",
      decision: "策略结果暂时无法构建",
      review: "模型复核暂时不可用",
      publish: "结果发布发生错误",
      freeze: "正式快照冻结发生错误",
      settlement: "收盘结算发生错误",
      snapshot: "快照数据质量降级",
    })[stage] || "运行链路暂时降级";
  }

  function splitStrategyPrefix(code, fallbackStrategy) {
    const separator = code.indexOf(":");
    if (separator > 0) {
      const prefix = code.slice(0, separator);
      if (STRATEGIES.has(prefix)) return { strategy: prefix, code: code.slice(separator + 1) };
    }
    return { strategy: cleanString(fallbackStrategy), code };
  }

  function stageFromCode(code) {
    const prefix = code.split(":", 1)[0];
    return STAGE_LABELS[prefix] ? prefix : "runtime";
  }

  function formatDateTime(value) {
    const parsed = new Date(value || "");
    if (!Number.isFinite(parsed.getTime())) return "时间待确认";
    return parsed.toLocaleString("zh-CN", { hour12: false });
  }

  function formatTime(value) {
    const parsed = new Date(value || "");
    if (!Number.isFinite(parsed.getTime())) return "时间待确认";
    return parsed.toLocaleTimeString("zh-CN", { hour12: false });
  }

  function cleanString(value) {
    if (typeof value !== "string") return null;
    const cleaned = value.trim();
    return cleaned ? cleaned.slice(0, 128) : null;
  }

  function strategyLabel(strategy) {
    return window.TraderSelection?.strategyLabel(strategy) || strategy;
  }

  function finitePositiveInteger(value) {
    const parsed = Number(value);
    return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
  }

  function finiteNonNegativeInteger(value) {
    if (value == null || value === "" || typeof value === "boolean") return null;
    const parsed = Number(value);
    return Number.isInteger(parsed) && parsed >= 0 ? parsed : null;
  }

  function severityRank(value) {
    return value === "error" ? 0 : 1;
  }

  function recoveryRank(value) {
    return value === "active" ? 0 : 1;
  }

  function timestamp(value) {
    const parsed = new Date(value || "").getTime();
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  window.TraderStatusView = Object.freeze({
    createDashboardStateRenderer,
    createErrorDrawer,
    formatDurationHms,
    decisionPipelineDetails,
    healthView,
    inputPipelineDetails,
    issueSummaryTitle,
    quoteAvailabilitySummary,
    recommendationReadinessStatus,
    renderBudgetSummary,
    renderHealth,
    renderInputQuality,
    renderPublicationStatus,
    renderTopScores,
    renderSummary,
    topScoredStocks,
    runtimeErrorRows,
    updateQuoteAge,
  });
})();
