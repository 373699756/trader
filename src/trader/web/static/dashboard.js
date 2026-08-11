(() => {
  "use strict";

  const state = { strategy: "today", date: "", etags: new Map(), payloads: new Map(), errors: [] };
  const byId = (id) => document.getElementById(id);
  const text = (id, value) => { byId(id).textContent = value ?? "-"; };
  const fmt = (value, digits = 2) => Number.isFinite(value) ? Number(value).toFixed(digits) : "-";
  const age = (seconds) => Number.isFinite(seconds) ? `${Math.round(seconds)} 秒` : "-";
  const diagnostics = { get strategy() { return state.strategy; }, get errors() { return [...state.errors]; } };
  window.TraderV2Diagnostics = diagnostics;

  async function fetchJson(path) {
    const headers = {};
    const etag = state.etags.get(path);
    if (etag) headers["If-None-Match"] = etag;
    const response = await fetch(path, { headers, cache: "no-store" });
    if (response.status === 304) return state.payloads.get(path);
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload?.error?.code || `http_${response.status}`);
    }
    const payload = await response.json();
    const nextEtag = response.headers.get("ETag");
    if (nextEtag) state.etags.set(path, nextEtag);
    state.payloads.set(path, payload);
    return payload;
  }

  function decisionPath() {
    const base = `/api/v2/decisions/${state.strategy}`;
    return state.date ? `${base}/history?date=${encodeURIComponent(state.date)}` : `${base}/current`;
  }

  async function loadDecision() {
    try {
      renderDecision(await fetchJson(decisionPath()));
    } catch (error) {
      recordError(error);
      renderEmpty("决策读取失败");
    }
  }

  async function loadDates() {
    const select = byId("dateSelect");
    select.replaceChildren(new Option("当前", ""));
    if (state.strategy === "long") {
      select.disabled = true;
      return;
    }
    select.disabled = false;
    try {
      const payload = await fetchJson(`/api/v2/decisions/${state.strategy}/dates`);
      for (const value of payload.dates || []) select.add(new Option(value, value));
      select.value = state.date;
    } catch (error) { recordError(error); }
  }

  async function loadStatus() {
    try {
      const payload = await fetchJson("/api/v2/status");
      const ready = payload.status === "running" || payload.status === "ready";
      text("runtimeStatus", payload.status || "unknown");
      byId("runtimeDot").className = `status-dot ${ready ? "ready" : "degraded"}`;
      const budget = payload.deepseek_budget || {};
      text("budget", `${budget.used ?? "-"} / ${budget.remaining ?? "-"} / ${budget.limit ?? "-"}`);
      text("lastError", `最近错误：${payload.last_error || "无"}`);
    } catch (error) {
      recordError(error);
      text("runtimeStatus", "degraded");
      byId("runtimeDot").className = "status-dot degraded";
    }
  }

  function renderDecision(payload) {
    const coverage = payload.coverage || {};
    const reasons = payload.degraded_reasons || [];
    text("tradeDate", payload.trade_date);
    text("dataAge", age(payload.data_age_seconds));
    text("observedAt", payload.observed_at ? new Date(payload.observed_at).toLocaleString("zh-CN", { hour12: false }) : "尚无数据");
    text("coverage", `${coverage.evaluated_count ?? 0} / ${coverage.candidate_count ?? 0}`);
    text("coverageDetail", `已评估 / 候选，过滤 ${coverage.rejected_count ?? 0}`);
    text("funnel", `${coverage.rejected_count ?? 0} → ${coverage.selected_count ?? 0}`);
    text("funnelDetail", `执行 ${coverage.executable_count ?? 0} / 观察 ${coverage.observation_count ?? 0}`);
    text("freeze", payload.frozen ? "已冻结" : "滚动");
    text("freezeDetail", payload.freeze_kind || "当前结果不写历史");
    text("degraded", reasons.length ? reasons.join("、") : "无降级");
    byId("degraded").classList.toggle("has-warning", reasons.length > 0);
    text("panelTitle", `${label(state.strategy)} ${state.date || "当前决策"}`);
    text("viewLabel", state.date ? `历史 · ${state.date}` : "当前决策");
    text("decisionStatus", payload.status);
    text("scoreStatus", payload.score_status);
    text("decisionStage", payload.stage || "未就绪");
    text("decisionVersion", payload.decision_version);
    text("inputVersions", entries(payload.input_versions));
    text("filterReasons", entries(payload.filter_reason_counts));
    renderRows(payload.items || [], payload.status);
  }

  function renderRows(items, status) {
    const body = byId("decisionRows");
    body.replaceChildren();
    text("itemCount", `${items.length} 项`);
    if (!items.length) {
      renderEmpty(status === "not_applicable" ? "该策略不提供历史" : "当前没有可展示决策");
      return;
    }
    for (const item of items) {
      const row = document.createElement("tr");
      row.dataset.code = item.code;
      const quote = item.quote || {};
      const scores = item.scores || {};
      append(row, item.rank || "-");
      const stock = append(row, item.code);
      const name = document.createElement("span"); name.className = "stock-name"; name.textContent = item.name || "-"; stock.append(name);
      append(row, item.group || item.industry || "-");
      const quoteCell = append(row, fmt(quote.price));
      const change = document.createElement("span");
      change.className = Number(quote.pct_change) >= 0 ? "positive" : "negative";
      change.textContent = ` ${fmt(quote.pct_change)}%`;
      quoteCell.append(change);
      append(row, fmt(scores.local));
      append(row, fmt(scores.deepseek));
      append(row, fmt(scores.final));
      const action = append(row, "");
      const badge = document.createElement("span"); badge.className = "action"; badge.textContent = item.action || "-"; action.append(badge);
      append(row, [...(item.risk_codes || []), item.action_reason].filter(Boolean).join(" · ") || "-");
      body.append(row);
    }
  }

  function append(row, value) { const cell = document.createElement("td"); cell.textContent = value; row.append(cell); return cell; }
  function renderEmpty(message) { const body = byId("decisionRows"); body.replaceChildren(); const row = document.createElement("tr"); const cell = append(row, message); cell.colSpan = 9; cell.className = "empty-state"; body.append(row); text("itemCount", "0 项"); }
  function entries(value) { const pairs = Object.entries(value || {}); return pairs.length ? pairs.map(([key, item]) => `${key}: ${item}`).join(" · ") : "-"; }
  function label(strategy) { return ({ today: "Today", tomorrow: "Tomorrow", d25: "D2-5", long: "Long" })[strategy]; }
  function recordError(error) { state.errors.push(String(error?.message || error)); state.errors = state.errors.slice(-20); }

  function connectEvents() {
    const stream = new EventSource("/api/v2/events");
    stream.onopen = () => text("streamStatus", "在线");
    stream.onerror = () => text("streamStatus", "重连中");
    const refresh = (event) => {
      try {
        const payload = JSON.parse(event.data);
        if (event.type === "resync_required" || payload.strategy === state.strategy) loadDecision();
        loadStatus();
      } catch (error) { recordError(error); }
    };
    stream.addEventListener("decision", refresh);
    stream.addEventListener("overlay", refresh);
    stream.addEventListener("resync_required", refresh);
  }

  document.addEventListener("DOMContentLoaded", async () => {
    byId("strategyTabs").addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-strategy]");
      if (!button) return;
      state.strategy = button.dataset.strategy;
      state.date = "";
      document.querySelectorAll("button[data-strategy]").forEach((item) => item.classList.toggle("is-active", item === button));
      await loadDates();
      await loadDecision();
    });
    byId("dateSelect").addEventListener("change", async (event) => { state.date = event.target.value; await loadDecision(); });
    byId("refreshButton").addEventListener("click", async () => { state.etags.clear(); await Promise.all([loadDecision(), loadStatus()]); });
    await Promise.all([loadDates(), loadDecision(), loadStatus()]);
    connectEvents();
    window.setInterval(loadStatus, 30000);
  });
})();
