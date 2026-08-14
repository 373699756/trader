(function () {
  "use strict";

  const state = {
    strategy: "today",
    view: "current",
    date: "",
    payload: null,
    payloads: new Map(),
    etags: new Map(),
    inflight: new Map(),
    stream: null,
    streamRetry: null,
    pollTimer: null,
    lastEventId: 0,
    projectionVersion: "",
    requestSequence: 0,
    selectionSequence: 0,
    selectedDateAvailability: "available",
    runtimePhase: "",
    statusPayload: null,
    longScope: "chokepoint",
    longGroup: "",
  };
  const CACHE_MAX_AGE_MS = 30000;
  const HISTORY_REFRESH_MS = 3000;
  const PATCH_LATENCY_SAMPLE_CAPACITY = 256;
  const selection = window.TraderSelection;
  const longGroups = window.TraderLongGroups;
  const formatters = window.TraderDashboardFormatters;
  const statusView = window.TraderStatusView;
  const patchDependencyMissing = !window.TraderDashboardPatches;
  const patches = window.TraderDashboardPatches || fallbackDashboardPatches();
  const patchToPaintSamples = [];
  const diagnostics = {
    recommendationRequests: 0,
    recommendationFullResponses: 0,
    recommendationNotModified: 0,
    recommendationPatchesApplied: 0,
    overlayPatchesApplied: 0,
    resyncRequests: 0,
    fullResponseBytes: 0,
    incrementalSseBytes: 0,
    patchToPaintDroppedSamples: 0,
    resyncReasons: {},
    browserErrors: [],
    runtimeDiagnostics: [],
  };
  if (patchDependencyMissing) {
    diagnostics.browserErrors.push("dependency_missing:TraderDashboardPatches");
  }
  window.TraderDashboardDiagnostics = Object.freeze({
    snapshot: () => ({
      ...diagnostics,
      resyncReasons: { ...diagnostics.resyncReasons },
      browserErrors: [...diagnostics.browserErrors],
      runtimeDiagnostics: [...diagnostics.runtimeDiagnostics],
      patchToPaint: formatters.latencySummary(patchToPaintSamples),
    }),
  });
  window.addEventListener("error", (event) => recordBrowserError("error", event.message));
  window.addEventListener("unhandledrejection", (event) => recordBrowserError("unhandledrejection", event.reason));
  const els = {};
  let errorDrawer;
  let stateRenderer;
  document.addEventListener("DOMContentLoaded", init);
  function init() {
    for (const id of [
      "marketPhase", "runtimeDot", "runtimeStatus", "quoteSource", "quoteTime", "quoteAge", "streamStatus",
      "scoreTime", "budgetStatus", "budgetMeta", "headerFreeze", "freezeMeta", "lastError", "lastErrorMeta",
      "refreshButton", "dateSelect", "strategyDescription", "quoteCoverageStatus", "quoteCoverageMeta", "funnelStatus", "funnelMeta",
      "notice", "noticeText", "snapshotStrategy", "snapshotDate", "snapshotMeta", "healthPanel", "healthBadge", "errorDetailsButton",
      "recommendationTable", "tableColumns", "tableHead", "tableBody",
      "observationPool", "observationPoolMeta", "observationTable", "observationColumns", "observationHead", "observationBody",
      "longScopeTabs", "longIndustryTabs", "longStockHeader", "longStockContext",
      "detailDrawer", "drawerBackdrop", "drawerCode", "drawerTitle", "drawerContent", "drawerClose",
      "errorDrawer", "errorDrawerContent", "errorDrawerClose", "errorDrawerTitle",
    ]) els[id] = document.getElementById(id);
    Object.assign(els, { resultLayout: document.getElementById("recommendation-layout"), longSidebar: document.getElementById("long-sidebar"), longTitle: document.getElementById("long-panel-title"), longMeta: document.getElementById("long-panel-meta") });
    stateRenderer = statusView.createDashboardStateRenderer(els, state, selection, window.TraderRender);
    errorDrawer = statusView.createErrorDrawer(els, closeDrawer, syncDrawerBackdrop);
    document.querySelectorAll(".strategy-tab").forEach((button) => {
      button.addEventListener("click", () => selectStrategy(button.dataset.strategy));
    });
    els.longScopeTabs.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-scope]");
      if (!button || state.longScope === button.dataset.scope) return;
      state.longScope = button.dataset.scope;
      state.longGroup = "";
      if (state.payload) renderPayload(state.payload);
    });
    els.longIndustryTabs.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-group]");
      if (!button || state.longGroup === button.dataset.group) return;
      state.longGroup = button.dataset.group;
      if (state.payload) renderPayload(state.payload);
    });
    els.dateSelect.addEventListener("change", () => {
      state.date = els.dateSelect.value;
      state.selectedDateAvailability = "available";
      loadRecommendations("date");
    });
    els.refreshButton.addEventListener("click", () => loadRecommendations("manual"));
    [els.tableBody, els.observationBody].forEach((body) => {
      body.addEventListener("click", selectRow);
      body.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") selectRow(event);
      });
    });
    els.drawerClose.addEventListener("click", closeDrawer);
    els.drawerBackdrop.addEventListener("click", () => {
      closeDrawer();
      errorDrawer.close(false);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      closeDrawer();
      errorDrawer.close(true);
    });

    initializeStrategy();
    prefetchStrategies();
    connectStream();
    window.setInterval(loadStatus, 15000);
    window.setInterval(updateQuoteAge, 1000);
    window.setInterval(() => {
      if (state.date && document.visibilityState !== "hidden") loadRecommendations("history_overlay");
    }, HISTORY_REFRESH_MS);
  }
  async function initializeStrategy() {
    const selectionId = state.selectionSequence;
    const status = await loadStatus();
    if (selectionId !== state.selectionSequence) return;
    await selectStrategy(selection.initialStrategy(status));
  }

  async function selectStrategy(strategy) {
    const nextStrategy = strategy || "today";
    if (!selection.descriptions[nextStrategy] || state.strategy === nextStrategy && state.payload) return;
    const previousStrategy = state.strategy;
    const selectedDate = state.date;
    const selectionId = ++state.selectionSequence;
    state.requestSequence += 1;
    state.strategy = nextStrategy;
    state.payload = null;
    state.projectionVersion = "";
    if (nextStrategy !== "long") {
      state.longScope = "chokepoint";
      state.longGroup = "";
    }
    closeDrawer();
    errorDrawer.close(false);
    els.dateSelect.disabled = true;
    els.strategyDescription.textContent = selection.descriptions[nextStrategy];
    stateRenderer.setLongControls(nextStrategy === "long");
    document.querySelectorAll(".strategy-tab").forEach((button) => {
      const active = button.dataset.strategy === state.strategy;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    stateRenderer.renderLoadingState();
    const dates = await loadDates(nextStrategy, selectionId);
    if (selectionId !== state.selectionSequence) return;
    const resolved = selection.resolveStrategyDate(previousStrategy, nextStrategy, selectedDate, dates);
    state.date = resolved.date;
    state.selectedDateAvailability = resolved.availability;
    selection.renderDateOptions(els.dateSelect, state.strategy, dates, resolved.date, resolved.availability);
    if (resolved.availability === "missing") {
      stateRenderer.renderMissingHistoricalDate(nextStrategy, resolved.date);
      return;
    }
    const key = recommendationKey(state.strategy, state.date, state.view);
    state.payload = displayableCachedPayload(key, state.strategy, state.date, state.view);
    if (state.payload) renderPayload(state.payload);
    await loadRecommendations("strategy");
  }

  async function loadDates(strategy, selectionId) {
    if (strategy === "long") return [];
    try {
      const response = await fetch(`/api/v2/decisions/${encodeURIComponent(strategy)}/dates`, { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok) throw new Error("历史日期接口请求失败");
      if (selectionId !== state.selectionSequence || strategy !== state.strategy) return [];
      return Array.from(new Set((payload.dates || []).filter((value) => typeof value === "string")));
    } catch (_error) {
      if (strategy === state.strategy) stateRenderer.setNotice("历史日期暂不可用，正在直接读取所选日期", "warn");
      return null;
    }
  }

  async function loadRecommendations(reason) {
    const requestId = ++state.requestSequence;
    const strategy = state.strategy;
    const selectedDate = state.date;
    const view = state.view;
    if (
      selectedDate
      && state.selectedDateAvailability === "missing"
      && reason !== "manual"
    ) {
      stateRenderer.renderMissingHistoricalDate(strategy, selectedDate);
      return;
    }
    const key = recommendationKey(strategy, selectedDate, view);
    const cached = displayableCachedPayload(key, strategy, selectedDate, view);
    els.refreshButton.classList.add("is-busy");
    if (cached) {
      if (state.payload !== cached) {
        state.payload = cached;
        renderPayload(cached);
      }
    } else if (!state.payload || reason === "strategy" || reason === "date") {
      if (strategy === "long" && !selectedDate) {
        const fallback = longGroups.staticFallbackPayload("long_api_pending");
        state.payload = fallback;
        renderPayload(fallback);
      } else {
        stateRenderer.renderTableState("正在读取推荐快照");
      }
    }
    try {
      const payload = await requestRecommendations(strategy, selectedDate, view);
      if (requestId !== state.requestSequence) return;
      if (selectedDate) {
        state.selectedDateAvailability = "available";
        selection.markDateAvailability(els.dateSelect, selectedDate, "available");
      }
      if (state.payload !== payload) {
        const previous = state.payload;
        state.payload = payload;
        state.projectionVersion = patches.projectionVersion(payload);
        if (["overlay", "history_overlay"].includes(reason) && patchLiveRows(previous, payload)) {
          const first = payload.items && payload.items[0];
          els.quoteSource.textContent = first && first.source
            ? window.TraderRender.sourceLabel(first.source)
            : "来源不可用";
          statusView.renderQuoteCoverage(els, payload.items);
          updateQuoteAge();
        } else {
          renderPayload(payload);
        }
      }
    } catch (error) {
      if (requestId !== state.requestSequence) return;
      if (selectedDate && selection.isSnapshotNotFound(error)) {
        state.selectedDateAvailability = "missing";
        selection.markDateAvailability(els.dateSelect, selectedDate, "missing");
        stateRenderer.renderMissingHistoricalDate(strategy, selectedDate);
        return;
      }
      if (cached) {
        state.payload = cached;
        stateRenderer.setNotice("后台刷新失败，显示最近已加载快照", "warn");
      } else if (strategy === "long" && !selectedDate) {
        const fallback = longGroups.staticFallbackPayload("long_api_unavailable");
        state.payload = fallback;
        renderPayload(fallback);
        stateRenderer.setNotice("实时行情暂不可用，固定长期名单仍可查看", "warn");
      } else {
        stateRenderer.renderTableState("推荐快照读取失败");
        stateRenderer.setNotice(error instanceof Error ? error.message : "推荐快照读取失败", "error");
      }
    } finally {
      if (requestId === state.requestSequence) els.refreshButton.classList.remove("is-busy");
    }
  }

  async function requestRecommendations(strategy, selectedDate, view) {
    const key = recommendationKey(strategy, selectedDate, view);
    const pending = state.inflight.get(key);
    if (pending) return pending;
    const request = (async () => {
      const endpoint = selectedDate
        ? `/api/v2/decisions/${encodeURIComponent(strategy)}/history?date=${encodeURIComponent(selectedDate)}`
        : `/api/v2/decisions/${encodeURIComponent(strategy)}/current`;
      const headers = {};
      if (!selectedDate && state.etags.has(key)) headers["If-None-Match"] = state.etags.get(key);
      diagnostics.recommendationRequests += 1;
      const response = await fetch(endpoint, {
        headers,
        cache: "no-store",
      });
      if (response.status === 304) {
        diagnostics.recommendationNotModified += 1;
        const cached = state.payloads.get(key);
        if (cached) return cached;
        throw new Error("推荐快照缓存不可用");
      }
      const rawPayload = await response.json();
      if (!response.ok) {
        const error = new Error(rawPayload.error && rawPayload.error.message ? rawPayload.error.message : "接口请求失败");
        error.code = rawPayload.error && rawPayload.error.code ? rawPayload.error.code : "";
        error.httpStatus = response.status;
        throw error;
      }
      diagnostics.recommendationFullResponses += 1;
      const payload = normalizeV2Payload(rawPayload, strategy, selectedDate, view);
      diagnostics.fullResponseBytes += formatters.utf8Bytes(JSON.stringify(payload));
      if (payload.strategy !== strategy) throw new Error("推荐快照策略不匹配");
      if (!cacheIdentityValid(payload, strategy, selectedDate, view)) throw new Error("推荐快照身份不匹配");
      const etag = response.headers.get("ETag");
      if (etag) state.etags.set(key, etag);
      state.payloads.set(key, payload);
      return payload;
    })();
    state.inflight.set(key, request);
    try {
      return await request;
    } finally {
      if (state.inflight.get(key) === request) state.inflight.delete(key);
    }
  }

  function recommendationKey(strategy, selectedDate, view) {
    return `${strategy}:${selectedDate || view}`;
  }

  function normalizeV2Payload(raw, strategy, selectedDate, view) {
    const historical = Boolean(selectedDate);
    const coverage = raw.coverage || {};
    const items = Array.isArray(raw.items) ? raw.items.map((item) => {
      const quote = item.quote || {};
      const scores = item.scores || {};
      return {
        rank: item.rank || 0,
        code: item.code,
        name: item.name || "",
        industry: item.industry || "",
        price: quote.price,
        pct_change: quote.pct_change,
        turnover_rate: quote.turnover_rate,
        amount: quote.amount,
        market_cap: quote.market_cap,
        source: quote.source,
        source_time: quote.source_time,
        quote_status: quote.status,
        action: item.action,
        action_reason: item.action_reason,
        anchor_price: quote.price,
        anchor_source_time: quote.source_time,
        scores: {
          candidate_score: scores.candidate,
          local_score: scores.local,
          deepseek_score: scores.deepseek,
          deepseek_risk_penalty: scores.deepseek_risk_penalty,
          final_score: scores.final,
        },
        risks: (item.risk_codes || []).map((risk_code) => ({ risk_code })),
      };
    }) : [];
    return {
      ...raw,
      snapshot_id: raw.decision_version,
      projection_version: raw.decision_version,
      requested_date: historical ? raw.trade_date : null,
      current_trade_date: historical ? null : raw.trade_date,
      historical,
      view: historical ? "history" : (raw.frozen ? "official" : view),
      published_at: raw.observed_at,
      phase: raw.stage || "current",
      stale: raw.status !== "ready",
      filtered_count: coverage.rejected_count || 0,
      selection_diagnostics: {
        observation_limit: coverage.observation_count || 0,
        executable_limit: coverage.executable_count || 0,
        selected_observation_count: coverage.observation_count || 0,
        selected_executable_count: coverage.executable_count || 0,
      },
      items,
    };
  }
  function displayableCachedPayload(key, strategy, selectedDate, view) {
    const payload = state.payloads.get(key) || null;
    if (!cacheIdentityValid(payload, strategy, selectedDate, view)) return null;
    if (payload.frozen) return payload;
    const publishedAt = new Date(payload.published_at).getTime();
    if (!Number.isFinite(publishedAt) || Date.now() - publishedAt > CACHE_MAX_AGE_MS) return null;
    return payload;
  }

  function cacheIdentityValid(payload, strategy, selectedDate, view) {
    if (!payload || payload.strategy !== strategy) return false;
    if (selectedDate) {
      return payload.historical === true
        && payload.view === "history"
        && payload.requested_date === selectedDate
        && payload.trade_date === selectedDate;
    }
    if (payload.status === "not_ready") return payload.view === view;
    if (payload.historical === true || !payload.current_trade_date) return false;
    const viewMatches = view === "current"
      ? selection.currentViewMatches(strategy, payload.view)
      : payload.view === view;
    return payload.trade_date === payload.current_trade_date && viewMatches;
  }

  function prefetchStrategies() {
    for (const strategy of ["today", "tomorrow", "d25"]) {
      requestRecommendations(strategy, "", state.view).catch(() => {});
    }
  }

  function renderPayload(payload) {
    payload = longGroups.displayPayload(payload);
    state.projectionVersion = patches.projectionVersion(payload);
    const items = Array.isArray(payload.items) ? payload.items : [], historical = payload.historical === true;
    const frozenToday = window.TraderRender.isFrozenTodayView(payload);
    stateRenderer.setLongControls(payload.strategy === "long" && !historical);
    stateRenderer.setLongLayout(payload.strategy === "long" && payload.status === "ready" && !historical);
    longGroups.renderBar(els, state, payload.status === "ready" ? payload : null);
    const recommendations = longGroups.visibleRecommendations(
      payload,
      selection.visibleRecommendations(payload),
      state.longScope,
      state.longGroup,
    );
    const observationState = selection.observationDisplayState(payload, state.runtimePhase);
    const observations = selection.observationRecommendations(payload, state.runtimePhase);
    const showObservationPool = observationState === "open";
    els.observationPool.hidden = !showObservationPool;
    const observationLimit = Number(payload.selection_diagnostics && payload.selection_diagnostics.observation_limit);
    const observationFloorValue = payload.selection_diagnostics && payload.selection_diagnostics.observation_floor;
    const executableThresholdValue = payload.selection_diagnostics && payload.selection_diagnostics.executable_threshold;
    const observationFloor = observationFloorValue == null ? Number.NaN : Number(observationFloorValue);
    const executableThreshold = executableThresholdValue == null ? Number.NaN : Number(executableThresholdValue);
    const limitText = `${observations.length} / ${Number.isInteger(observationLimit) && observationLimit > 0 ? observationLimit : 6}`;
    els.observationPoolMeta.textContent = Number.isFinite(observationFloor)
      ? `${limitText} · 门槛 ${observationFloor.toFixed(2)}`
      : limitText;
    els.observationPoolMeta.title = Number.isFinite(observationFloor) && Number.isFinite(executableThreshold)
      ? `观察门槛 = 正式门槛 ${executableThreshold.toFixed(2)} - 观察余量 ${(executableThreshold - observationFloor).toFixed(2)}`
      : "";
    const firstVisible = recommendations[0] || items[0];
    statusView.renderSummary(
      els,
      payload,
      items,
      observationState,
      firstVisible,
      selection,
      window.TraderRender,
      state.statusPayload,
    );
    renderHealth(state.statusPayload || {});
    const definition = window.TraderRender.tableDefinition(payload);
    els.recommendationTable.classList.toggle("is-history", historical);
    els.recommendationTable.classList.toggle("is-anchor-table", frozenToday);
    els.recommendationTable.classList.toggle("is-long-table", payload.strategy === "long" && !historical);
    els.tableColumns.innerHTML = definition.columns;
    els.tableHead.innerHTML = definition.head;
    if (showObservationPool) {
      const observationDefinition = window.TraderRender.observationTableDefinition(payload);
      els.observationTable.classList.toggle("is-anchor-table", frozenToday);
      els.observationColumns.innerHTML = observationDefinition.columns;
      els.observationHead.innerHTML = observationDefinition.head;
    } else {
      els.observationTable.classList.remove("is-anchor-table");
      els.observationColumns.innerHTML = "";
      els.observationHead.innerHTML = "";
      els.observationBody.innerHTML = "";
    }
    if (payload.status === "not_ready") {
      const notReady = patches.notReadyMessage(payload);
      stateRenderer.renderTableState(notReady.message, window.TraderRender.tableColumnCount(payload));
      stateRenderer.setNotice(notReady.notice, "idle");
      return;
    }
    if (recommendations.length === 0) {
      const emptyMessage = historical
        ? "当前门槛下没有历史推荐结果"
        : payload.strategy === "long"
          ? longGroups.emptyMessage(payload, state.longScope)
          : payload.frozen
            ? patches.frozenEmptyMessage(payload)
            : patches.emptyRecommendationMessage(payload, observations.length);
      stateRenderer.renderTableState(emptyMessage, window.TraderRender.tableColumnCount(payload));
    } else {
      els.tableBody.innerHTML = window.TraderRender.tableRows(recommendations, payload);
    }
    if (showObservationPool) {
      if (observations.length) {
        els.observationBody.innerHTML = window.TraderRender.observationTableRows(observations, payload);
      } else {
        const message = recommendations.length
          ? "本轮无观察项；入选股票均为正式推荐"
          : patches.emptyRecommendationMessage(payload, 0);
        stateRenderer.renderTableState(message, window.TraderRender.observationTableColumnCount(payload), els.observationBody);
      }
    }
    const notice = patches.snapshotNotice(payload);
    stateRenderer.setNotice(notice.message, notice.level);
    stampRowIdentities(payload);
    updateQuoteAge();
  }
  function patchLiveRows(previous, payload) {
    if (!previous || !payload || previous.snapshot_id !== payload.snapshot_id) return false;
    if (previous.historical !== payload.historical) return false;
    if (payload.historical !== true) return false;
    const before = Array.isArray(previous.items) ? previous.items : [];
    const after = Array.isArray(payload.items) ? payload.items : [];
    if (before.length !== after.length) return false;
    const existingRows = new Map(
      Array.from(els.tableBody.querySelectorAll("tr[data-code]")).map((row) => [row.dataset.code, row]),
    );
    if (existingRows.size !== after.length) return false;
    const beforeByCode = new Map(before.map((item) => [item.code, item]));
    for (const item of after) {
      const prior = beforeByCode.get(item.code);
      const currentRow = existingRows.get(item.code);
      if (!prior || !currentRow) return false;
      if (
        prior.price === item.price
        && prior.pct_change === item.pct_change
        && prior.source_time === item.source_time
        && prior.quote_data_version === item.quote_data_version
      ) continue;
      const holder = document.createElement("tbody");
      holder.innerHTML = window.TraderRender.row(item, payload.historical === true);
      if (!holder.firstElementChild) return false;
      holder.firstElementChild.dataset.rowIdentity = rowIdentity(payload, item.code);
      currentRow.replaceWith(holder.firstElementChild);
    }
    return true;
  }
  function selectRow(event) {
    const row = event.target.closest("tr[data-code]");
    if (!row || !state.payload) return;
    const item = (state.payload.items || []).find((candidate) => candidate.code === row.dataset.code);
    if (!item) return;
    errorDrawer.close(false);
    els.drawerCode.textContent = `${item.code || "-"} · ${item.industry || "未分类"}`;
    els.drawerTitle.textContent = `${item.name || "股票"} 股票详情`;
    els.drawerContent.innerHTML = window.TraderRender.drawer(item, state.payload);
    els.detailDrawer.classList.add("is-open");
    els.detailDrawer.setAttribute("aria-hidden", "false");
    syncDrawerBackdrop();
    els.drawerClose.focus();
  }

  function closeDrawer() {
    els.detailDrawer.classList.remove("is-open");
    els.detailDrawer.setAttribute("aria-hidden", "true");
    syncDrawerBackdrop();
  }

  function syncDrawerBackdrop() {
    const open = els.detailDrawer.classList.contains("is-open") || errorDrawer && errorDrawer.isOpen();
    els.drawerBackdrop.hidden = !open;
  }

  async function loadStatus() {
    try {
      const response = await fetch("/api/v2/status", { cache: "no-store" });
      const payload = await response.json();
      state.statusPayload = payload;
      const running = Boolean(payload.runtime_started);
      const previousPhase = state.runtimePhase;
      state.runtimePhase = typeof payload.phase === "string" ? payload.phase : "";
      els.runtimeStatus.textContent = running ? "运行中" : payload.status === "not_ready" ? "未就绪" : "已停止";
      els.marketPhase.textContent = formatters.phaseLabel(payload.phase || "closed");
      const health = renderHealth(payload);
      els.runtimeDot.dataset.state = health.level === "normal" ? running ? "ok" : "warn" : health.level === "error" ? "error" : "warn";
      statusView.renderBudgetSummary(els, payload.deepseek_budget, state.payload);
      const score = state.payload && state.payload.published_at;
      els.scoreTime.textContent = state.payload && state.payload.score_status === "not_applicable"
        ? "不适用"
        : score ? window.TraderRender.formatTime(score) : "-";
      reconcileRecommendationIdentity(payload);
      if (previousPhase !== state.runtimePhase && state.payload) renderPayload(state.payload);
      updateQuoteAge();
      return payload;
    } catch (_error) {
      state.statusPayload = null;
      els.runtimeStatus.textContent = "状态不可用";
      els.runtimeDot.dataset.state = "error";
      renderHealth({
        health: { level: "error", issue_count: 1 },
        recent_errors: [{
          code: "runtime_status_unavailable",
          severity: "error",
          strategy: null,
          stage: "runtime",
          count: 1,
          recovery_status: "active",
        }],
      });
      return null;
    }
  }

  function renderHealth(statusPayload) {
    const snapshotReasons = state.payload && Array.isArray(state.payload.degraded_reasons)
      ? state.payload.degraded_reasons
      : [];
    const health = statusView.renderHealth(
      els,
      statusPayload,
      snapshotReasons,
      state.strategy,
      (code) => window.TraderRender.rememberDiagnostic(diagnostics.runtimeDiagnostics, code),
    );
    errorDrawer.setIssues(health.issues);
    return health;
  }

  function reconcileRecommendationIdentity(statusPayload) {
    if (state.date || !state.payload || !statusPayload || !statusPayload.strategies) return;
    const current = statusPayload.strategies[state.strategy];
    if (!current || !current.snapshot_id || current.snapshot_id === state.payload.snapshot_id) return;
    loadRecommendations("status_identity");
  }

  function updateQuoteAge() {
    statusView.updateQuoteAge(els, state.payload, window.TraderRender);
  }

  function connectStream() {
    if (state.stream) state.stream.close();
    const query = state.lastEventId > 0 ? `?cursor=${state.lastEventId}` : "";
    const stream = new EventSource(`/api/v2/events${query}`);
    state.stream = stream;
    els.streamStatus.textContent = "连接中";
    stream.onopen = () => {
      els.streamStatus.textContent = "实时";
      stopPolling();
      if (state.streamRetry) window.clearTimeout(state.streamRetry);
    };
    const refreshFromEvent = (event) => {
      const receivedAt = performance.now();
      rememberEvent(event);
      diagnostics.incrementalSseBytes += formatters.utf8Bytes(event.data || "");
      if (!state.date) loadRecommendations("v2_event").finally(() => recordPatchPaint(receivedAt));
    };
    stream.addEventListener("decision", refreshFromEvent);
    stream.addEventListener("overlay", refreshFromEvent);
    stream.addEventListener("resync_required", (event) => {
      rememberEvent(event);
      if (!state.date) requestRecommendationResync("server_resync");
    });
    stream.onerror = () => {
      stream.close();
      if (state.stream === stream) state.stream = null;
      els.streamStatus.textContent = "轮询";
      startPolling();
      if (state.streamRetry) window.clearTimeout(state.streamRetry);
      state.streamRetry = window.setTimeout(connectStream, 15000);
    };
  }

  function applyRecommendationPatch(patch) {
    const currentVersion = state.projectionVersion || patches.projectionVersion(state.payload);
    const decision = patches.recommendationPatchDecision(patch, state.payload, currentVersion, state.strategy, state.view);
    if (decision === "ignore_late_draft") return false;
    if (decision !== "apply") {
      requestRecommendationResync(decision);
      return false;
    }
    const current = state.payload || {};
    const removed = new Set([...(patch.removed_codes || []), ...(patch.removals || [])]);
    const merged = patch.replace === true
      ? patch.upserts
      : patches.mergePatchItems(current.items, patch.upserts, removed);
    if (!patches.topKValid(merged, patch.strategy)) {
      requestRecommendationResync("topk_mismatch");
      return false;
    }
    state.payload = {
      ...current,
      status: "ready",
      snapshot_id: patch.snapshot_id,
      projection_version: patch.projection_version || patch.snapshot_id,
      strategy: patch.strategy,
      trade_date: patch.trade_date,
      requested_date: null,
      current_trade_date: patch.current_trade_date || patch.trade_date,
      historical: false,
      view: patch.view,
      phase: patch.phase,
      published_at: patch.published_at,
      strategy_version: patch.strategy_version,
      fusion_mode: patch.fusion_mode,
      stale: patch.stale,
      frozen: patch.frozen,
      degraded_reasons: patch.degraded_reasons || [],
      filtered_count: patch.filtered_count,
      selection_diagnostics: patch.selection_diagnostics || {},
      readiness_reason: null,
      long_groups: Array.isArray(patch.long_groups) ? patch.long_groups : current.long_groups || [],
      items: merged,
      error: null,
    };
    state.projectionVersion = patches.projectionVersion(state.payload);
    const key = recommendationKey(state.strategy, state.date, state.view);
    state.payloads.set(key, state.payload);
    if (typeof patch.etag === "string" && patch.etag && patch.view === state.view) {
      state.etags.set(key, formatters.quotedEtag(patch.etag));
    }
    diagnostics.recommendationPatchesApplied += 1;
    renderPayload(state.payload);
    return true;
  }

  function applyOverlayPatch(patch) {
    if (state.date) return false;
    const decision = patches.overlayPatchDecision(patch, state.payload, state.projectionVersion, state.strategy);
    if (decision !== "apply") {
      requestRecommendationResync(decision);
      return false;
    }
    const quotes = new Map((patch.quotes || []).map((quote) => [quote.code, quote]));
    state.payload = {
      ...state.payload,
      items: (state.payload.items || []).map((item) => {
        const quote = quotes.get(item.code);
        if (!quote) return item;
        const anchor = Number(item.anchor_price);
        const current = Number(quote.price);
        const anchorToNow = Number.isFinite(anchor) && anchor > 0 && Number.isFinite(current)
          ? ((current / anchor) - 1) * 100 : null;
        return {
          ...item,
          ...quote,
          quote_status: quote.quote_status || quote.status || item.quote_status,
          anchor_to_now_pct: anchorToNow,
        };
      }),
    };
    state.payloads.set(recommendationKey(state.strategy, state.date, state.view), state.payload);
    diagnostics.overlayPatchesApplied += 1;
    renderPayload(state.payload);
    return true;
  }

  function requestRecommendationResync(reason) {
    diagnostics.resyncRequests += 1;
    diagnostics.resyncReasons[reason] = (diagnostics.resyncReasons[reason] || 0) + 1;
    loadRecommendations(`resync_${reason}`);
  }

  function recordPatchPaint(receivedAt) {
    window.requestAnimationFrame(() => {
      const elapsed = Math.max(0, performance.now() - receivedAt);
      if (patchToPaintSamples.length >= PATCH_LATENCY_SAMPLE_CAPACITY) {
        patchToPaintSamples.shift();
        diagnostics.patchToPaintDroppedSamples += 1;
      }
      patchToPaintSamples.push(elapsed);
    });
  }

  function recordBrowserError(kind, detail) {
    diagnostics.browserErrors.push(`${kind}:${String(detail || "unknown").slice(0, 300)}`);
    if (diagnostics.browserErrors.length > 20) diagnostics.browserErrors.shift();
  }

  function fallbackDashboardPatches() {
    return Object.freeze({
      emptyRecommendationMessage: () => "当前没有达到正式推荐条件的股票",
      frozenEmptyMessage: () => "正式冻结结果为空；观察池已关闭且未保存",
      mergePatchItems: (items) => items || [],
      notReadyMessage: (payload) => payload && payload.strategy === "long"
        ? { message: "长期策略当前尚无可用数据", notice: "长期策略只展示当前研究快照" }
        : { message: "当前暂无可用荐股数据", notice: "等待策略数据更新" },
      overlayPatchDecision: () => "dependency_missing",
      patchVersionValid: () => false,
      projectionVersion: (payload) => payload && (payload.projection_version || payload.snapshot_id) || "",
      recommendationPatchDecision: () => "dependency_missing",
      snapshotNotice: (payload) => ({ message: payload && payload.status === "not_ready" ? "等待策略数据更新" : "快照状态不可用", level: "idle" }),
      topKValid: () => false,
    });
  }

  function rowIdentity(payload, code) {
    return [payload.strategy, payload.trade_date, payload.view, code].map((value) => String(value || "")).join(":");
  }

  function stampRowIdentities(payload) {
    if (!payload || !Array.isArray(payload.items)) return;
    els.tableBody.querySelectorAll("tr[data-code]").forEach((row) => {
      row.dataset.rowIdentity = rowIdentity(payload, row.dataset.code);
    });
    els.observationBody.querySelectorAll("tr[data-code]").forEach((row) => {
      row.dataset.rowIdentity = rowIdentity(payload, row.dataset.code);
    });
  }

  function rememberEvent(event) {
    const parsed = Number(event.lastEventId);
    if (Number.isInteger(parsed) && parsed >= 0) state.lastEventId = parsed;
  }

  function startPolling() {
    if (state.pollTimer) return;
    state.pollTimer = window.setInterval(() => {
      loadStatus();
      if (!state.date) loadRecommendations("poll");
    }, 15000);
  }

  function stopPolling() {
    if (!state.pollTimer) return;
    window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }

})();
