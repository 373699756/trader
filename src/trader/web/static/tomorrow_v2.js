(() => {
  "use strict";

  const diagnostics = { errors: [] };
  window.TomorrowV2Diagnostics = diagnostics;
  window.addEventListener("error", (event) => {
    diagnostics.errors.push(String(event.message || "script_error").slice(0, 200));
  });
  window.addEventListener("unhandledrejection", () => {
    diagnostics.errors.push("unhandled_promise_rejection");
  });

  const reasons = {
    current_decision_unavailable: "当前尚无可用决策",
    formal_decision_unavailable: "该日期没有正式冻结记录",
    history_unavailable: "历史记录暂时不可读",
    candidate_timeout: "候选处理超时",
    close_fallback: "收盘补算",
    official_close: "正式收盘行情",
    local_only: "仅本地评分",
    checkpoint_unavailable: "冻结检查点不可用",
  };
  const actions = {
    executable: "可执行",
    observe: "观察",
    unavailable: "不可用",
  };
  const stages = { local: "本地", hybrid: "融合" };
  const sources = {
    tencent: "腾讯行情",
    eastmoney: "东方财富",
    sina: "新浪行情",
    tushare: "Tushare",
    official_close: "正式收盘",
    offline_fixture: "离线验收源",
  };
  const state = {
    view: null,
    etag: null,
    selectedCode: null,
    historical: false,
    stream: null,
    reconnectTimer: null,
    fallbackTimer: null,
    requestController: null,
    requestSequence: 0,
  };
  const byId = (id) => document.getElementById(id);

  function text(id, value) {
    byId(id).textContent = value == null || value === "" ? "-" : String(value);
  }

  function number(value, digits = 2) {
    return Number.isFinite(value) ? Number(value).toFixed(digits) : "-";
  }

  function pct(value) {
    if (!Number.isFinite(value)) return "-";
    return `${value > 0 ? "+" : ""}${Number(value).toFixed(2)}%`;
  }

  function clock(value) {
    if (!value) return "-";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime())
      ? "-"
      : parsed.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function reason(value) {
    if (!value) return "无";
    return reasons[value] || "运行条件暂不完整";
  }

  function setRuntime(status) {
    const dot = byId("runtimeDot");
    dot.dataset.state = status === "ready" ? "ready" : status === "degraded" ? "degraded" : "failed";
    text("runtimeState", status === "ready" ? "正常" : status === "degraded" ? "降级" : "未就绪");
  }

  async function loadDecision(options = {}) {
    state.requestSequence += 1;
    const requestSequence = state.requestSequence;
    if (state.requestController) state.requestController.abort();
    const controller = new AbortController();
    state.requestController = controller;
    const historical = Boolean(options.historical);
    const date = options.date;
    const url = historical
      ? `/api/v2/tomorrow/history?date=${encodeURIComponent(date)}`
      : "/api/v2/tomorrow/current";
    const headers = {};
    if (!historical && state.etag && !options.force) headers["If-None-Match"] = state.etag;
    try {
      const response = await fetch(url, { headers, cache: "no-store", signal: controller.signal });
      if (response.status === 304) return;
      const payload = await response.json();
      if (!response.ok) throw new Error(payload?.error?.code || "request_failed");
      if (requestSequence !== state.requestSequence) return;
      state.view = payload;
      state.historical = historical;
      state.etag = historical ? null : response.headers.get("ETag");
      renderDecision();
    } catch (error) {
      if (error?.name === "AbortError" || requestSequence !== state.requestSequence) return;
      setRuntime("not_ready");
      text("snapshotState", "决策读取失败");
      text("degradedState", "只读服务暂时不可用");
      renderRows([]);
    }
  }

  async function loadStatus() {
    try {
      const response = await fetch("/api/v2/status", { cache: "no-store" });
      if (!response.ok) throw new Error("status_failed");
      const payload = await response.json();
      setRuntime(payload.status);
      const budget = payload.deepseek_budget || {};
      text("budgetState", `${budget.used ?? 0} / ${budget.remaining ?? 0}`);
      const failures = payload.recent_failures || [];
      if (failures.length) text("degradedState", failures.map(reason).join("；"));
    } catch (_error) {
      setRuntime("not_ready");
    }
  }

  function renderDecision() {
    const view = state.view;
    if (!view || view.status !== "ready") {
      const degraded = view?.degraded_reasons || ["current_decision_unavailable"];
      text("snapshotState", state.historical ? "该日期无正式记录" : "当前决策未就绪");
      text("degradedState", degraded.map(reason).join("；"));
      text("tradeDate", view?.trade_date || "当前交易日");
      text("selectedCount", 0);
      text("evaluatedCount", 0);
      text("rejectedCount", 0);
      text("projectionStage", "-");
      text("highestScore", "-");
      text("quoteSource", "-");
      text("freezeState", "未冻结");
      text("dataAge", "-");
      renderRows([]);
      renderDetail(null);
      return;
    }
    const items = Array.isArray(view.items) ? view.items : [];
    const coverage = view.coverage || {};
    text("tradeDate", view.trade_date);
    text("snapshotState", view.frozen ? "正式冻结决策" : "实时决策");
    text("degradedState", (view.degraded_reasons || []).map(reason).join("；") || "无");
    text("selectedCount", coverage.selected_count ?? items.length);
    text("evaluatedCount", coverage.evaluated_count ?? 0);
    text("rejectedCount", coverage.rejected_count ?? 0);
    text("projectionStage", stages[view.projection_stage] || "本地");
    text("highestScore", items.length ? number(Math.max(...items.map((item) => item.final_score))) : "-");
    text(
      "quoteSource",
      [...new Set(items.map((item) => sources[item.quote_source] || "行情源").filter(Boolean))].join(" / ")
    );
    text("freezeState", view.frozen ? clock(view.frozen_at) : "未冻结");
    text("dataAge", Number.isFinite(view.data_age_seconds) ? `${number(view.data_age_seconds, 1)}秒` : "-");
    text("viewState", state.historical ? `历史 · ${view.trade_date}` : "当前决策");
    renderRows(items);
    const selected = items.find((item) => item.code === state.selectedCode) || items[0] || null;
    state.selectedCode = selected?.code || null;
    renderDetail(selected);
  }

  function renderRows(items) {
    const body = byId("decisionRows");
    if (!items.length) {
      body.innerHTML = '<tr><td class="table-message" colspan="8">没有可展示的决策</td></tr>';
      return;
    }
    body.innerHTML = items
      .map((item) => {
        const changeClass = item.current_pct_change > 0 ? "is-up" : item.current_pct_change < 0 ? "is-down" : "";
        const actionClass = item.action === "executable" ? "action-executable" : "action-observe";
        return `<tr data-code="${item.code}" class="${item.code === state.selectedCode ? "is-selected" : ""}">
          <td>${item.rank}</td>
          <td><b class="stock-name">${escapeText(item.name)}</b><span class="stock-code">${item.code}</span></td>
          <td title="${escapeText(item.industry)}">${escapeText(item.industry)}</td>
          <td>${number(item.current_price)}</td>
          <td class="${changeClass}">${pct(item.current_pct_change)}</td>
          <td>${number(item.final_score)}</td>
          <td class="${actionClass}">${actions[item.action] || "观察"}</td>
          <td>${clock(item.quote_source_time)}</td>
        </tr>`;
      })
      .join("");
  }

  function renderDetail(item) {
    text("detailTitle", item?.name || "尚未选择股票");
    text("detailCode", item?.code);
    const values = item
      ? [actions[item.action] || "观察", number(item.local_score), number(item.deepseek_score), number(item.deepseek_risk_penalty)]
      : ["-", "-", "-", "-"];
    [...byId("detailFacts").querySelectorAll("dd")].forEach((node, index) => {
      node.textContent = values[index];
    });
    text("detailReason", item ? reason(item.action_reason) : "-");
    const risks = item ? [...(item.local_risk_codes || []), ...(item.deepseek_risk_codes || [])] : [];
    text("detailRisk", risks.length ? risks.map(reason).join("；") : "未发现已登记风险");
    text(
      "detailIdentity",
      item && state.view
        ? `${stages[state.view.projection_stage] || "本地"} · ${sources[item.quote_source] || "行情源"} · ${clock(item.quote_source_time)}`
        : "-"
    );
  }

  function applyOverlay(payload) {
    if (
      !state.view ||
      state.historical ||
      payload.patch_schema_version !== 2 ||
      payload.projection_version !== state.view.projection_version ||
      payload.decision_version !== state.view.decision_version
    ) {
      loadDecision({ force: true });
      return;
    }
    const quotes = new Map((payload.quotes || []).map((quote) => [quote.code, quote]));
    state.view.items = state.view.items.map((item) => {
      const quote = quotes.get(item.code);
      return quote
        ? {
            ...item,
            current_price: quote.price,
            current_pct_change: quote.pct_change,
            quote_source: quote.source,
            quote_source_time: quote.source_time,
            quote_version: quote.quote_version,
            quote_age_seconds: quote.data_age_seconds,
          }
        : item;
    });
    state.view.quote_version = payload.quote_version;
    renderDecision();
  }

  function connectStream() {
    closeStream();
    if (state.historical) return;
    const stream = new EventSource("/api/v2/events");
    state.stream = stream;
    text("streamState", "连接中");
    stream.onopen = () => {
      text("streamState", "在线");
      clearInterval(state.fallbackTimer);
      state.fallbackTimer = null;
    };
    stream.addEventListener("decision", (event) => {
      const payload = parseEvent(event);
      if (
        !payload ||
        payload.patch_schema_version !== 2 ||
        payload.projection_version !== state.view?.projection_version ||
        payload.etag !== state.etag
      ) {
        loadDecision({ force: true });
      }
    });
    stream.addEventListener("quote_overlay", (event) => {
      const payload = parseEvent(event);
      if (payload) applyOverlay(payload);
      else loadDecision({ force: true });
    });
    stream.addEventListener("resync_required", () => loadDecision({ force: true }));
    stream.onerror = () => {
      text("streamState", "重连中");
      closeStream();
      if (!state.fallbackTimer) {
        state.fallbackTimer = setInterval(() => loadDecision({ force: true }), 30000);
      }
      clearTimeout(state.reconnectTimer);
      state.reconnectTimer = setTimeout(connectStream, 3000);
    };
  }

  function closeStream() {
    if (state.stream) state.stream.close();
    state.stream = null;
  }

  function escapeText(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function parseEvent(event) {
    try {
      return JSON.parse(event.data);
    } catch (_error) {
      return null;
    }
  }

  byId("decisionRows").addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-code]");
    if (!row || !state.view) return;
    state.selectedCode = row.dataset.code;
    renderDecision();
  });
  byId("refreshButton").addEventListener("click", () => {
    if (state.historical) {
      loadDecision({ historical: true, date: byId("historyDate").value, force: true });
    } else {
      loadDecision({ force: true });
    }
  });
  byId("currentButton").addEventListener("click", () => {
    state.historical = false;
    byId("currentButton").classList.add("is-active");
    byId("historyDate").value = "";
    loadDecision({ force: true });
    connectStream();
  });
  byId("historyDate").addEventListener("change", (event) => {
    if (!event.target.value) return;
    state.historical = true;
    byId("currentButton").classList.remove("is-active");
    closeStream();
    loadDecision({ historical: true, date: event.target.value, force: true });
  });

  loadDecision({ force: true });
  loadStatus();
  connectStream();
  setInterval(loadStatus, 10000);
})();
