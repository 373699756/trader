const state = {
  timer: null,
  eventSource: null,
  streamRetryTimer: null,
  validationAutoRefreshTimer: null,
  validationAutoRefreshInFlight: false,
  validationAutoRefreshDate: "",
  validationAutoRefreshAt: 0,
  countdown: window.APP_CONFIG.refreshSeconds,
  renderFingerprints: {},
  lastRows: {
    shortTerm: [],
    tomorrow: [],
    swing: [],
  },
  tomorrowLoaded: false,
  horizonLoaded: false,
  tomorrowLoading: null,
  horizonLoading: null,
  validationLoaded: false,
  marketRegime: {},
  selectedValidation: {
    date: "",
    strategy: "",
  },
  validationMetrics: {},
  deepseekAttributionByStrategy: {},
  validationCache: {},
  validationDailyCache: {},
  validationRequestSeq: 0,
  validationDailyRequestSeq: 0,
  validationQuotesRequestSeq: 0,
  validationDateRows: [],
  validationDatePage: 0,
  charts: {},
};

const VALIDATION_AUTO_REFRESH_MS = 30 * 60 * 1000;
const VALIDATION_DATE_PAGE_SIZE = 5;
const DEFAULT_MARKET = "all";
const DEFAULT_ACTION_FILTER = "all";
const DEFAULT_SORT_MODE = "rank";
const ValidationUI = window.TraderValidationUI;
const ValidationRenderers = window.TraderValidationRenderers;
const RecommendationUtils = window.TraderRecommendationUtils;
const RecommendationRenderers = window.TraderRecommendationRenderers;
const RecommendationTables = window.TraderRecommendationTables;

// 深色图表主题：ECharts 选项里的轴线/文字/分隔/正负色集中在此，配合深色背景。
const CHART_THEME = {
  axis: "#3a4452",
  split: "#222b37",
  text: "#8b98a8",
  strong: "#e6edf3",
  track: "#222b37",
  positive: "#f0666a",
  negative: "#3fb37f",
  accent: "#4f8cf7",
  muted: "#8b98a8",
  areaFill: ["#161b22", "#1c2330"],
};

// C1：ECharts 渲染封装。库加载失败时优雅降级为提示文字，不阻塞表格主功能。
function renderChart(elId, option) {
  const el = document.getElementById(elId);
  if (!el) return;
  if (window.__echartsFailed || typeof window.echarts === "undefined") {
    el.innerHTML = '<div class="chart-fallback">图表库未加载（离线环境）</div>';
    return;
  }
  let chart = state.charts[elId];
  if (!chart || chart.isDisposed?.()) {
    chart = window.echarts.init(el);
    state.charts[elId] = chart;
  }
  chart.setOption(option, true);
  chart.resize();
}

window.addEventListener("resize", () => {
  Object.values(state.charts).forEach((chart) => chart && !chart.isDisposed?.() && chart.resize());
});

const els = {
  statusText: document.getElementById("statusText"),
  quoteSource: document.getElementById("quoteSource"),
  sentimentSource: document.getElementById("sentimentSource"),
  candidateCount: document.getElementById("candidateCount"),
  factorCoverageStatus: document.getElementById("factorCoverageStatus"),
  hardFilterCount: document.getElementById("hardFilterCount"),
  marketSentiment: document.getElementById("marketSentiment"),
  riskBlacklistStatus: document.getElementById("riskBlacklistStatus"),
  refreshButton: document.getElementById("refreshButton"),
  tabButtons: document.querySelectorAll(".tab-button"),
  tabPanels: document.querySelectorAll(".tab-panel"),
  poolTabs: document.querySelectorAll(".pool-tab"),
  poolGroups: document.querySelectorAll(".rec-pool-group"),
  recommendationActionSummary: document.getElementById("recommendationActionSummary"),
  stockPredictionInput: document.getElementById("stockPredictionInput"),
  stockPredictionBtn: document.getElementById("stockPredictionBtn"),
  stockPredictionStatus: document.getElementById("stockPredictionStatus"),
  toolResultPane: document.getElementById("toolResultPane"),
  shortTermBody: document.getElementById("shortTermBody"),
  tomorrowBody: document.getElementById("tomorrowBody"),
  swingBody: document.getElementById("swingBody"),
  updateStatus: document.getElementById("updateStatus"),
  validationSimpleDecision: document.getElementById("validationSimpleDecision"),
  validationTitle: document.getElementById("validationTitle"),
  validationSubtitle: document.getElementById("validationSubtitle"),
  validationStrategySelect: document.getElementById("validationStrategySelect"),
  validationStrategyTabs: document.querySelectorAll(".validation-strategy-tab"),
  validationDaysSelect: document.getElementById("validationDaysSelect"),
  validationSelectionLabel: document.getElementById("validationSelectionLabel"),
  validationSampleCount: document.getElementById("validationSampleCount"),
  validationWinRateLabel: document.getElementById("validationWinRateLabel"),
  validationWinRate: document.getElementById("validationWinRate"),
  validationAvgReturnLabel: document.getElementById("validationAvgReturnLabel"),
  validationAvgReturn: document.getElementById("validationAvgReturn"),
  validationDeepseekAttribution: document.getElementById("validationDeepseekAttribution"),
  validationDeepseekMarketGate: document.getElementById("validationDeepseekMarketGate"),
  tuningStatus: document.getElementById("tuningStatus"),
  generateTuningBtn: document.getElementById("generateTuningBtn"),
  validationDatesBody: document.getElementById("validationDatesBody"),
  validationDatesPager: document.getElementById("validationDatesPager"),
  validationDatesPrev: document.getElementById("validationDatesPrev"),
  validationDatesNext: document.getElementById("validationDatesNext"),
  validationDatesPageLabel: document.getElementById("validationDatesPageLabel"),
  validationDetailTitle: document.getElementById("validationDetailTitle"),
  validationDetailBody: document.getElementById("validationDetailBody"),
};

function rememberFingerprint(key, value) {
  const next = JSON.stringify(value ?? null);
  if (state.renderFingerprints[key] === next) {
    return false;
  }
  state.renderFingerprints[key] = next;
  return true;
}

function hasRows(rows) {
  return Array.isArray(rows) && rows.length > 0;
}

function applyRecommendationsPayload(payload) {
  if (!payload.ok) {
    throw new Error(payload.error || "接口返回异常");
  }
  const recommendations = payload.recommendations || {};
  const shortTerm = recommendations.short_term || payload.data || [];
  const tomorrow = recommendations.tomorrow_picks || [];
  const marketRegime = payload.meta?.market_regime || {};
  const shouldRenderTables = rememberFingerprint("recommendations", {
    shortTerm,
    tomorrow,
    marketRegime,
  });
  state.lastRows.shortTerm = shortTerm;
  if (hasRows(tomorrow)) {
    state.lastRows.tomorrow = tomorrow;
  }
  state.marketRegime = marketRegime;
  renderMetrics(payload);
  if (shouldRenderTables) {
    rerenderCurrentTables();
  }
  if (state.tomorrowLoaded) {
    loadTomorrowPicks({ background: true });
  }
  if (state.horizonLoaded) {
    loadHorizonPicks({ background: true });
  }
  prefetchRecommendationPools();
  if (shouldRenderTables) {
    const generatedAt = payload.meta?.generated_at || "最近快照";
    setStatus(`后端推送更新 ${generatedAt}`);
  }
}

async function loadRecommendations() {
  setStatus("刷新中...");
  const params = new URLSearchParams({
    top_n: String(window.APP_CONFIG.defaultTopN || 18),
    market: DEFAULT_MARKET,
  });
  try {
    const res = await fetch(`/api/recommendations?${params.toString()}`);
    const payload = await res.json();
    applyRecommendationsPayload(payload);
  } catch (err) {
    const message = `<tr><td colspan="17" class="empty">${escapeHtml(err.message)}</td></tr>`;
    els.shortTermBody.innerHTML = message;
    setStatus(`刷新失败：${err.message}`);
  }
}

async function loadLatestRecommendationSnapshot() {
  const params = new URLSearchParams({
    top_n: String(window.APP_CONFIG.defaultTopN || 18),
    market: DEFAULT_MARKET,
    max_age: String(window.APP_CONFIG.recommendationSnapshotMaxAgeSeconds || 300),
  });
  try {
    const res = await fetch(`/api/recommendations/latest?${params.toString()}`);
    if (!res.ok) {
      return false;
    }
    const payload = await res.json();
    applyRecommendationsPayload(payload);
    const savedAt = payload.snapshot?.saved_at || payload.meta?.generated_at || "最近快照";
    setStatus(`已加载快照 ${savedAt}，等待实时更新...`);
    return true;
  } catch (err) {
    return false;
  }
}

