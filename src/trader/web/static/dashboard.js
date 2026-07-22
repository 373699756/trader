(function () {
  "use strict";

  const state = {
    strategy: "today",
    view: "live",
    date: "",
    payload: null,
    payloads: new Map(),
    etags: new Map(),
    inflight: new Map(),
    stream: null,
    streamRetry: null,
    pollTimer: null,
    lastEventId: 0,
    requestSequence: 0,
  };
  const CACHE_MAX_AGE_MS = 30000;
  const HISTORY_REFRESH_MS = 3000;

  const els = {};

  document.addEventListener("DOMContentLoaded", init);

  function init() {
    for (const id of [
      "marketPhase", "runtimeDot", "runtimeStatus", "quoteSource", "quoteTime", "quoteAge", "streamStatus",
      "scoreTime", "budgetStatus", "headerFreeze", "lastError", "routeHealth",
      "refreshButton", "dateSelect", "recommendationCount", "executableCount", "filteredCount", "dataSource",
      "strategyVersion", "freezeStatus", "notice", "recommendationTable", "tableColumns", "tableHead", "tableBody",
      "detailDrawer", "drawerBackdrop", "drawerCode", "drawerTitle", "drawerContent", "drawerClose",
    ]) els[id] = document.getElementById(id);

    document.querySelectorAll(".strategy-tab").forEach((button) => {
      button.addEventListener("click", () => selectStrategy(button.dataset.strategy));
    });
    document.querySelectorAll(".view-tab").forEach((button) => {
      button.addEventListener("click", () => selectView(button.dataset.view));
    });
    els.dateSelect.addEventListener("change", () => {
      state.date = els.dateSelect.value;
      if (state.date) setView("official");
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
    if (!strategy || state.strategy === strategy && state.payload) return;
    state.strategy = strategy || "today";
    state.date = "";
    document.querySelectorAll(".strategy-tab").forEach((button) => {
      const active = button.dataset.strategy === state.strategy;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    const key = recommendationKey(state.strategy, state.date, state.view);
    state.payload = displayableCachedPayload(key, state.strategy, state.date, state.view);
    if (state.payload) renderPayload(state.payload);
    await Promise.all([loadDates(), loadRecommendations("strategy")]);
  }

  function selectView(view) {
    if (!view || view === state.view && !state.date) return;
    setView(view);
    state.date = "";
    els.dateSelect.value = "";
    state.payload = null;
    loadRecommendations("view");
  }

  function setView(view) {
    state.view = view === "official" ? "official" : "live";
    document.querySelectorAll(".view-tab").forEach((button) => {
      const active = button.dataset.view === state.view;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
  }

  async function loadDates() {
    const strategy = state.strategy;
    els.dateSelect.innerHTML = '<option value="">当前</option>';
    els.dateSelect.disabled = strategy === "long";
    if (strategy === "long") return;
    try {
      const response = await fetch(`/api/recommendation-dates?strategy=${encodeURIComponent(strategy)}`, { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok || strategy !== state.strategy) return;
      for (const value of payload.items || []) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        els.dateSelect.append(option);
      }
    } catch (_error) {
      if (strategy === state.strategy) setNotice("历史日期暂不可用", "warn");
    }
  }

  async function loadRecommendations(reason) {
    const requestId = ++state.requestSequence;
    const strategy = state.strategy;
    const selectedDate = state.date;
    const view = state.view;
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
      if (state.payload !== payload) {
        const previous = state.payload;
        state.payload = payload;
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
      else if (view === "live") query.set("view", "live");
      const headers = {};
      if (!selectedDate && state.etags.has(key)) headers["If-None-Match"] = state.etags.get(key);
      const response = await fetch(`/api/recommendations/${encodeURIComponent(strategy)}?${query}`, {
        headers,
        cache: "no-store",
      });
      if (response.status === 304) {
        const cached = state.payloads.get(key);
        if (cached) return cached;
        throw new Error("推荐快照缓存不可用");
      }
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error && payload.error.message ? payload.error.message : "接口请求失败");
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
    if (payload.status === "not_ready") return true;
    if (payload.historical === true || !payload.current_trade_date) return false;
    return payload.trade_date === payload.current_trade_date && payload.view === view;
  }

  function prefetchStrategies() {
    for (const strategy of ["today", "tomorrow", "d25"]) {
      requestRecommendations(strategy, "", state.view).catch(() => {});
    }
  }

  function renderPayload(payload) {
    const items = Array.isArray(payload.items) ? payload.items : [];
    els.recommendationCount.textContent = String(items.length);
    els.executableCount.textContent = String(items.filter((item) => item.action === "executable").length);
    els.filteredCount.textContent = String(payload.filtered_count || 0);
    els.dataSource.textContent = items[0] && items[0].source ? items[0].source : "-";
    els.strategyVersion.textContent = payload.strategy_version || "-";
    els.freezeStatus.textContent = payload.status === "not_ready" ? "未就绪" : payload.frozen ? "已冻结" : "实时草稿";
    const historical = payload.historical === true;
    const definition = historical ? window.TraderRender.historyTable() : window.TraderRender.currentTable();
    els.recommendationTable.classList.toggle("is-history", historical);
    els.tableColumns.innerHTML = definition.columns;
    els.tableHead.innerHTML = definition.head;
    if (payload.status === "not_ready") {
      renderTableState("流水线已启动，当前策略尚无可用快照", historical ? 6 : 9);
      setNotice("当前策略尚未发布快照", "warn");
      return;
    }
    if (items.length === 0) {
      renderTableState("当前门槛下没有推荐结果", historical ? 6 : 9);
    } else {
      els.tableBody.innerHTML = window.TraderRender.rows(items, historical);
    }
    if (payload.stale) setNotice("行情已过期，当前结果仅供观察", "warn");
    else if ((payload.degraded_reasons || []).length) setNotice(`降级：${payload.degraded_reasons.join("、")}`, "warn");
    else if (payload.frozen) setNotice(`已冻结于 ${window.TraderRender.formatDateTime(payload.published_at)}`, "ok");
    else if (payload.view === "live") setNotice(`临时实时 · ${window.TraderRender.formatDateTime(payload.published_at)} · 不替代正式冻结`, "warn");
    else setNotice(`快照 ${window.TraderRender.formatDateTime(payload.published_at)} · ${payload.fusion_mode}`, "ok");
    updateQuoteAge();
  }

  function renderTableState(message, columns) {
    els.tableBody.innerHTML = `<tr><td class="table-state" colspan="${columns || 9}">${window.TraderRender.escapeHtml(message)}</td></tr>`;
  }

  function patchLiveRows(previous, payload) {
    if (!previous || !payload || previous.snapshot_id !== payload.snapshot_id) return false;
    if (previous.historical !== payload.historical) return false;
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
      currentRow.replaceWith(holder.firstElementChild);
    }
    return true;
  }

  function setNotice(message, level) {
    els.notice.textContent = message;
    els.notice.dataset.level = level || "idle";
  }

  function selectRow(event) {
    const row = event.target.closest("tr[data-code]");
    if (!row || !state.payload) return;
    const item = (state.payload.items || []).find((candidate) => candidate.code === row.dataset.code);
    if (!item) return;
    els.drawerCode.textContent = `${item.code || "-"} · ${item.industry || "未分类"}`;
    els.drawerTitle.textContent = `${item.name || "股票"} 评分明细`;
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
      els.marketPhase.textContent = phaseLabel(payload.phase || "closed");
      els.lastError.textContent = payload.last_error || "无";
      const deepseek = payload.dependencies && payload.dependencies.deepseek;
      const budget = deepseek && deepseek.budget;
      els.budgetStatus.textContent = budget && budget.available === false
        ? "不可用"
        : budget ? `${budget.used} / ${budget.remaining}` : "0 / 188";
      const market = payload.dependencies && payload.dependencies.market_data;
      const route = market && market.route;
      els.quoteSource.textContent = market && market.active_source ? market.active_source : "-";
      const routeStatus = route && route.status ? route.status : "idle";
      const routeState = route && route.degraded ? `${routeStatus}/降级` : `${routeStatus}/稳定`;
      const routeFallback = route && route.fallback_reason ? ` (${route.fallback_reason})` : "";
      const routeVendor = route && route.used_vendor ? ` · ${route.used_vendor}` : "";
      els.routeHealth.textContent = `${routeState}${routeFallback}${routeVendor}`;
      const attempted = route && Array.isArray(route.attempted_vendors) ? route.attempted_vendors : [];
      els.routeHealth.title = attempted
        .map((vendor) => `${vendor.name} ${vendor.status}${vendor.error ? `: ${vendor.error}` : ""}`)
        .join(" -> ");
      const score = state.payload && state.payload.published_at;
      els.scoreTime.textContent = score ? window.TraderRender.formatTime(score) : "-";
      els.headerFreeze.textContent = state.payload
        ? state.payload.status === "not_ready" ? "未就绪" : state.payload.frozen ? "已冻结" : "草稿"
        : "-";
      reconcileRecommendationIdentity(payload);
      updateQuoteAge();
    } catch (_error) {
      els.runtimeStatus.textContent = "状态不可用";
      els.runtimeDot.dataset.state = "error";
      els.routeHealth.textContent = "-";
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
    stream.addEventListener("recommendations", (event) => {
      rememberEvent(event);
      let published = null;
      try { published = JSON.parse(event.data); } catch (_error) { published = null; }
      if (!state.date && published && published.strategy === state.strategy) loadRecommendations("stream");
    });
    stream.addEventListener("live_overlay", (event) => {
      rememberEvent(event);
      let overlay = null;
      try { overlay = JSON.parse(event.data); } catch (_error) { overlay = null; }
      if (overlay && overlay.strategy === state.strategy) {
        loadRecommendations(state.date ? "history_overlay" : "overlay");
      }
    });
    stream.addEventListener("resync_required", (event) => {
      rememberEvent(event);
      if (!state.date) loadRecommendations("resync");
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

  function phaseLabel(value) {
    return ({
      closed: "休市",
      warmup: "共享预热",
      today_observe: "今早观察",
      today_main: "今早主执行",
      today_late: "今早降级执行",
      midday: "午间暂停",
      afternoon: "午后主审",
      final_review: "最终补审",
      deepseek_cutoff: "模型截止",
      final_quote: "最终报价",
      frozen: "冻结窗口",
      after_close: "收盘后",
    })[value] || value;
  }
})();
