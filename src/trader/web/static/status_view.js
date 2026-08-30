(function () {
  "use strict";

  const STRATEGY_LABELS = {
    today: "今早",
    tomorrow: "明日",
    d25: "2-5日",
    long: "长期",
  };

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
    const strategy = STRATEGY_LABELS[issue.strategy] || (issue.strategy ? "其他策略" : "系统");
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
      els.noticeText.textContent = message;
      els.notice.dataset.level = level === "warning" ? "warn" : level || "idle";
    };
    const renderLoadingState = () => {
      els.dataReadinessStatus.textContent = "-";
      els.dataReadinessMeta.textContent = "正在读取数据可用性";
      els.funnelStatus.textContent = "-";
      els.funnelMeta.textContent = "正在读取推荐漏斗";
      els.scoreTime.textContent = "-";
      els.headerFreeze.textContent = "-";
      els.freezeMeta.textContent = "等待当前策略快照";
      els.quoteTime.textContent = "-";
      els.quoteAge.textContent = "-";
      els.quoteSource.textContent = "来源不可用";
      els.snapshotStrategy.textContent = selection.strategyLabel(state.strategy);
      els.snapshotDate.textContent = "—";
      els.snapshotMeta.textContent = "等待行情与策略数据";
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
      els.dataReadinessStatus.textContent = "—";
      els.dataReadinessMeta.textContent = "当前无数据可检查";
      els.funnelStatus.textContent = "— → — → 0";
      els.funnelMeta.textContent = "正式 0 · 观察 不保存";
      els.scoreTime.textContent = "-";
      els.headerFreeze.textContent = "-";
      els.freezeMeta.textContent = "历史日期无正式快照";
      els.quoteTime.textContent = "-";
      els.quoteAge.textContent = "-";
      els.quoteSource.textContent = "来源不可用";
      els.snapshotStrategy.textContent = selection.strategyLabel(strategy);
      els.snapshotDate.textContent = selectedDate || "—";
      els.snapshotMeta.textContent = "所选历史日期无正式快照";
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
    const inputQuality = payload && payload.status === "not_ready" ? strategyQuality : null;
    const marketWarmup = payload && payload.status === "not_ready" && !inputQuality
      ? marketWarmupStatus(statusPayload)
      : null;
    const runtimeFunnel = inputQuality && inputQuality.supply_funnel
      || marketWarmup && marketWarmup.supply_funnel
      || {};
    const runtimeSummary = inputQuality && inputQuality.summary
      || marketWarmup && marketWarmup.summary
      || {};
    const strategySummary = strategyQuality && strategyQuality.summary
      || marketWarmup && marketWarmup.summary
      || {};
    const useRuntime = Boolean(inputQuality || marketWarmup);
    const candidate = displayCount(useRuntime ? runtimeFunnel.requested_candidates : coverage.candidate_count);
    const evaluated = displayCount(useRuntime ? runtimeFunnel.full_scored : coverage.evaluated_count);
    const rejected = displayCount(useRuntime ? runtimeFunnel.filter_reject : coverage.rejected_count);
    const runtimeExecutable = finiteNonNegativeInteger(runtimeFunnel.selected_executable);
    const executableCount = useRuntime
      ? displayCount(runtimeExecutable == null ? runtimeFunnel.action_executable : runtimeExecutable)
      : items.filter((item) => item.action === "executable").length;
    const observedCount = inputQuality
      ? finiteNonNegativeInteger(runtimeFunnel.selected_observe) || 0
      : marketWarmup
        ? payload.draft && Array.isArray(payload.draft.items) ? payload.draft.items.length : 0
      : items.filter((item) => item.action === "observe").length;
    const observed = observationSummary(payload, observationState, observedCount);
    const scoreSummary = selection.recommendationSummary(payload, items);
    const highestRuntimeScore = finiteNumber(runtimeSummary.highest_final_score);
    const topScore = highestRuntimeScore == null
      ? useRuntime ? "—" : scoreSummary.topScore
      : highestRuntimeScore.toFixed(2);
    renderDataReadiness(els, items, runtimeSummary, inputQuality || marketWarmup);
    if (payload.strategy === "long") {
      els.funnelStatus.textContent = "不适用";
      els.funnelMeta.textContent = "长期固定观察池不评分、不产生推荐";
    } else {
      const acquisitionPending = inputQuality
        && ["candidate_quotes_pending", "scoring_pending"].includes(inputQuality.primary_blocker);
      els.funnelStatus.textContent = marketWarmup || acquisitionPending
        ? `${candidate} → 采集中 → 0`
        : `${candidate} → ${evaluated} → ${executableCount}`;
      els.funnelMeta.textContent = marketWarmup || acquisitionPending
        ? `过滤 待计算 · 观察草稿 ${payload.draft ? observedCount : observationState === "warming" ? "正在生成" : "未形成"} · 最高 —`
        : useRuntime
        ? `过滤 ${rejected} · 观察草稿 ${observedCount} · 最高 ${topScore}`
        : `过滤 ${rejected} · 观察 ${observed} · 最高 ${topScore}`;
    }
    const runtimeSource = useRuntime && visibleText(strategySummary.latest_quote_source)
      ? strategySummary.latest_quote_source
      : null;
    els.quoteSource.textContent = runtimeSource
      ? render.sourceLabel(runtimeSource)
      : firstVisible && firstVisible.source
        ? render.sourceLabel(firstVisible.source)
        : visibleText(strategySummary.latest_quote_source)
          ? render.sourceLabel(strategySummary.latest_quote_source)
          : "来源不可用";
    renderBudgetSummary(els, statusPayload && statusPayload.deepseek_budget, payload);
    renderFreezeSummary(els, payload, render, statusPayload);
    els.snapshotStrategy.textContent = selection.strategyLabel(payload.strategy);
    els.snapshotDate.textContent = cleanDate(payload.trade_date);
  }

  function renderDataReadiness(els, items, runtimeSummary, inputQuality) {
    const quoteAvailability = quoteAvailabilitySummary(items);
    const runtimeTotal = finiteNonNegativeInteger(runtimeSummary && runtimeSummary.quote_total_count);
    const runtimeAvailable = finiteNonNegativeInteger(runtimeSummary && runtimeSummary.quote_covered_count);
    if (!quoteAvailability.total && runtimeTotal != null && runtimeAvailable != null) {
      const acquisitionPending = inputQuality
        && ["candidate_quotes_pending", "scoring_pending"].includes(inputQuality.primary_blocker);
      const funnel = inputQuality && inputQuality.supply_funnel;
      const securityMaster = finiteNonNegativeInteger(funnel && funnel.security_master);
      const history = finiteNonNegativeInteger(funnel && funnel.history);
      if (acquisitionPending || securityMaster == null || history == null) {
        els.dataReadinessStatus.textContent = "准备中";
        els.dataReadinessMeta.textContent = `行情 ${runtimeAvailable} / ${runtimeTotal} · 基础资料待评分 · 历史待计算`;
        return;
      }
      els.dataReadinessStatus.textContent = `基础资料 ${securityMaster} / ${runtimeTotal}`;
      els.dataReadinessMeta.textContent = `行情 ${runtimeAvailable} / ${runtimeTotal} · 历史有效 ${history}`;
      return;
    }
    els.dataReadinessStatus.textContent = quoteAvailability.total
      ? `行情 ${quoteAvailability.available} / ${quoteAvailability.total}`
      : "—";
    els.dataReadinessMeta.textContent = quoteAvailability.total
      ? `当前名单缺行情 ${quoteAvailability.quoteMissing}`
      : "当前无数据可检查";
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
    els.healthPanel.dataset.level = health.level;
    els.healthBadge.textContent = health.badge;
    els.errorDetailsButton.hidden = health.issues.length === 0;
    els.errorDetailsButton.textContent = health.issues.length ? `查看全部 ${health.issues.length}项` : "查看全部";
    if (health.primary) {
      els.lastError.textContent = health.primary.message;
      els.lastErrorMeta.textContent = health.primary.meta;
    } else if (health.level === "normal") {
      els.lastError.textContent = "系统运行正常";
      els.lastErrorMeta.textContent = health.issues.length ? "最近问题均已恢复" : "当前没有活动问题";
    } else {
      els.lastError.textContent = "运行状态暂时不可用";
      els.lastErrorMeta.textContent = "请查看错误详情或服务状态";
    }
    if (typeof rememberDiagnostic === "function") {
      health.issues.forEach((issue) => rememberDiagnostic(issue.code));
    }
    return health;
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

  function renderFreezeSummary(els, payload, render, statusPayload) {
    if (!payload || payload.status === "not_ready") {
      const collecting = strategyLaneCollecting(payload, statusPayload);
      els.headerFreeze.textContent = collecting ? "采集中" : "未就绪";
      els.freezeMeta.textContent = collecting ? "首次评分正在运行" : "等待当前策略快照";
      return;
    }
    if (payload.strategy === "long") {
      els.headerFreeze.textContent = "不适用";
      els.freezeMeta.textContent = "长期观察 · 不评分、不冻结";
      return;
    }
    if (payload.frozen) {
      els.headerFreeze.textContent = "已冻结";
      els.freezeMeta.textContent = payload.frozen_at
        ? `冻结于 ${render.formatTime(payload.frozen_at)}`
        : `${payload.strategy === "today" ? "11:20" : "14:50"} 正式冻结`;
      return;
    }
    els.headerFreeze.textContent = "滚动中";
    els.freezeMeta.textContent = `${payload.strategy === "today" ? "11:20" : "14:50"} 冻结`;
  }

  function updateQuoteAge(els, payload, render, statusPayload) {
    const item = payload && payload.items && payload.items[0];
    const inputQuality = strategyInputQuality(payload, statusPayload);
    const sourceTime = item && item.source_time
      || inputQuality && inputQuality.summary && inputQuality.summary.latest_quote_source_time
      || statusPayload && statusPayload.market_data
        && statusPayload.market_data.candidate_quote_age
        && statusPayload.market_data.candidate_quote_age.latest_source_time
      || payload && payload.published_at;
    const timestamp = new Date(sourceTime || "").getTime();
    if (!Number.isFinite(timestamp)) {
      els.quoteAge.textContent = "-";
      els.quoteTime.textContent = "-";
      els.snapshotMeta.textContent = "行情时间不可用";
      return;
    }
    const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
    els.quoteAge.textContent = formatDurationHms(seconds);
    els.quoteTime.textContent = render.formatTime(sourceTime);
    els.snapshotMeta.textContent = `${els.quoteSource.textContent} · 数据年龄 ${els.quoteAge.textContent} · ${els.quoteTime.textContent}`;
  }

  function formatDurationHms(totalSeconds) {
    const numeric = Number(totalSeconds);
    const duration = Number.isFinite(numeric) ? Math.max(0, Math.floor(numeric)) : 0;
    const hours = Math.floor(duration / 3600);
    const minutes = Math.floor((duration % 3600) / 60);
    const seconds = duration % 60;
    if (hours > 0) return `${hours}h ${minutes}m ${seconds}s`;
    if (minutes > 0) return `${minutes}m ${seconds}s`;
    return `${seconds}s`;
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
      els.errorDrawerTitle.textContent = `最近错误 · ${issues.length}项`;
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
        els.errorDrawerTitle.textContent = `最近错误 · ${issues.length}项`;
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
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed >= 0 ? String(Math.trunc(parsed)) : "—";
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
      supply_funnel: {
        requested_candidates: normalizedTotal,
        candidate_features: normalizedCovered,
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
      if (STRATEGY_LABELS[prefix]) return { strategy: prefix, code: code.slice(separator + 1) };
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
    healthView,
    quoteAvailabilitySummary,
    recommendationReadinessStatus,
    renderBudgetSummary,
    renderHealth,
    renderDataReadiness,
    renderSummary,
    runtimeErrorRows,
    updateQuoteAge,
  });
})();