function stopRecommendationStream() {
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
  if (state.streamRetryTimer) {
    clearTimeout(state.streamRetryTimer);
    state.streamRetryTimer = null;
  }
}

function connectRecommendationStream() {
  stopRecommendationStream();
  clearInterval(state.timer);
  state.countdown = window.APP_CONFIG.refreshSeconds;
  if (!window.EventSource) {
    setStatus("浏览器不支持后端推送，改用手动刷新");
    loadRecommendations();
    return;
  }
  const params = new URLSearchParams({
    top_n: String(window.APP_CONFIG.defaultTopN || 18),
    market: DEFAULT_MARKET,
  });
  const source = new EventSource(`/api/recommendations/stream?${params.toString()}`);
  state.eventSource = source;
  setStatus("已连接后端推送，等待数据...");
  startPushStatusCountdown();
  source.addEventListener("recommendations", (event) => {
    try {
      const payload = JSON.parse(event.data);
      applyRecommendationsPayload(payload);
      state.countdown = window.APP_CONFIG.refreshSeconds;
    } catch (err) {
      setStatus(`推送数据解析失败：${err.message}`);
    }
  });
  source.addEventListener("recommendations-error", (event) => {
    try {
      const payload = JSON.parse(event.data);
      setStatus(`后端推送失败：${payload.error || "接口返回异常"}`);
    } catch (err) {
      setStatus(`后端推送失败：${err.message}`);
    }
  });
  source.onerror = () => {
    setStatus("后端推送连接中断，正在重连...");
    source.close();
    if (state.eventSource === source) {
      state.eventSource = null;
      loadRecommendations();
      state.streamRetryTimer = setTimeout(connectRecommendationStream, 3000);
    }
  };
}

async function startRecommendationStreamWithSnapshot() {
  const loadedSnapshot = await loadLatestRecommendationSnapshot();
  if (!loadedSnapshot) {
    await loadRecommendations();
  }
  prefetchRecommendationPools();
  connectRecommendationStream();
}

async function loadValidation() {
  state.validationLoaded = true;
  const options = arguments[0] || {};
  const isSilent = Boolean(options.silent);
  const fromAutoRefresh = Boolean(options.fromAutoRefresh);
  const skipAutoOutcomeUpdate = Boolean(options.skipAutoOutcomeUpdate);
  const strategy = currentValidationStrategy();
  updateValidationChrome(strategy);
  const cacheKey = `${strategy}:${els.validationDaysSelect.value}`;
  const cached = state.validationCache[cacheKey];
  if (cached) {
    applyValidationPayload(cached);
  } else {
    els.validationDatesBody.innerHTML = '<tr><td colspan="4" class="empty">加载中...</td></tr>';
    els.validationDetailBody.innerHTML = '<tr><td colspan="11" class="empty">选择左侧批次查看明细</td></tr>';
  }
  const params = new URLSearchParams({
    strategy,
    days: els.validationDaysSelect.value,
    light: "1",
  });
  const requestSeq = ++state.validationRequestSeq;
  try {
    const res = await fetch(`/api/strategy-validation?${params.toString()}`);
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "接口返回异常");
    }
    if (requestSeq !== state.validationRequestSeq || strategy !== currentValidationStrategy()) {
      return;
    }
    state.validationCache[cacheKey] = payload;
    applyValidationPayload(payload);
    window.setTimeout(() => {
      loadValidationMetrics(strategy, els.validationDaysSelect.value, requestSeq, cacheKey, {
        skipAutoOutcomeUpdate,
      });
    }, 80);
    if (!els.updateStatus.textContent || els.updateStatus.textContent.includes("后台")) {
      loadValidationAutoUpdateStatus();
    }
    if (!isSilent) {
      setStatus("策略验证已更新");
    }
    if (!fromAutoRefresh) {
      startValidationAutoRefreshLoop();
    }
  } catch (err) {
    if (requestSeq !== state.validationRequestSeq) {
      return;
    }
    els.validationDatesBody.innerHTML = `<tr><td colspan="4" class="empty">${escapeHtml(err.message)}</td></tr>`;
    setStatus(`策略验证加载失败：${err.message}`);
  }
}

async function loadValidationMetrics(strategy, days, requestSeq, cacheKey, options = {}) {
  const params = new URLSearchParams({ strategy, days });
  try {
    const res = await fetch(`/api/strategy-validation?${params.toString()}`);
    const payload = await res.json();
    if (!payload.ok) {
      return;
    }
    if (requestSeq !== state.validationRequestSeq || strategy !== currentValidationStrategy()) {
      return;
    }
    state.validationCache[cacheKey] = payload;
    if (payload.metrics) {
      state.validationMetrics = payload.metrics;
      renderValidationMetrics(payload.metrics);
    }
    state.deepseekAttributionByStrategy = payload.deepseek_attribution_by_strategy || {};
    renderValidationDeepseekAttribution(state.deepseekAttributionByStrategy);
    renderValidationDeepseekMarketGate(payload.deepseek_market_gate || {});
    renderValidationDeepseekReview(payload.deepseek_review || {});
    if (!options.skipAutoOutcomeUpdate) {
      autoFillMissingValidationOutcomes(payload.metrics || {}, payload.dates || []);
    }
    loadTuningLatest(strategy);
  } catch (err) {
    /* 指标慢或失败不影响批次列表和明细查看 */
  }
}

function applyValidationPayload(payload) {
  if (payload.metrics) {
    state.validationMetrics = payload.metrics || {};
    renderValidationMetrics(state.validationMetrics);
  }
  if (payload.deepseek_attribution_by_strategy) {
    state.deepseekAttributionByStrategy = payload.deepseek_attribution_by_strategy || {};
    renderValidationDeepseekAttribution(state.deepseekAttributionByStrategy);
  }
  if (payload.deepseek_market_gate) {
    renderValidationDeepseekMarketGate(payload.deepseek_market_gate || {});
  }
  renderValidationDates(payload.dates || []);
  syncValidationSelection(payload.dates || []);
}

async function loadTuningLatest(strategy = currentValidationStrategy()) {
  if (!els.toolResultPane) return;
  const params = new URLSearchParams({ strategy });
  try {
    const res = await fetch(`/api/strategy-validation/tuning?${params.toString()}`);
    const payload = await res.json();
    if (!payload.ok || strategy !== currentValidationStrategy()) {
      return;
    }
  } catch (err) {
    /* 调参建议不影响验证主流程 */
  }
}

async function generateTuningPlan() {
  await loadStockOptimization();
}

function renderTuningRun(run, strategy = currentValidationStrategy()) {
  if (!els.toolResultPane) return;
  const plan = run?.plan || run || null;
  if (!plan || !Object.keys(plan).length) {
    renderToolResult('<div class="empty">暂无调参建议</div>');
    return;
  }
  const issues = (plan.issues || []).slice(0, 4);
  const suggestions = (plan.suggestions || []).slice(0, 6);
  const gate = plan.gate || {};
  const gateItems = (gate.items || []).slice(0, 4);
  const statusText = plan.can_apply
    ? "允许应用"
    : plan.shadow_mode
    ? "影子验证"
    : "仅记录";
  renderToolResult(`
    <div class="tuning-line">
      <strong>${escapeHtml(statusText)}</strong>
      <span>${escapeHtml(plan.reason || "-")}</span>
      <span>${escapeHtml(plan.generated_at || run?.run_time || "")}</span>
    </div>
    <div class="tuning-tags">
      ${issues.length ? issues.map(item => `<span class="tag warning">${escapeHtml(item)}</span>`).join("") : '<span class="tag muted">暂无主要问题</span>'}
    </div>
    <div class="tuning-tags">
      ${suggestions.length ? suggestions.map(item => `<span class="tag validation">${escapeHtml(item.parameter)}：${escapeHtml(formatTuningValue(item.value))}</span>`).join("") : '<span class="tag muted">暂无参数建议</span>'}
    </div>
    <div class="tuning-tags">
      ${gateItems.map(item => `<span class="tag ${item.passed ? "stable" : "risk"}">${escapeHtml(item.name)} ${item.passed ? "通过" : "阻断"}</span>`).join("")}
    </div>
  `);
}

function formatTuningValue(value) {
  if (value && typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value ?? "-");
}

function isValidationPanelActive() {
  const panel = document.getElementById("validationPanel");
  return Boolean(panel && panel.classList.contains("active"));
}

function currentValidationStrategy() {
  return els.validationStrategySelect?.value || "short_term";
}

function syncValidationStrategyTabs(strategy) {
  els.validationStrategyTabs.forEach(button => {
    const isActive = button.dataset.validationStrategy === strategy;
    button.classList.toggle("active", isActive);
    button.setAttribute("aria-selected", isActive ? "true" : "false");
  });
}

