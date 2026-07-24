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
    longScope: "chokepoint",
    longGroup: "",
  };
  const CACHE_MAX_AGE_MS = 30000;
  const HISTORY_REFRESH_MS = 3000;
  const PATCH_LATENCY_SAMPLE_CAPACITY = 256;
  const selection = window.TraderSelection;
  const longGroups = window.TraderLongGroups;
  const utils = window.TraderDashboardUtils;
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

  window.TraderDashboardDiagnostics = Object.freeze({
    snapshot: () => ({
      ...diagnostics,
      resyncReasons: { ...diagnostics.resyncReasons },
      browserErrors: [...diagnostics.browserErrors],
      runtimeDiagnostics: [...diagnostics.runtimeDiagnostics],
      patchToPaint: utils.latencySummary(patchToPaintSamples),
    }),
  });
  window.addEventListener("error", (event) => recordBrowserError("error", event.message));
  window.addEventListener("unhandledrejection", (event) => recordBrowserError("unhandledrejection", event.reason));
  const els = {};
  document.addEventListener("DOMContentLoaded", init);

  function init() {
    for (const id of [
      "marketPhase", "runtimeDot", "runtimeStatus", "quoteSource", "quoteTime", "quoteAge", "streamStatus",
      "scoreTime", "budgetStatus", "headerFreeze", "lastError",
      "refreshButton", "dateSelect", "strategyDescription", "recommendationCount", "executableCount", "filteredCount", "dataSource",
      "topScore", "modelReview", "dataQuality", "notice", "noticeText", "recommendationTable", "tableColumns", "tableHead", "tableBody",
      "longGroupBar", "longScopeTabs", "longIndustryTabs",
      "detailDrawer", "drawerBackdrop", "drawerCode", "drawerTitle", "drawerContent", "drawerClose",
    ]) els[id] = document.getElementById(id);

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
    els.tableBody.addEventListener("click", selectRow);
    els.tableBody.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") selectRow(event);
    });
    els.drawerClose.addEventListener("click", closeDrawer);
    els.drawerBackdrop.addEventListener("click", closeDrawer);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeDrawer();
    });

    selectStrategy("today");
    prefetchStrategies();
    loadStatus();
    connectStream();
    window.setInterval(loadStatus, 15000);
    window.setInterval(updateQuoteAge, 1000);
    window.setInterval(() => {
      if (state.date && document.visibilityState !== "hidden") loadRecommendations("history_overlay");
    }, HISTORY_REFRESH_MS);
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
    els.dateSelect.disabled = true;
    els.strategyDescription.textContent = selection.descriptions[nextStrategy];
    document.querySelectorAll(".strategy-tab").forEach((button) => {
      const active = button.dataset.strategy === state.strategy;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    renderLoadingState();
    const dates = await loadDates(nextStrategy, selectionId);
    if (selectionId !== state.selectionSequence) return;
    const resolved = selection.resolveStrategyDate(previousStrategy, nextStrategy, selectedDate, dates);
    state.date = resolved.date;
    state.selectedDateAvailability = resolved.availability;
    selection.renderDateOptions(els.dateSelect, state.strategy, dates, resolved.date, resolved.availability);
    if (resolved.availability === "missing") {
      renderMissingHistoricalDate(nextStrategy, resolved.date);
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
      const response = await fetch(`/api/recommendation-dates?strategy=${encodeURIComponent(strategy)}`, { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok) throw new Error("历史日期接口请求失败");
      if (selectionId !== state.selectionSequence || strategy !== state.strategy) return [];
      return Array.from(new Set((payload.items || []).filter((value) => typeof value === "string")));
    } catch (_error) {
      if (strategy === state.strategy) setNotice("历史日期暂不可用，正在直接读取所选日期", "warn");
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
      renderMissingHistoricalDate(strategy, selectedDate);
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
      renderTableState("正在读取推荐快照");
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
        state.projectionVersion = projectionVersion(payload);
        if (["overlay", "history_overlay"].includes(reason) && patchLiveRows(previous, payload)) {
          const first = payload.items && payload.items[0];
          els.dataSource.textContent = first && first.source ? first.source : "-";
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
        renderMissingHistoricalDate(strategy, selectedDate);
        return;
      }
      if (cached) {
        state.payload = cached;
        setNotice("后台刷新失败，显示最近已加载快照", "warn");
      } else {
        renderTableState("推荐快照读取失败");
        setNotice(error instanceof Error ? error.message : "推荐快照读取失败", "error");
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
      const query = new URLSearchParams({ top_n: "18" });
      if (selectedDate) query.set("date", selectedDate);
      else query.set("view", view);
      const headers = {};
      if (!selectedDate && state.etags.has(key)) headers["If-None-Match"] = state.etags.get(key);
      diagnostics.recommendationRequests += 1;
      const response = await fetch(`/api/recommendations/${encodeURIComponent(strategy)}?${query}`, {
        headers,
        cache: "no-store",
      });
      if (response.status === 304) {
        diagnostics.recommendationNotModified += 1;
        const cached = state.payloads.get(key);
        if (cached) return cached;
        throw new Error("推荐快照缓存不可用");
      }
      const payload = await response.json();
      if (!response.ok) {
        const error = new Error(payload.error && payload.error.message ? payload.error.message : "接口请求失败");
        error.code = payload.error && payload.error.code ? payload.error.code : "";
        error.httpStatus = response.status;
        throw error;
      }
      diagnostics.recommendationFullResponses += 1;
      diagnostics.fullResponseBytes += utils.utf8Bytes(JSON.stringify(payload));
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
      ? ["live", "official"].includes(payload.view)
      : payload.view === view;
    return payload.trade_date === payload.current_trade_date && viewMatches;
  }

  function prefetchStrategies() {
    for (const strategy of ["today", "tomorrow", "d25"]) {
      requestRecommendations(strategy, "", state.view).catch(() => {});
    }
  }

  function renderPayload(payload) {
    state.projectionVersion = projectionVersion(payload);
    const items = Array.isArray(payload.items) ? payload.items : [];
    const historical = payload.historical === true;
    longGroups.renderBar(els, state, payload.status === "ready" ? payload : null);
    const recommendations = longGroups.visibleRecommendations(
      payload,
      selection.visibleRecommendations(payload),
      state.longScope,
      state.longGroup,
    );
    els.recommendationCount.textContent = String(recommendations.length);
    els.executableCount.textContent = String(recommendations.filter((item) => item.action === "executable").length);
    els.filteredCount.textContent = String(payload.filtered_count || 0);
    const firstVisible = recommendations[0] || items[0];
    els.dataSource.textContent = firstVisible && firstVisible.source ? firstVisible.source : "-";
    const summary = selection.recommendationSummary(payload, recommendations);
    els.topScore.textContent = summary.topScore;
    els.modelReview.textContent = summary.modelReview;
    els.dataQuality.textContent = summary.dataQuality;
    els.dataQuality.title = summary.dataQualityTitle;
    const definition = historical ? window.TraderRender.historyTable() : window.TraderRender.currentTable();
    els.recommendationTable.classList.toggle("is-history", historical);
    els.tableColumns.innerHTML = definition.columns;
    els.tableHead.innerHTML = definition.head;
    if (payload.status === "not_ready") {
      const message = payload.strategy === "long"
        ? "长期策略当前尚无可用数据"
        : "当前暂无可用荐股数据";
      renderTableState(message, historical ? 6 : 9);
      setNotice(payload.strategy === "long" ? "长期策略只展示当前研究快照" : "等待策略数据更新", "idle");
      return;
    }
    if (recommendations.length === 0) {
      const emptyMessage = historical
        ? "当前门槛下没有历史推荐结果"
        : payload.strategy === "long"
          ? longGroups.emptyMessage(payload, state.longScope)
          : emptyRecommendationMessage(payload);
      renderTableState(emptyMessage, historical ? 6 : 9);
    } else {
      els.tableBody.innerHTML = window.TraderRender.rows(recommendations, historical);
    }
    if (payload.stale) setNotice("行情已过期，当前结果仅供观察", "warn");
    else if (payload.phase === "close_fallback") {
      const degraded = (payload.degraded_reasons || []).length
        ? ` · 降级：${window.TraderRender.reasonLabels(payload.degraded_reasons).join("、")}`
        : "";
      setNotice(`已冻结 · 收盘补算 · ${window.TraderRender.formatDateTime(payload.published_at)}${degraded}`, degraded ? "warn" : "ok");
    }
    else if ((payload.degraded_reasons || []).length) setNotice(`降级：${window.TraderRender.reasonLabels(payload.degraded_reasons).join("、")}`, "warn");
    else if (payload.frozen) setNotice(`已冻结于 ${window.TraderRender.formatDateTime(payload.published_at)}`, "ok");
    else if (payload.strategy === "long") setNotice(`当前快照 · ${window.TraderRender.formatDateTime(payload.published_at)}`, "ok");
    else if (payload.view === "live") setNotice(`实时数据 · ${window.TraderRender.formatDateTime(payload.published_at)} · 未冻结，结果可能变化`, "warn");
    else setNotice(`快照 ${window.TraderRender.formatDateTime(payload.published_at)} · ${window.TraderRender.fusionModeLabel(payload.fusion_mode)}`, "ok");
    stampRowIdentities(payload);
    updateQuoteAge();
  }

  function renderTableState(message, columns) {
    els.tableBody.innerHTML = `<tr><td class="table-state" colspan="${columns || 9}">${window.TraderRender.escapeHtml(message)}</td></tr>`;
  }

  function renderLoadingState() {
    els.recommendationCount.textContent = "-";
    els.executableCount.textContent = "-";
    els.filteredCount.textContent = "-";
    els.dataSource.textContent = "-";
    els.topScore.textContent = "-";
    els.modelReview.textContent = "-";
    els.dataQuality.textContent = "读取中";
    els.dataQuality.title = "";
    els.scoreTime.textContent = "-";
    els.headerFreeze.textContent = "-";
    els.quoteTime.textContent = "-";
    els.quoteAge.textContent = "-";
    els.recommendationTable.classList.remove("is-history");
    const definition = window.TraderRender.currentTable();
    els.tableColumns.innerHTML = definition.columns;
    els.tableHead.innerHTML = definition.head;
    if (els.longGroupBar) els.longGroupBar.hidden = true;
    renderTableState("正在读取推荐快照");
    setNotice("正在读取推荐快照", "idle");
  }

  function renderMissingHistoricalDate(strategy, selectedDate) {
    state.payload = null;
    state.projectionVersion = "";
    els.recommendationCount.textContent = "0";
    els.executableCount.textContent = "0";
    els.filteredCount.textContent = "-";
    els.dataSource.textContent = "-";
    els.topScore.textContent = "-";
    els.modelReview.textContent = "-";
    els.dataQuality.textContent = "无数据";
    els.dataQuality.title = "";
    els.scoreTime.textContent = "-";
    els.headerFreeze.textContent = "-";
    els.quoteTime.textContent = "-";
    els.quoteAge.textContent = "-";
    els.recommendationTable.classList.add("is-history");
    if (els.longGroupBar) els.longGroupBar.hidden = true;
    const definition = window.TraderRender.historyTable();
    els.tableColumns.innerHTML = definition.columns;
    els.tableHead.innerHTML = definition.head;
    const message = `${selection.strategyLabel(strategy)}策略在 ${selectedDate} 没有荐股数据`;
    renderTableState(message, 6);
    setNotice("已保留所选历史日期", "idle");
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

  function setNotice(message, level) {
    els.noticeText.textContent = message;
    els.notice.dataset.level = level || "idle";
  }

  function selectRow(event) {
    const row = event.target.closest("tr[data-code]");
    if (!row || !state.payload) return;
    const item = (state.payload.items || []).find((candidate) => candidate.code === row.dataset.code);
    if (!item) return;
    els.drawerCode.textContent = `${item.code || "-"} · ${item.industry || "未分类"}`;
    els.drawerTitle.textContent = `${item.name || "股票"} 股票详情`;
    els.drawerContent.innerHTML = window.TraderRender.drawer(item, state.payload);
    els.detailDrawer.classList.add("is-open");
    els.detailDrawer.setAttribute("aria-hidden", "false");
    els.drawerBackdrop.hidden = false;
    els.drawerClose.focus();
  }

  function closeDrawer() {
    els.detailDrawer.classList.remove("is-open");
    els.detailDrawer.setAttribute("aria-hidden", "true");
    els.drawerBackdrop.hidden = true;
  }

  async function loadStatus() {
    try {
      const response = await fetch("/api/status", { cache: "no-store" });
      const payload = await response.json();
      const running = Boolean(payload.runtime_started);
      els.runtimeStatus.textContent = running ? "运行中" : payload.status === "not_ready" ? "未就绪" : "已停止";
      els.runtimeDot.dataset.state = running ? "ok" : payload.last_error ? "error" : "warn";
      els.marketPhase.textContent = utils.phaseLabel(payload.phase || "closed");
      const rawLastError = payload.last_error || "";
      els.lastError.textContent = window.TraderRender.statusErrorLabel(rawLastError);
      window.TraderRender.rememberDiagnostic(diagnostics.runtimeDiagnostics, rawLastError);
      const deepseek = payload.dependencies && payload.dependencies.deepseek;
      const budget = deepseek && deepseek.budget;
      els.budgetStatus.textContent = budget && budget.available === false
        ? "不可用"
        : budget ? `${budget.used} / ${budget.remaining}` : "0 / 168";
      const market = payload.dependencies && payload.dependencies.market_data;
      els.quoteSource.textContent = market && market.active_source ? market.active_source : "-";
      const score = state.payload && state.payload.published_at;
      els.scoreTime.textContent = score ? window.TraderRender.formatTime(score) : "-";
      els.headerFreeze.textContent = state.payload
        ? state.payload.status === "not_ready" ? "未就绪" : state.payload.frozen ? "已冻结" : "未冻结"
        : "-";
      reconcileRecommendationIdentity(payload);
      updateQuoteAge();
    } catch (_error) {
      els.runtimeStatus.textContent = "状态不可用";
      els.runtimeDot.dataset.state = "error";
    }
  }

  function reconcileRecommendationIdentity(statusPayload) {
    if (state.date || !state.payload || !statusPayload || !statusPayload.strategies) return;
    const current = statusPayload.strategies[state.strategy];
    if (!current || !current.snapshot_id || current.snapshot_id === state.payload.snapshot_id) return;
    loadRecommendations("status_identity");
  }

  function updateQuoteAge() {
    const item = state.payload && state.payload.items && state.payload.items[0];
    if (!item || !item.source_time) {
      els.quoteAge.textContent = "-";
      els.quoteTime.textContent = "-";
      return;
    }
    const timestamp = new Date(item.source_time).getTime();
    if (!Number.isFinite(timestamp)) {
      els.quoteAge.textContent = "-";
      els.quoteTime.textContent = "-";
      return;
    }
    const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
    els.quoteAge.textContent = seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分`;
    els.quoteTime.textContent = window.TraderRender.formatTime(item.source_time);
  }

  function connectStream() {
    if (state.stream) state.stream.close();
    const query = state.lastEventId > 0 ? `?cursor=${state.lastEventId}` : "";
    const stream = new EventSource(`/api/events/stream${query}`);
    state.stream = stream;
    els.streamStatus.textContent = "连接中";
    stream.onopen = () => {
      els.streamStatus.textContent = "实时";
      stopPolling();
      if (state.streamRetry) window.clearTimeout(state.streamRetry);
    };
    stream.addEventListener("recommendation_patch", (event) => {
      const receivedAt = performance.now();
      rememberEvent(event);
      diagnostics.incrementalSseBytes += utils.utf8Bytes(event.data || "");
      let patch = null;
      try { patch = JSON.parse(event.data); } catch (_error) { patch = null; }
      if (
        !state.date
        && (!patch || !patch.strategy || patch.strategy === state.strategy)
        && applyRecommendationPatch(patch)
      ) recordPatchPaint(receivedAt);
    });
    stream.addEventListener("overlay_patch", (event) => {
      const receivedAt = performance.now();
      rememberEvent(event);
      diagnostics.incrementalSseBytes += utils.utf8Bytes(event.data || "");
      let patch = null;
      try { patch = JSON.parse(event.data); } catch (_error) { patch = null; }
      if ((!patch || !patch.strategy || patch.strategy === state.strategy) && applyOverlayPatch(patch)) {
        recordPatchPaint(receivedAt);
      }
    });
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
    const currentVersion = state.projectionVersion || projectionVersion(state.payload);
    const decision = recommendationPatchDecision(patch, state.payload, currentVersion, state.strategy, state.view);
    if (decision === "ignore_late_draft") return false;
    if (decision !== "apply") {
      requestRecommendationResync(decision);
      return false;
    }
    const current = state.payload || {};
    const removed = new Set([...(patch.removed_codes || []), ...(patch.removals || [])]);
    const merged = patch.replace === true
      ? patch.upserts
      : mergePatchItems(current.items, patch.upserts, removed);
    if (!topKValid(merged, patch.strategy)) {
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
      long_groups: Array.isArray(patch.long_groups) ? patch.long_groups : current.long_groups || [],
      items: merged,
      error: null,
    };
    state.projectionVersion = projectionVersion(state.payload);
    const key = recommendationKey(state.strategy, state.date, state.view);
    state.payloads.set(key, state.payload);
    if (typeof patch.etag === "string" && patch.etag && patch.view === state.view) {
      state.etags.set(key, utils.quotedEtag(patch.etag));
    }
    diagnostics.recommendationPatchesApplied += 1;
    renderPayload(state.payload);
    return true;
  }

  function applyOverlayPatch(patch) {
    if (state.date) return false;
    const decision = overlayPatchDecision(patch, state.payload, state.projectionVersion, state.strategy);
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
        return { ...item, ...quote, anchor_to_now_pct: anchorToNow };
      }),
    };
    state.payloads.set(recommendationKey(state.strategy, state.date, state.view), state.payload);
    diagnostics.overlayPatchesApplied += 1;
    renderPayload(state.payload);
    return true;
  }

  function patchVersionValid(patch) {
    return Boolean(patch && patch.patch_schema_version === 2 && patch.schema_version === 2);
  }

  function recommendationPatchDecision(patch, payload, currentVersion, strategy, view) {
    if (!patchVersionValid(patch) || !Array.isArray(patch.upserts)
      || !Array.isArray(patch.removed_codes) || !Array.isArray(patch.removals || [])) return "schema_mismatch";
    if (!patch.projection_version || patch.snapshot_id !== patch.projection_version
      || patch.strategy !== strategy || !["live", "official"].includes(patch.view)
      || patch.view !== (patch.frozen ? "official" : "live")) return "identity_mismatch";
    const expectedDate = payload && (payload.current_trade_date || payload.trade_date);
    if (expectedDate && patch.trade_date !== expectedDate) return "identity_mismatch";
    if (["current", "official"].includes(view) && payload && payload.frozen === true && patch.frozen !== true) {
      return "ignore_late_draft";
    }
    const baseVersion = patch.base_projection_version || patch.base_snapshot_id || "";
    if (baseVersion && baseVersion !== currentVersion) return "base_mismatch";
    if (!baseVersion && patch.replace !== true && payload && payload.status !== "not_ready") return "base_mismatch";
    if (!patchItemsValid(patch.upserts, patch.removed_codes, patch.removals || [])) return "topk_mismatch";
    return "apply";
  }

  function overlayPatchDecision(patch, payload, currentVersion, strategy) {
    if (!patchVersionValid(patch) || !Array.isArray(patch.quotes)) return "schema_mismatch";
    if (!payload || patch.strategy !== strategy || patch.trade_date !== payload.trade_date) return "identity_mismatch";
    const incomingProjection = patch.projection_version || patch.snapshot_id || "";
    if (!incomingProjection || incomingProjection !== currentVersion || patch.snapshot_id !== payload.snapshot_id) {
      return "overlay_projection_mismatch";
    }
    if (!patch.quotes.every((quote) => quote && typeof quote.code === "string" && quote.code)) {
      return "schema_mismatch";
    }
    return "apply";
  }

  function projectionVersion(payload) {
    if (!payload) return "";
    return payload.projection_version || payload.snapshot_id || "";
  }

  function emptyRecommendationMessage(payload) {
    const diagnostics = payload && payload.selection_diagnostics || {};
    const maximum = Number(diagnostics.maximum_final_score), floor = Number(diagnostics.selection_floor);
    if (diagnostics.empty_reason === "score_below_observation_floor" && diagnostics.maximum_final_score != null
      && diagnostics.selection_floor != null && Number.isFinite(maximum) && Number.isFinite(floor)) {
      return `最高评分 ${maximum.toFixed(2)}，低于观察门槛 ${floor.toFixed(2)}，本轮不荐股`;
    }
    if (diagnostics.empty_reason === "no_scored_candidates") return "本轮没有可评分候选";
    if (diagnostics.empty_reason === "risk_or_execution_blocked") return "候选达到评分门槛，但被风险或执行条件拦截";
    if (diagnostics.empty_reason === "selection_limits") return "候选达到门槛，但未通过最终集中度限制";
    return "当前没有达到正式推荐条件的股票";
  }
  function mergePatchItems(existingItems, upserts, removed) {
    const byCode = new Map((existingItems || []).map((item) => [item.code, item]));
    for (const code of removed) byCode.delete(code);
    for (const item of upserts) {
      if (item && item.code) byCode.set(item.code, item);
    }
    return Array.from(byCode.values()).sort((left, right) => {
      const leftRank = Number(left.rank);
      const rightRank = Number(right.rank);
      if (Number.isFinite(leftRank) && Number.isFinite(rightRank) && leftRank !== rightRank) return leftRank - rightRank;
      return String(left.code || "").localeCompare(String(right.code || ""));
    });
  }

  function patchItemsValid(upserts, removedCodes, removals) {
    const codes = upserts.map((item) => item && item.code);
    const removed = [...removedCodes, ...removals];
    return codes.every((code) => typeof code === "string" && code)
      && removed.every((code) => typeof code === "string" && code)
      && new Set(codes).size === codes.length
      && !codes.some((code) => removed.includes(code));
  }

  function topKValid(items, strategy) {
    const effectiveStrategy = strategy || state.strategy;
    if (!Array.isArray(items) || (effectiveStrategy !== "long" && items.length > 18)) return false;
    const codes = items.map((item) => item && item.code);
    const ranks = items.map((item) => Number(item && item.rank));
    return codes.every((code) => typeof code === "string" && code)
      && new Set(codes).size === codes.length
      && ranks.every((rank) => Number.isInteger(rank) && rank > 0)
      && new Set(ranks).size === ranks.length;
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
  function rowIdentity(payload, code) {
    return [payload.strategy, payload.trade_date, payload.view, code].map((value) => String(value || "")).join(":");
  }

  function stampRowIdentities(payload) {
    if (!payload || !Array.isArray(payload.items)) return;
    els.tableBody.querySelectorAll("tr[data-code]").forEach((row) => {
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