function updateValidationChrome(strategy) {
  const meta = ValidationUI.validationStrategyMeta(strategy, strategyLabel);
  syncValidationStrategyTabs(strategy);
  if (els.validationTitle) {
    els.validationTitle.textContent = `${meta.label}复盘`;
  }
  if (els.validationSubtitle) {
    els.validationSubtitle.textContent = `查看${meta.label}已保存样本、真实回填和回放表现，主看${meta.outcome}与执行状态。`;
  }
  if (els.validationWinRateLabel) {
    els.validationWinRateLabel.textContent = `${meta.horizon}净胜率`;
  }
  if (els.validationAvgReturnLabel) {
    els.validationAvgReturnLabel.textContent = `${meta.horizon}净收益`;
  }
  if (els.validationDetailTitle) {
    els.validationDetailTitle.textContent = `${meta.label}股票主周期明细`;
  }
}

function startValidationAutoRefreshLoop() {
  if (state.validationAutoRefreshTimer) {
    return;
  }
  state.validationAutoRefreshTimer = setInterval(() => {
    if (!isValidationPanelActive()) {
      return;
    }
    void loadValidation({ silent: true, fromAutoRefresh: true });
  }, VALIDATION_AUTO_REFRESH_MS);
}

function stopValidationAutoRefreshLoop() {
  if (!state.validationAutoRefreshTimer) {
    return;
  }
  clearInterval(state.validationAutoRefreshTimer);
  state.validationAutoRefreshTimer = null;
}

async function autoFillMissingValidationOutcomes(metrics, dates) {
  if (state.validationAutoRefreshInFlight) {
    return;
  }
  const outcomeSampleCount = Number(metrics?.outcome_sample_count || 0);
  const realSampleCount = Number(metrics?.real_sample_count || 0);
  if (outcomeSampleCount > 0 && realSampleCount > 0) {
    return;
  }
  const latestDate = Array.isArray(dates) && dates.length ? String(dates[0]?.signal_date || "").trim() : "";
  if (!latestDate) {
    return;
  }
  const strategy = currentValidationStrategy();
  const refreshKey = `${strategy}:${latestDate}`;
  const now = Date.now();
  if (state.validationAutoRefreshDate === refreshKey && now - state.validationAutoRefreshAt < VALIDATION_AUTO_REFRESH_MS) {
    return;
  }
  state.validationAutoRefreshInFlight = true;
  state.validationAutoRefreshDate = refreshKey;
  state.validationAutoRefreshAt = now;
  try {
    setOpsStatus(els.updateStatus, `检测到 ${strategyLabel(strategy)} ${latestDate} 无真实验证结果，正在回填收益`, "pending");
    const params = new URLSearchParams({ date: latestDate, strategy });
    const res = await fetch(`/api/strategy-validation/update?${params.toString()}`, {
      method: "POST",
    });
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "后台回填失败");
    }
    const result = payload.result || {};
    setOpsStatus(
      els.updateStatus,
      `已触发 ${strategyLabel(strategy)} ${latestDate} 回填，新增 ${result.updated || 0} 条，跳过 ${result.skipped || 0} 条，执行跳过 ${result.execution_skipped || 0} 条`,
      "ok",
    );
    if (isValidationPanelActive()) {
      await loadValidation({ silent: true, fromAutoRefresh: true, skipAutoOutcomeUpdate: true });
    }
  } catch (err) {
    setOpsStatus(els.updateStatus, `自动回填失败：${escapeHtml(err.message)}`, "bad");
  } finally {
    state.validationAutoRefreshInFlight = false;
  }
}

// 就地操作反馈：在操作块下方的状态行显示进度/成功/失败。
function setOpsStatus(el, text, level) {
  if (!el) return;
  el.textContent = text;
  el.className = "ops-status" + (level ? ` ops-${level}` : "");
}

function renderToolResult(html) {
  if (!els.toolResultPane) return;
  els.toolResultPane.innerHTML = html;
}

async function loadStockPrediction() {
  await loadStockPredictionWithMode("combined");
}

async function loadStockOptimization() {
  await loadStockPredictionWithMode("optimization");
}

async function loadStockPredictionWithMode(mode = "combined") {
  const raw = els.stockPredictionInput.value.trim();
  const code = raw.replace(/\D/g, "").slice(0, 6);
  if (code.length !== 6) {
    setOpsStatus(els.stockPredictionStatus, "请输入 6 位股票代码。", "bad");
    renderToolResult('<div class="empty">请输入 6 位股票代码</div>');
    return;
  }
  const label = els.stockPredictionBtn.textContent;
  const tuningLabel = els.generateTuningBtn?.textContent || "";
  els.stockPredictionBtn.disabled = true;
  els.stockPredictionBtn.textContent = mode === "optimization" ? "分析中…" : "预测中…";
  if (els.generateTuningBtn) {
    els.generateTuningBtn.disabled = true;
    if (mode === "optimization") {
      els.generateTuningBtn.textContent = "分析中…";
    }
  }
  setOpsStatus(
    els.stockPredictionStatus,
    mode === "optimization"
      ? "正在结合本地预测和 DeepSeek 生成策略优化建议…"
      : "正在读取当前行情并生成本地预测…",
    "pending"
  );
  setOpsStatus(
    els.tuningStatus,
    mode === "optimization" ? "正在生成个股优化建议…" : "",
    mode === "optimization" ? "pending" : ""
  );
  try {
    const params = mode === "optimization" ? "?deepseek=1" : "";
    const res = await fetch(`/api/stock-prediction/${encodeURIComponent(code)}${params}`);
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "无法给出预测");
    }
    renderStockPrediction(payload, mode);
    setOpsStatus(
      els.stockPredictionStatus,
      mode === "optimization"
        ? payload.optimization?.enabled === false
          ? "预测已更新，DeepSeek 未启用。"
          : "预测和优化建议已更新。"
        : "本地预测已更新。",
      "ok"
    );
    setOpsStatus(
      els.tuningStatus,
      payload.optimization?.enabled === false
        ? "DeepSeek 未启用，仅显示本地预测。"
        : mode === "optimization"
        ? "个股优化建议已更新。"
        : "",
      payload.optimization?.enabled === false ? "bad" : mode === "optimization" ? "ok" : ""
    );
  } catch (err) {
    renderToolResult(`
      <div class="prediction-empty">
        <strong>无法预测</strong>
        <p>${escapeHtml(err.message)}</p>
      </div>
    `);
    setOpsStatus(els.stockPredictionStatus, `预测失败：${err.message}`, "bad");
    if (mode === "optimization") {
      setOpsStatus(els.tuningStatus, `生成失败：${err.message}`, "bad");
    }
  } finally {
    els.stockPredictionBtn.disabled = false;
    els.stockPredictionBtn.textContent = label;
    if (els.generateTuningBtn) {
      els.generateTuningBtn.disabled = false;
      els.generateTuningBtn.textContent = tuningLabel;
    }
  }
}

function renderStockPrediction(payload, mode = "combined") {
  const p = payload.prediction || {};
  const cls = predictionClass(p.direction);
  const horizons = payload.horizons || {};
  const hits = payload.strategy_hits || [];
  const missed = payload.missed_strategies || [];
  const riskFlags = payload.risk_flags || [];
  const optimization = payload.optimization || null;
  const hitRows = hits.length
    ? hits.map(item => `
        <tr>
          <td>${escapeHtml(item.horizon_label || "-")}</td>
          <td>${escapeHtml(item.strategy_label)}</td>
          <td class="num">${item.rank ?? "-"}</td>
          <td class="num">${formatNumber(item.score, 1)}</td>
          <td class="num">${formatNumber(item.direction_score, 1)}</td>
          <td class="num">${formatNumber(item.risk_score, 1)}</td>
          <td>${escapeHtml(item.action || "-")}</td>
          <td class="reasons">${stockPredictionTags(item)}</td>
        </tr>
      `).join("")
    : '<tr><td colspan="8" class="empty">未命中任何策略</td></tr>';
  const missedTags = missed.length
    ? missed.map(item => `<span class="tag stable">${escapeHtml(item.horizon_label || "-")} / ${escapeHtml(item.strategy_label)}：${escapeHtml(item.reason)}</span>`).join("")
    : '<span class="tag strategy">全部适用策略已命中或参与评分</span>';
  renderToolResult(`
    <div class="prediction-head prediction-${cls} prediction-overall">
      <div>
        <span>${escapeHtml(payload.code)} ${escapeHtml(payload.name || "")}</span>
        <strong>综合：${escapeHtml(p.label || "-")}</strong>
        <p>${escapeHtml(p.advice || "")}</p>
      </div>
      <div class="prediction-score">
        <span>方向分</span>
        <strong>${formatNumber(p.score, 1)}</strong>
      </div>
      <div class="prediction-score">
        <span>置信度</span>
        <strong>${formatNumber(p.confidence, 1)}%</strong>
      </div>
      <div class="prediction-score">
        <span>风险</span>
        <strong>${escapeHtml(riskLevelLabel(p.risk_level))}</strong>
      </div>
    </div>
    <div class="prediction-horizons">
      ${renderPredictionHorizonCard(horizons.short, "short")}
    </div>
    <div class="prediction-facts">
      <div><span>最新价</span><strong>${formatNumber(payload.price, 3)}</strong></div>
      <div><span>今日涨跌</span><strong class="${numberClass(payload.pct_chg)}">${formatNumber(payload.pct_chg, 2)}%</strong></div>
      <div><span>成交额</span><strong>${formatMoney(payload.turnover)}</strong></div>
      <div><span>量比</span><strong>${formatNumber(payload.volume_ratio, 2)}</strong></div>
      <div><span>60日</span><strong class="${numberClass(payload.sixty_day_pct)}">${formatNumber(payload.sixty_day_pct, 2)}%</strong></div>
      <div><span>数据源</span><strong>${escapeHtml(payload.data_source || "实时行情")}</strong></div>
      <div><span>盘面</span><strong>${escapeHtml(payload.market_regime?.label || "-")}</strong></div>
    </div>
    ${riskFlags.length ? `
      <div class="prediction-section prediction-risk-flags">
        <h3>${payload.filtered ? "未入选原因 / 风险点" : "风险点"}</h3>
        <div class="prediction-tags">
          ${riskFlags.map(text => `<span class="tag risk">${escapeHtml(text)}</span>`).join("")}
        </div>
      </div>
    ` : ""}
    <div class="prediction-section">
      <h3>策略命中</h3>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>周期</th>
              <th>策略</th>
              <th>排名</th>
              <th>策略分</th>
              <th>方向分</th>
              <th>风险</th>
              <th>动作</th>
              <th>证据/风险</th>
            </tr>
          </thead>
          <tbody>${hitRows}</tbody>
        </table>
      </div>
    </div>
    <div class="prediction-section">
      <h3>未命中策略</h3>
      <div class="prediction-tags">${missedTags}</div>
    </div>
    ${renderPredictionOptimization(optimization, mode)}
    <p class="prediction-disclaimer">${escapeHtml(payload.disclaimer || "")}</p>
  `);
}

function renderPredictionOptimization(optimization, mode = "combined") {
  if (!optimization) {
    return "";
  }
  if (optimization.enabled === false) {
    return `
      <div class="prediction-section">
        <h3>DeepSeek</h3>
        <div class="prediction-tags">
          <span class="tag muted">当前未启用，暂只显示本地预测结果</span>
        </div>
      </div>
    `;
  }
  if (optimization.status && !["ok", "cache_hit"].includes(optimization.status)) {
    return `
      <div class="prediction-section">
        <h3>${mode === "optimization" ? "优化建议" : "DeepSeek 优化建议"}</h3>
        <div class="prediction-tags">
          <span class="tag warning">DeepSeek 暂时未返回有效建议</span>
          ${optimization.error ? `<span class="tag muted">${escapeHtml(optimization.error)}</span>` : ""}
        </div>
      </div>
    `;
  }
  const reasoning = (optimization.reasoning || []).map(text => `<span class="tag stable">${escapeHtml(text)}</span>`).join("");
  const entryPlan = (optimization.entry_plan || []).map(text => `<span class="tag validation">${escapeHtml(text)}</span>`).join("");
  const adjustments = (optimization.strategy_adjustments || []).map(text => `<span class="tag strategy">${escapeHtml(text)}</span>`).join("");
  const riskControls = (optimization.risk_controls || []).map(text => `<span class="tag warning">${escapeHtml(text)}</span>`).join("");
  const avoids = (optimization.avoid_conditions || []).map(text => `<span class="tag risk">${escapeHtml(text)}</span>`).join("");
  return `
    <div class="prediction-section">
      <h3>${mode === "optimization" ? "优化建议" : "DeepSeek 优化建议"}</h3>
      <div class="tuning-line">
        <strong>${escapeHtml(optimization.summary || "-")}</strong>
        <span>${escapeHtml(stockOptimizationStance(optimization.stance))}</span>
        <span>${escapeHtml(stockOptimizationTiming(optimization.timing))}</span>
      </div>
      ${reasoning ? `<div class="tuning-tags">${reasoning}</div>` : ""}
      ${entryPlan ? `<div class="tuning-tags">${entryPlan}</div>` : ""}
      ${adjustments ? `<div class="tuning-tags">${adjustments}</div>` : ""}
      ${riskControls ? `<div class="tuning-tags">${riskControls}</div>` : ""}
      ${avoids ? `<div class="tuning-tags">${avoids}</div>` : ""}
    </div>
  `;
}

function stockOptimizationStance(stance) {
  const map = {
    buy_trial: "小仓试单",
    watch_only: "只观察",
    hold_or_wait: "等确认",
    avoid_chase: "不追价",
  };
  return map[stance] || "策略待观察";
}

function stockOptimizationTiming(timing) {
  const map = {
    now: "可立即观察执行",
    pullback: "等回踩",
    breakout_confirm: "等突破确认",
    observe: "先观察",
  };
  return map[timing] || "时机待确认";
}

function renderPredictionHorizonCard(horizon, fallback) {
  const item = horizon || {};
  const p = item.prediction || {};
  const cls = predictionClass(p.direction);
  const hitCount = (item.strategy_hits || []).length;
  return `
    <article class="prediction-horizon-card prediction-${cls}">
      <span>${escapeHtml(item.label || (fallback === "long" ? "长期" : "短期"))}</span>
      <strong>${escapeHtml(p.label || "-")}</strong>
      <p>${escapeHtml(p.advice || "")}</p>
      <div class="prediction-horizon-metrics">
        <span>方向 ${formatNumber(p.score, 1)}</span>
        <span>置信 ${formatNumber(p.confidence, 1)}%</span>
        <span>命中 ${hitCount}</span>
        <span>风险 ${escapeHtml(riskLevelLabel(p.risk_level))}</span>
      </div>
    </article>
  `;
}

function stockPredictionTags(item) {
  const tags = [];
  (item.reasons || []).slice(0, 3).forEach(text => tags.push(`<span class="tag strategy">${escapeHtml(text)}</span>`));
  (item.failure_reasons || []).slice(0, 3).forEach(text => tags.push(`<span class="tag risk">${escapeHtml(text)}</span>`));
  const verdict = item.verdict?.label;
  if (verdict) {
    tags.push(`<span class="tag validation">${escapeHtml(verdict)}</span>`);
  }
  return tags.join("") || '<span class="tag stable">暂无额外证据</span>';
}

function predictionClass(direction) {
  if (direction === "up") return "up";
  if (direction === "down") return "down";
  return "neutral";
}

function riskLevelLabel(level) {
  if (level === "high") return "高";
  if (level === "medium") return "中";
  if (level === "low") return "低";
  return "未知";
}

async function loadValidationAutoUpdateStatus() {
  try {
    const res = await fetch("/api/strategy-validation/auto-update-status");
    const payload = await res.json();
    if (!payload.ok) {
      return;
    }
    const status = payload.auto_update || {};
    const snapshot = payload.auto_snapshot || {};
    const config = status.config || {};
    const snapshotText = snapshotStatusText(snapshot);
    if (!status.enabled) {
      setOpsStatus(els.updateStatus, joinStatusText(["14:30 后自动保存荐股快照已关闭", snapshotText]), "pending");
      return;
    }
    if (status.running) {
      setOpsStatus(els.updateStatus, joinStatusText(["正在保存今天/明天/2-5天荐股快照…", snapshotText]), "pending");
      return;
    }
    const result = status.last_result || {};
    if (status.last_error) {
      setOpsStatus(els.updateStatus, joinStatusText([`荐股快照自动保存上次失败：${status.last_error}`, snapshotText]), "bad");
      return;
    }
    if (status.last_finished_at) {
      const savedText = snapshotSaveText(result);
      setOpsStatus(
        els.updateStatus,
        joinStatusText([
          `荐股快照 ${status.last_finished_at} 已保存${savedText ? `：${savedText}` : ""}`,
          snapshotText,
        ]),
        "ok"
      );
      return;
    }
    setOpsStatus(
      els.updateStatus,
      joinStatusText([
        `自动保存已启动：${config.start_time || "14:30"} 之后每 ${Math.round((config.interval_seconds || 0) / 60)} 分钟保存今天/明天/2-5天荐股快照`,
        snapshotText,
      ]),
      "pending"
    );
  } catch (err) {
    /* 状态提示不影响验证主流程 */
  }
}

function joinStatusText(parts) {
  return parts.filter(Boolean).join("；");
}

function snapshotStatusText(snapshot) {
  if (!snapshot || snapshot.enabled === false) {
    return "荐股快照自动保存已关闭";
  }
  if (snapshot.running) {
    return "正在保存今天/明天/2-5天荐股快照";
  }
  if (snapshot.last_error) {
    return `荐股快照自动保存上次失败：${snapshot.last_error}`;
  }
  const snapshots = snapshot.last_result?.snapshots || [];
  const savedParts = snapshots
    .filter(item => item && item.ok && item.saved)
    .map(item => `${strategyLabel(item.strategy)} ${item.saved.saved || 0}条`);
  if (savedParts.length) {
    const signalDate = snapshots.find(item => item?.saved?.signal_date)?.saved?.signal_date || "";
    const tuningText = snapshot.last_tuning_date
      ? `；DeepSeek复盘 ${snapshot.last_tuning_date} 已生成`
      : "";
    return `已自动保存 ${signalDate} ${savedParts.join(" / ")}${tuningText}`;
  }
  if (snapshot.next_run_at) {
    return `下次自动保存 ${snapshot.next_run_at}`;
  }
  return "荐股快照自动保存已启动";
}

function snapshotSaveText(result) {
  const snapshots = result?.snapshots || [];
  return snapshots
    .filter(item => item && item.ok && item.saved)
    .map(item => `${strategyLabel(item.strategy)} ${item.saved.saved || 0}条`)
    .join(" / ");
}

async function loadValidationDaily(date, strategy) {
  state.selectedValidation = { date, strategy };
  renderValidationSelection();
  markSelectedValidationRow();
  const cacheKey = `${strategy}:${date}`;
  const cached = state.validationDailyCache[cacheKey];
  if (cached) {
    renderValidationDetail(cached.data || []);
    renderValidationBatchSummary(cached.data || [], date, strategy, cached.summary || null);
  } else {
    els.validationDetailBody.innerHTML = '<tr><td colspan="11" class="empty">加载中...</td></tr>';
  }
  const params = new URLSearchParams({ date, strategy });
  const requestSeq = ++state.validationDailyRequestSeq;
  try {
    const res = await fetch(`/api/strategy-validation/daily?${params.toString()}`);
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "接口返回异常");
    }
    if (
      requestSeq !== state.validationDailyRequestSeq ||
      state.selectedValidation.date !== date ||
      state.selectedValidation.strategy !== strategy
    ) {
      return;
    }
    state.validationDailyCache[cacheKey] = payload;
    renderValidationDetail(payload.data || []);
    renderValidationBatchSummary(payload.data || [], date, strategy, payload.summary || null);
    if ((payload.data || []).length) {
      refreshValidationDailyQuotes(date, strategy, cacheKey);
    }
  } catch (err) {
    if (requestSeq !== state.validationDailyRequestSeq) {
      return;
    }
    els.validationDetailBody.innerHTML = `<tr><td colspan="11" class="empty">${escapeHtml(err.message)}</td></tr>`;
    renderValidationBatchSummary([], date, strategy);
  }
}

async function refreshValidationDailyQuotes(date, strategy, cacheKey) {
  const requestSeq = ++state.validationQuotesRequestSeq;
  const params = new URLSearchParams({ date, strategy, quotes: "1" });
  try {
    const res = await fetch(`/api/strategy-validation/daily?${params.toString()}`);
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "接口返回异常");
    }
    if (
      requestSeq !== state.validationQuotesRequestSeq ||
      state.selectedValidation.date !== date ||
      state.selectedValidation.strategy !== strategy
    ) {
      return;
    }
    state.validationDailyCache[cacheKey] = payload;
    patchValidationQuoteColumns(payload.data || []);
  } catch (err) {
    /* 实时行情补列失败不影响批次明细查看 */
  }
}

async function loadTomorrowPicks(options = {}) {
  if (state.tomorrowLoading) {
    return state.tomorrowLoading;
  }
  state.tomorrowLoaded = true;
  const background = Boolean(options.background);
  const hasCachedRows = hasRows(state.lastRows.tomorrow);
  if (hasCachedRows) {
    renderTomorrowTable(state.lastRows.tomorrow);
    if (!background) {
      setStatus("明天推荐已显示，后台用 DeepSeek 刷新中...");
    }
  } else {
    els.tomorrowBody.innerHTML = '<tr><td colspan="17" class="empty">加载中...</td></tr>';
  }
  const params = new URLSearchParams({
    top_n: String(window.APP_CONFIG.defaultTopN || 18),
    market: DEFAULT_MARKET,
  });
  state.tomorrowLoading = (async () => {
    try {
      const res = await fetch(`/api/tomorrow-picks?${params.toString()}`);
      const payload = await res.json();
      if (!payload.ok) {
        throw new Error(payload.error || "接口返回异常");
      }
      const rows = payload.data || [];
      const shouldRender = rememberFingerprint("tomorrow", { rows, meta: payload.meta || {} });
      state.lastRows.tomorrow = rows;
      renderMetrics({ health: payload.health, meta: payload.meta, market_sentiment: {} });
      if (shouldRender) {
        renderTomorrowTable(state.lastRows.tomorrow);
      }
      if (!background) {
        setStatus(`明天推荐更新时间 ${payload.meta.generated_at || "最近快照"}`);
      }
    } catch (err) {
      state.tomorrowLoaded = false;
      if (!background || !hasRows(state.lastRows.tomorrow)) {
        els.tomorrowBody.innerHTML = `<tr><td colspan="17" class="empty">${escapeHtml(err.message)}</td></tr>`;
      }
      if (!background) {
        setStatus(`明天推荐加载失败：${err.message}`);
      }
    } finally {
      state.tomorrowLoading = null;
    }
  })();
  return state.tomorrowLoading;
}

async function loadHorizonPicks(options = {}) {
  if (state.horizonLoading) {
    return state.horizonLoading;
  }
  state.horizonLoaded = true;
  const background = Boolean(options.background);
  if (!background || !hasRows(state.lastRows.swing)) {
    els.swingBody.innerHTML = '<tr><td colspan="17" class="empty">加载中...</td></tr>';
  }
  const params = new URLSearchParams({
    top_n: String(window.APP_CONFIG.defaultTopN || 18),
    market: DEFAULT_MARKET,
  });
  state.horizonLoading = (async () => {
    try {
      const swingRes = await fetch(`/api/swing-picks?${params.toString()}`);
      const swingPayload = await swingRes.json();
      if (!swingPayload.ok) {
        throw new Error(swingPayload.error || "波段接口返回异常");
      }
      const swingRows = swingPayload.data || [];
      const shouldRenderSwing = rememberFingerprint("swing", swingRows);
      state.lastRows.swing = swingRows;
      renderMetrics({ health: swingPayload.health, meta: swingPayload.meta, market_sentiment: {} });
      if (shouldRenderSwing) {
        renderSwingTable(state.lastRows.swing);
      }
      if (!background) {
        setStatus(`2-5天更新时间 ${swingPayload.meta.generated_at}`);
      }
    } catch (err) {
      state.horizonLoaded = false;
      if (!background || !hasRows(state.lastRows.swing)) {
        els.swingBody.innerHTML = `<tr><td colspan="17" class="empty">${escapeHtml(err.message)}</td></tr>`;
      }
      if (!background) {
        setStatus(`2-5天加载失败：${err.message}`);
      }
    } finally {
      state.horizonLoading = null;
    }
  })();
  return state.horizonLoading;
}

function renderMetrics(payload) {
  const health = payload.health || {};
  const meta = payload.meta || {};
  const marketSentiment = payload.market_sentiment || {};
  els.quoteSource.textContent = health.quotes_source || "-";
  els.sentimentSource.textContent = health.sentiment_source || "-";
  els.candidateCount.textContent = meta.candidate_count ?? "-";
  renderFactorCoverageStatus(health.factor_coverage || meta.factor_coverage || payload.factor_coverage);
  renderHardFilterStatus(meta.hard_filter_report);
  els.marketSentiment.textContent = marketSentiment.score ? `${marketSentiment.score}` : "-";
  renderRiskBlacklistStatus(meta.risk_blacklist || payload.risk_blacklist);
}

function renderFactorCoverageStatus(coverage) {
  if (!els.factorCoverageStatus) return;
  let text = "-";
  let level = "neutral";
  let title = "暂无历史因子覆盖率信息";
  if (coverage) {
    const alerts = Array.isArray(coverage.alerts) ? coverage.alerts : [];
    const readyPct = Number(coverage.alphalite_ready_ratio || 0) * 100;
    const zeroPct = Number(coverage.alphalite_zero_coverage_ratio || 0) * 100;
    title = `ready ${formatNumber(readyPct, 1)}%，zero ${formatNumber(zeroPct, 1)}%`;
    if (alerts.length) {
      text = `告警${alerts.length}`;
      level = "error";
      title = alerts.map(item => item.message || item.code || "因子覆盖率异常").join("；");
    } else if (coverage.degraded) {
      text = "降级";
      level = "warn";
    } else if (coverage.history_factors_enabled === false) {
      text = "关闭";
      level = "warn";
      title = "历史因子未开启，2-5天和明日历史类因子不会参与打分。";
    } else {
      text = `${formatNumber(readyPct, 0)}%`;
      level = "ok";
    }
  }
  els.factorCoverageStatus.textContent = text;
  els.factorCoverageStatus.dataset.level = level;
  els.factorCoverageStatus.title = title;
}

function renderHardFilterStatus(report) {
  if (!els.hardFilterCount || !report) return;
  const rejected = Number(report.rejected_count || 0);
  els.hardFilterCount.textContent = `${rejected}`;
  els.hardFilterCount.dataset.level = rejected > 0 ? "warn" : "ok";
  const reasons = (report.reasons || [])
    .slice(0, 4)
    .map(item => `${item.label}:${item.count}`)
    .join("；");
  els.hardFilterCount.title = reasons || "无硬过滤剔除";
}

function renderRiskBlacklistStatus(risk) {
  if (!els.riskBlacklistStatus || !risk) return;
  let text = "-";
  let level = "neutral";
  if (!risk.enabled) {
    text = "关闭";
    level = "warn";
  } else if (risk.status === "ok") {
    text = `已加载${risk.item_count ?? 0}`;
    level = "ok";
  } else if (risk.status === "empty") {
    text = "空";
    level = "warn";
  } else if (risk.status === "partial") {
    text = `部分${risk.item_count ?? 0}`;
    level = "warn";
  } else if (risk.status === "error") {
    text = "异常";
    level = "error";
  } else {
    text = risk.status || "-";
  }
  els.riskBlacklistStatus.textContent = text;
  els.riskBlacklistStatus.dataset.level = level;
  els.riskBlacklistStatus.title = (risk.sources || []).join("，");
}

function currentPoolRows() {
  const filter = activePoolFilter();
  if (filter === "today") return state.lastRows.shortTerm || [];
  if (filter === "next") return state.lastRows.tomorrow || [];
  if (filter === "swing") return state.lastRows.swing || [];
  return state.lastRows.shortTerm || [];
}

function renderRecommendationActionSummary() {
  if (!els.recommendationActionSummary) return;
  const rows = RecommendationUtils.filterAndSortRows(currentPoolRows(), {
    actionFilter: DEFAULT_ACTION_FILTER,
    sortMode: DEFAULT_SORT_MODE,
  });
  if (!rows.length) {
    els.recommendationActionSummary.innerHTML = '<div class="empty">当前筛选下暂无动作汇总</div>';
    return;
  }
  els.recommendationActionSummary.innerHTML = RecommendationRenderers.renderRecommendationActionSummaryHtml(rows, {
    escapeHtml,
    formatNumber,
    rowScore: RecommendationUtils.rowScore.bind(RecommendationUtils),
  });
}

function renderShortTermTable(rows) {
  const displayRows = RecommendationUtils.filterAndSortRows(rows, {
    actionFilter: DEFAULT_ACTION_FILTER,
    sortMode: DEFAULT_SORT_MODE,
  });
  if (!displayRows.length) {
    els.shortTermBody.innerHTML = '<tr><td colspan="17" class="empty">暂无符合条件的股票</td></tr>';
    return;
  }
  els.shortTermBody.innerHTML = RecommendationTables.renderShortTermTableRows(displayRows, {
    escapeHtml,
    formatNumber,
    formatMoney,
    rowIndustryLabel: RecommendationRenderers.rowIndustryLabel.bind(RecommendationRenderers),
    explanationTags: (row) => RecommendationRenderers.explanationTags(row, { formatNumber, escapeHtml }),
    actionColumn: (row) => RecommendationRenderers.actionColumn(row, { formatNumber, escapeHtml }),
    scoreCell: (row) => RecommendationRenderers.scoreCell(row, {
      escapeHtml,
      formatNumber,
      rowScore: RecommendationUtils.rowScore,
    }),
  });
}

function renderTomorrowTable(rows) {
  const displayRows = RecommendationUtils.filterAndSortRows(rows, {
    actionFilter: DEFAULT_ACTION_FILTER,
    sortMode: DEFAULT_SORT_MODE,
  });
  if (!displayRows.length) {
    els.tomorrowBody.innerHTML = '<tr><td colspan="17" class="empty">暂无符合条件的股票</td></tr>';
    return;
  }
  els.tomorrowBody.innerHTML = RecommendationTables.renderTomorrowTableRows(displayRows, {
    escapeHtml,
    formatNumber,
    formatMoney,
    rowIndustryLabel: RecommendationRenderers.rowIndustryLabel.bind(RecommendationRenderers),
    explanationTags: (row) => RecommendationRenderers.explanationTags(row, { formatNumber, escapeHtml }),
    actionColumn: (row) => RecommendationRenderers.actionColumn(row, { formatNumber, escapeHtml }),
    scoreCell: (row) => RecommendationRenderers.scoreCell(row, {
      escapeHtml,
      formatNumber,
      rowScore: RecommendationUtils.rowScore,
    }),
  });
}

function renderSwingTable(rows) {
  const displayRows = RecommendationUtils.filterAndSortRows(rows, {
    actionFilter: DEFAULT_ACTION_FILTER,
    sortMode: DEFAULT_SORT_MODE,
  });
  if (!displayRows.length) {
    els.swingBody.innerHTML = '<tr><td colspan="17" class="empty">暂无符合条件的2-5天股票</td></tr>';
    return;
  }
  els.swingBody.innerHTML = RecommendationTables.renderSwingTableRows(displayRows, {
    escapeHtml,
    formatNumber,
    formatMoney,
    rowIndustryLabel: RecommendationRenderers.rowIndustryLabel.bind(RecommendationRenderers),
    explanationTags: (row) => RecommendationRenderers.explanationTags(row, { formatNumber, escapeHtml }),
    actionColumn: (row) => RecommendationRenderers.actionColumn(row, { formatNumber, escapeHtml }),
    scoreCell: (row) => RecommendationRenderers.scoreCell(row, {
      escapeHtml,
      formatNumber,
      rowScore: RecommendationUtils.rowScore,
    }),
  });
}

function renderValidationDeepseekAttribution(attributionByStrategy) {
  if (!els.validationDeepseekAttribution) return;
  const strategies = ["short_term", "tomorrow_picks", "swing_picks"];
  const cards = strategies.map(strategy => {
    const item = attributionByStrategy?.[strategy] || {};
    const counter = item.counterfactual_topn || {};
    const priorityDelta = item.priority_vs_watch || {};
    const avoid = item.avoid_veto || {};
    const sortDelta = Number(counter.avg_return_delta_pct);
    const winDelta = Number(counter.win_rate_delta_pct);
    const priorityWinDelta = Number(priorityDelta.win_rate_delta_pct);
    const notes = Array.isArray(item.notes) ? item.notes.slice(0, 1) : [];
    return `
      <div class="deepseek-attribution-card">
        <div class="deepseek-attribution-head">
          <strong>${escapeHtml(strategyLabel(strategy))}</strong>
          <span class="tag ${deepseekAttributionTagClass(item.status)}">${escapeHtml(deepseekAttributionStatusText(item.status))}</span>
        </div>
        <div class="deepseek-attribution-stats">
          <div><span>真实/全部</span><strong>${Number(item.real_sample_count || 0)}/${Number(item.sample_count || 0)}</strong></div>
          <div><span>覆盖</span><strong>${formatNumber(item.covered_ratio_pct, 1)}%</strong></div>
          <div><span>alpha</span><strong>${formatNumber(item.blend_alpha_avg, 2)}</strong></div>
        </div>
        <div class="deepseek-attribution-lines">
          <div><span>排序净收益增益</span><strong class="${numberClass(sortDelta)}">${formatSignedPct(sortDelta)}</strong></div>
          <div><span>排序净胜率增益</span><strong class="${numberClass(winDelta)}">${formatSignedPct(winDelta)}</strong></div>
          <div><span>priority-watch 净胜率差</span><strong class="${numberClass(priorityWinDelta)}">${formatSignedPct(priorityWinDelta)}</strong></div>
          <div><span>avoid/veto 平均净收益</span><strong class="${numberClass(avoid.avg_primary_return_net)}">${formatSignedPct(avoid.avg_primary_return_net)}</strong></div>
        </div>
        <div class="deepseek-attribution-foot">
          <span>local top${Number(counter.top_n || 0)} vs DeepSeek top${Number(counter.top_n || 0)}</span>
          <span>重排 ${Number(item.reordered_sample_count || 0)} 条</span>
        </div>
        ${notes.length ? `<div class="deepseek-attribution-warning">${escapeHtml(notes[0])}</div>` : ""}
      </div>
    `;
  }).join("");
  els.validationDeepseekAttribution.innerHTML = cards || '<div class="empty">暂无 DeepSeek 归因数据</div>';
}

function renderValidationDeepseekMarketGate(metrics) {
  if (!els.validationDeepseekMarketGate) return;
  const sampleCount = Number(metrics?.sample_count || 0);
  if (!sampleCount) {
    els.validationDeepseekMarketGate.innerHTML = '<div class="empty">暂无大盘 Gate 验证数据</div>';
    return;
  }
  const recent = Array.isArray(metrics.recent) ? (metrics.recent[0] || {}) : {};
  const avgReturn = Number(recent.avg_primary_return_net);
  const hit = recent.hit === true ? "命中" : recent.hit === false ? "偏离" : "待回填";
  const hitClass = recent.hit === true ? "stable" : recent.hit === false ? "warning" : "muted";
  els.validationDeepseekMarketGate.innerHTML = `
    <div class="deepseek-market-gate-card">
      <div class="deepseek-attribution-head">
        <strong>大盘 Gate</strong>
        <span class="tag ${hitClass}">${hit}</span>
      </div>
      <div class="deepseek-attribution-stats">
        <div><span>回填/判断</span><strong>${Number(metrics.outcome_sample_count || 0)}/${sampleCount}</strong></div>
        <div><span>命中率</span><strong>${formatNumber(metrics.hit_rate, 1)}%</strong></div>
        <div><span>最近 regime</span><strong>${escapeHtml(marketGateRegimeText(recent.regime))}</strong></div>
      </div>
      <div class="deepseek-attribution-lines">
        <div><span>缩量系数</span><strong>${formatNumber(recent.size_factor, 2)}</strong></div>
        <div><span>同日平均净收益</span><strong class="${numberClass(avgReturn)}">${formatSignedPct(avgReturn)}</strong></div>
        <div><span>实际状态</span><strong>${escapeHtml(marketGateRegimeText(recent.actual_regime))}</strong></div>
      </div>
    </div>
  `;
}

function marketGateRegimeText(regime) {
  if (regime === "risk_on") return "risk_on";
  if (regime === "risk_off") return "risk_off";
  if (regime === "balanced") return "balanced";
  if (regime === "unknown") return "待回填";
  return regime || "-";
}

function deepseekAttributionStatusText(status) {
  if (status === "ok") return "可评估";
  if (status === "insufficient_real_samples") return "样本不足";
  if (status === "no_deepseek_samples") return "无归因样本";
  if (status === "empty") return "暂无回填";
  if (status === "missing_strategy") return "缺少策略";
  return status || "未计算";
}

function deepseekAttributionTagClass(status) {
  if (status === "ok") return "stable";
  if (status === "insufficient_real_samples") return "warning";
  if (status === "no_deepseek_samples" || status === "empty") return "muted";
  return "validation";
}

function formatSignedPct(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "-";
  const sign = num > 0 ? "+" : "";
  return `${sign}${formatNumber(num, 2)}%`;
}

function renderValidationDeepseekReview(review) {
  state.latestDeepseekReview = review || {};
}

function renderValidationMetrics(metrics) {
  const sample = Number(metrics.sample_count || 0);
  const outcome = Number(metrics.outcome_sample_count || 0);
  const real = Number(metrics.real_sample_count || 0);
  const replay = Number(metrics.replay_sample_count || 0);
  const executionSkipped = Number(metrics.execution_skipped_count || 0);
  const pendingOutcome = Number(metrics.pending_outcome_count || 0);
  const coverage = metrics.outcome_coverage_pct == null ? null : Number(metrics.outcome_coverage_pct);
  const horizon = metrics.primary_horizon_label || "主周期";
  const winRate = metrics.win_rate_primary_net == null ? null : Number(metrics.win_rate_primary_net);
  const avgReturn = metrics.avg_primary_return_net == null ? null : Number(metrics.avg_primary_return_net);
  els.validationSampleCount.textContent = outcome > sample ? `${sample}/${outcome}` : `${sample}`;
  els.validationWinRate.textContent = winRate != null ? `${formatNumber(winRate, 1)}%` : "-";
  els.validationAvgReturn.textContent = avgReturn != null ? `${horizon} ${formatNumber(avgReturn, 2)}%` : "-";
  ValidationUI.renderValidationSimpleDecision(
    els.validationSimpleDecision,
    { sample, outcome, real, replay, winRate, avgReturn, horizon, executionSkipped, pendingOutcome },
    { formatNumber },
  );
}

function renderValidationBatchSummary(rows, date, strategy, summary = null) {
  const meta = ValidationUI.validationStrategyMeta(strategy, strategyLabel);
  const localSummary = summary || ValidationUI.validationBatchSummaryFromRows(rows, {
    primaryValidationNetReturn: ValidationRenderers.primaryValidationNetReturn.bind(ValidationRenderers),
    validationSkipReason: ValidationRenderers.validationSkipReason.bind(ValidationRenderers),
  });
  const sample = Number(localSummary.sample_count || 0);
  const up = Number(localSummary.up_count || 0);
  const down = Number(localSummary.down_count || 0);
  const flat = Number(localSummary.flat_count || 0);
  const pending = Number(localSummary.pending_count || 0);
  const winRate = localSummary.win_rate == null ? null : Number(localSummary.win_rate);
  const avgReturn = localSummary.avg_return == null ? null : Number(localSummary.avg_return);
  if (els.validationSelectionLabel) {
    els.validationSelectionLabel.textContent = date
      ? `${date} ${strategyLabel(strategy)}`
      : `未选择${meta.label}批次`;
  }
  if (els.validationSampleCount) {
    els.validationSampleCount.textContent = pending > 0 ? `${sample}（待回填${pending}）` : `${sample}`;
  }
  if (els.validationWinRate) {
    const flatText = flat > 0 ? ` / 平${flat}` : "";
    els.validationWinRate.textContent = winRate == null
      ? (pending > 0 ? `-（待回填${pending}）` : "-")
      : `${formatNumber(winRate, 1)}%（涨${up} / 跌${down}${flatText}）`;
  }
  if (els.validationAvgReturn) {
    els.validationAvgReturn.textContent = avgReturn == null ? "-" : `${formatNumber(avgReturn, 2)}%`;
    els.validationAvgReturn.className = avgReturn == null ? "" : numberClass(avgReturn);
  }
}

function renderValidationDates(rows) {
  state.validationDateRows = rows || [];
  if (!rows.length) {
    els.validationDatesBody.innerHTML = '<tr><td colspan="4" class="empty">暂无保存记录</td></tr>';
    els.validationDetailBody.innerHTML = '<tr><td colspan="11" class="empty">暂无可查看明细</td></tr>';
    state.selectedValidation = { date: "", strategy: currentValidationStrategy() };
    renderValidationBatchSummary([], "", currentValidationStrategy());
    updateValidationDatesPager();
    return;
  }
  clampValidationDatePage();
  renderValidationDatePage();
}

function validationDatePageCount() {
  return ValidationRenderers.validationDatePageCount(state.validationDateRows.length, VALIDATION_DATE_PAGE_SIZE);
}

function clampValidationDatePage() {
  state.validationDatePage = ValidationRenderers.clampValidationDatePage(
    state.validationDatePage,
    state.validationDateRows.length,
    VALIDATION_DATE_PAGE_SIZE,
  );
}

function renderValidationDatePage() {
  clampValidationDatePage();
  const start = state.validationDatePage * VALIDATION_DATE_PAGE_SIZE;
  const pageRows = state.validationDateRows.slice(start, start + VALIDATION_DATE_PAGE_SIZE);
  els.validationDatesBody.innerHTML = ValidationRenderers.renderValidationDatePageRows(pageRows, { escapeHtml });
  [...els.validationDatesBody.querySelectorAll("tr")].forEach(row => {
    row.addEventListener("click", () => loadValidationDaily(row.dataset.date, row.dataset.strategy));
  });
  markSelectedValidationRow();
  updateValidationDatesPager();
}

function updateValidationDatesPager() {
  if (!els.validationDatesPager) return;
  const totalRows = state.validationDateRows.length;
  const totalPages = validationDatePageCount();
  const hasMultiplePages = totalRows > VALIDATION_DATE_PAGE_SIZE;
  els.validationDatesPager.hidden = !totalRows;
  if (els.validationDatesPageLabel) {
    els.validationDatesPageLabel.textContent = totalRows
      ? `${state.validationDatePage + 1}/${totalPages} 共${totalRows}条`
      : "0/0";
  }
  if (els.validationDatesPrev) {
    els.validationDatesPrev.disabled = !hasMultiplePages || state.validationDatePage <= 0;
  }
  if (els.validationDatesNext) {
    els.validationDatesNext.disabled = !hasMultiplePages || state.validationDatePage >= totalPages - 1;
  }
}

function moveValidationDatePage(delta) {
  state.validationDatePage += delta;
  clampValidationDatePage();
  renderValidationDatePage();
}

function syncValidationDatePageToSelection() {
  const index = state.validationDateRows.findIndex(row =>
    row.signal_date === state.selectedValidation.date &&
    row.strategy_name === state.selectedValidation.strategy
  );
  if (index < 0) return;
  const nextPage = Math.floor(index / VALIDATION_DATE_PAGE_SIZE);
  if (nextPage !== state.validationDatePage) {
    state.validationDatePage = nextPage;
    renderValidationDatePage();
  } else {
    markSelectedValidationRow();
    updateValidationDatesPager();
  }
}

function renderValidationDetail(rows) {
  if (!rows.length) {
    els.validationDetailBody.innerHTML = '<tr><td colspan="11" class="empty">暂无明细</td></tr>';
    return;
  }
  els.validationDetailBody.innerHTML = ValidationRenderers.renderValidationDetailRows(rows, {
    escapeHtml,
    formatNumber,
    numberClass,
  });
}

function patchValidationQuoteColumns(rows) {
  const lookup = new Map((rows || []).map(row => [String(row.code || ""), row]));
  [...els.validationDetailBody.querySelectorAll("tr[data-code]")].forEach(tr => {
    const row = lookup.get(String(tr.dataset.code || ""));
    if (!row) return;
    ValidationRenderers.updateValidationPctCell(
      tr.querySelector('[data-validation-field="current_pct_chg"]'),
      row.current_pct_chg,
      { numberClass, formatNumber },
    );
    ValidationRenderers.updateValidationPctCell(
      tr.querySelector('[data-validation-field="anchor_to_now_return"]'),
      row.anchor_to_now_return,
      { numberClass, formatNumber },
    );
  });
}

function syncValidationSelection(rows) {
  const exists = rows.some(row =>
    row.signal_date === state.selectedValidation.date &&
    row.strategy_name === state.selectedValidation.strategy
  );
  if (!exists) {
    const first = rows.find(row => Number(row.real_count || 0) > 0) || rows[0];
    state.selectedValidation = first
      ? { date: first.signal_date, strategy: first.strategy_name }
      : { date: "", strategy: "" };
  }
  renderValidationSelection();
  syncValidationDatePageToSelection();
  if (state.selectedValidation.date && state.selectedValidation.strategy) {
    loadValidationDaily(state.selectedValidation.date, state.selectedValidation.strategy);
  }
}

function renderValidationSelection() {
  if (!state.selectedValidation.date) {
    els.validationSelectionLabel.textContent = `未选择${strategyLabel(currentValidationStrategy())}批次`;
    return;
  }
  els.validationSelectionLabel.textContent = `${state.selectedValidation.date} ${strategyLabel(state.selectedValidation.strategy)}`;
}

function markSelectedValidationRow() {
  [...els.validationDatesBody.querySelectorAll("tr")].forEach(row => {
    row.classList.toggle(
      "selected",
      row.dataset.date === state.selectedValidation.date &&
      row.dataset.strategy === state.selectedValidation.strategy
    );
  });
}

function rerenderCurrentTables() {
  renderShortTermTable(state.lastRows.shortTerm);
  if (state.tomorrowLoaded) {
    renderTomorrowTable(state.lastRows.tomorrow);
  }
  if (state.horizonLoaded) {
    renderSwingTable(state.lastRows.swing);
  }
  renderRecommendationActionSummary();
}

function startPushStatusCountdown() {
  clearInterval(state.timer);
  state.timer = null;
}

function setStatus(text) {
  els.statusText.textContent = text;
}

function formatNumber(value, digits) {
  const num = Number(value);
  if (!Number.isFinite(num)) {
    return "-";
  }
  return num.toFixed(digits);
}

function formatMoney(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) {
    return "-";
  }
  if (num >= 100000000) {
    return `${(num / 100000000).toFixed(2)}亿`;
  }
  if (num >= 10000) {
    return `${(num / 10000).toFixed(1)}万`;
  }
  return num.toFixed(0);
}

function numberClass(value) {
  const num = Number(value);
  if (!Number.isFinite(num) || num === 0) {
    return "";
  }
  return num >= 0 ? "positive" : "negative";
}

function strategyLabel(value) {
  if (value === "short_term") return "今天推荐";
  if (value === "tomorrow_picks") return "明天推荐";
  if (value === "swing_picks") return "2-5天推荐";
  return value || "-";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function activePoolFilter() {
  return document.querySelector(".pool-tab.active")?.dataset.poolFilter || "today";
}

function applyRecommendationPoolFilter(filter = activePoolFilter()) {
  els.poolTabs.forEach(tab => {
    tab.classList.toggle("active", tab.dataset.poolFilter === filter);
  });
  els.poolGroups.forEach(group => {
    group.hidden = group.dataset.poolGroup !== filter;
  });
  renderRecommendationActionSummary();
}

function ensureRecommendationPoolData(options = {}) {
  const background = Boolean(options.background);
  const filter = activePoolFilter();
  if (filter === "next" && !state.tomorrowLoaded) {
    loadTomorrowPicks({ background });
  }
  if (filter === "swing" && !state.horizonLoaded) {
    loadHorizonPicks({ background });
  }
}

function prefetchRecommendationPools() {
  const tasks = [];
  if (!state.tomorrowLoaded || !hasRows(state.lastRows.tomorrow)) {
    tasks.push(loadTomorrowPicks({ background: true }));
  }
  if (!state.horizonLoaded || !hasRows(state.lastRows.swing)) {
    tasks.push(loadHorizonPicks({ background: true }));
  }
  return Promise.allSettled(tasks);
}

els.refreshButton.addEventListener("click", startRecommendationStreamWithSnapshot);
els.stockPredictionBtn.addEventListener("click", loadStockPrediction);
els.stockPredictionInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    loadStockPrediction();
  }
});
els.poolTabs.forEach(button => {
  button.addEventListener("click", () => {
    applyRecommendationPoolFilter(button.dataset.poolFilter || "today");
    ensureRecommendationPoolData();
  });
});
els.tabButtons.forEach(button => {
  button.addEventListener("click", () => {
    els.tabButtons.forEach(item => item.classList.toggle("active", item === button));
    els.tabPanels.forEach(panel => panel.classList.toggle("active", panel.id === button.dataset.tab));
    // 切到可见 tab 后 resize 其内图表：ECharts 在 display:none 容器里 init 会得到 0x0，
    // 切回可见时需主动 resize 才能正确渲染。
    requestAnimationFrame(() => {
      Object.values(state.charts).forEach((chart) => {
        if (chart && !chart.isDisposed?.() && chart.getDom?.()?.offsetParent !== null) {
          chart.resize();
        }
      });
    });
    if (button.dataset.tab === "todayPanel") {
      startRecommendationStreamWithSnapshot();
      applyRecommendationPoolFilter();
      ensureRecommendationPoolData();
      stopValidationAutoRefreshLoop();
    } else {
      stopRecommendationStream();
      if (!state.validationLoaded) {
        loadValidation();
      }
      startValidationAutoRefreshLoop();
    }
  });
});
els.validationStrategySelect?.addEventListener("change", () => {
  state.selectedValidation = { date: "", strategy: "" };
  state.validationAutoRefreshDate = "";
  state.validationDatePage = 0;
  renderToolResult('<div class="empty">点击左侧按钮后在这里显示结果</div>');
  loadValidation();
});
els.validationStrategyTabs.forEach(button => {
  button.addEventListener("click", () => {
    const strategy = button.dataset.validationStrategy || "short_term";
    if (els.validationStrategySelect) {
      els.validationStrategySelect.value = strategy;
    }
    state.selectedValidation = { date: "", strategy: "" };
    state.validationAutoRefreshDate = "";
    state.validationDatePage = 0;
    renderToolResult('<div class="empty">点击左侧按钮后在这里显示结果</div>');
    loadValidation();
  });
});
els.generateTuningBtn?.addEventListener("click", generateTuningPlan);
els.validationDatesPrev?.addEventListener("click", () => moveValidationDatePage(-1));
els.validationDatesNext?.addEventListener("click", () => moveValidationDatePage(1));
els.validationDaysSelect.addEventListener("change", () => {
  state.validationDatePage = 0;
  loadValidation();
});

applyRecommendationPoolFilter();
startRecommendationStreamWithSnapshot();
