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
    consensus: [],
    tomorrow: [],
    swing: [],
  },
  tomorrowLoaded: false,
  horizonLoaded: false,
  overviewLoaded: false,
  validationLoaded: false,
  marketRegime: {},
  selectedValidation: {
    date: "",
    strategy: "",
  },
  validationMetrics: {},
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
  hardFilterCount: document.getElementById("hardFilterCount"),
  marketSentiment: document.getElementById("marketSentiment"),
  riskBlacklistStatus: document.getElementById("riskBlacklistStatus"),
  marketSelect: document.getElementById("marketSelect"),
  actionFilterSelect: document.getElementById("actionFilterSelect"),
  sortSelect: document.getElementById("sortSelect"),
  glossaryToggle: document.getElementById("glossaryToggle"),
  glossaryPopover: document.getElementById("glossaryPopover"),
  refreshButton: document.getElementById("refreshButton"),
  tabButtons: document.querySelectorAll(".tab-button"),
  tabPanels: document.querySelectorAll(".tab-panel"),
  poolTabs: document.querySelectorAll(".pool-tab"),
  poolGroups: document.querySelectorAll(".rec-pool-group"),
  decisionRegimeLabel: document.getElementById("decisionRegimeLabel"),
  decisionAdvice: document.getElementById("decisionAdvice"),
  decisionConsensusCount: document.getElementById("decisionConsensusCount"),
  decisionPriorityCount: document.getElementById("decisionPriorityCount"),
  decisionAvgRisk: document.getElementById("decisionAvgRisk"),
  stockPredictionInput: document.getElementById("stockPredictionInput"),
  stockPredictionBtn: document.getElementById("stockPredictionBtn"),
  stockPredictionStatus: document.getElementById("stockPredictionStatus"),
  stockPredictionResult: document.getElementById("stockPredictionResult"),
  overviewBestStrategy: document.getElementById("overviewBestStrategy"),
  overviewVerifiedCount: document.getElementById("overviewVerifiedCount"),
  overviewSampleCount: document.getElementById("overviewSampleCount"),
  overviewDays: document.getElementById("overviewDays"),
  overviewRegimeLabel: document.getElementById("overviewRegimeLabel"),
  overviewRegimeScore: document.getElementById("overviewRegimeScore"),
  overviewRegimeBreadth: document.getElementById("overviewRegimeBreadth"),
  overviewRegimeMedian: document.getElementById("overviewRegimeMedian"),
  overviewRegimeStrong: document.getElementById("overviewRegimeStrong"),
  overviewRegimeAdvice: document.getElementById("overviewRegimeAdvice"),
  strategyConsensusBody: document.getElementById("strategyConsensusBody"),
  strategyOverviewGrid: document.getElementById("strategyOverviewGrid"),
  strategyOverviewBody: document.getElementById("strategyOverviewBody"),
  shortTermBody: document.getElementById("shortTermBody"),
  tomorrowBody: document.getElementById("tomorrowBody"),
  swingBody: document.getElementById("swingBody"),
  updateStatus: document.getElementById("updateStatus"),
  validationScoreboard: document.getElementById("validationScoreboard"),
  validationSimpleDecision: document.getElementById("validationSimpleDecision"),
  validationDeepseekReview: document.getElementById("validationDeepseekReview"),
  validationTitle: document.getElementById("validationTitle"),
  validationSubtitle: document.getElementById("validationSubtitle"),
  validationGuidePrimary: document.getElementById("validationGuidePrimary"),
  validationStrategySelect: document.getElementById("validationStrategySelect"),
  validationStrategyTabs: document.querySelectorAll(".validation-strategy-tab"),
  validationDaysSelect: document.getElementById("validationDaysSelect"),
  validationOpsHint: document.getElementById("validationOpsHint"),
  validationSelectionLabel: document.getElementById("validationSelectionLabel"),
  validationSampleCount: document.getElementById("validationSampleCount"),
  validationWinRateLabel: document.getElementById("validationWinRateLabel"),
  validationWinRate: document.getElementById("validationWinRate"),
  validationHit3: document.getElementById("validationHit3"),
  validationAvgReturnLabel: document.getElementById("validationAvgReturnLabel"),
  validationAvgReturn: document.getElementById("validationAvgReturn"),
  validationExecutionSkipped: document.getElementById("validationExecutionSkipped"),
  validationPendingOutcome: document.getElementById("validationPendingOutcome"),
  tuningTitle: document.getElementById("tuningTitle"),
  tuningStatus: document.getElementById("tuningStatus"),
  tuningBody: document.getElementById("tuningBody"),
  generateTuningBtn: document.getElementById("generateTuningBtn"),
  nextDayCompareTitle: document.getElementById("nextDayCompareTitle"),
  nextDayCompareGrid: document.getElementById("nextDayCompareGrid"),
  validationLineTitle: document.getElementById("validationLineTitle"),
  validationDatesBody: document.getElementById("validationDatesBody"),
  validationDatesPager: document.getElementById("validationDatesPager"),
  validationDatesPrev: document.getElementById("validationDatesPrev"),
  validationDatesNext: document.getElementById("validationDatesNext"),
  validationDatesPageLabel: document.getElementById("validationDatesPageLabel"),
  validationDetailTitle: document.getElementById("validationDetailTitle"),
  validationDetailBody: document.getElementById("validationDetailBody"),
  detailsPanel: document.getElementById("detailsPanel"),
  detailsTitle: document.getElementById("detailsTitle"),
  detailsSummary: document.getElementById("detailsSummary"),
  newsList: document.getElementById("newsList"),
  closeDetails: document.getElementById("closeDetails"),
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
  const consensus = payload.meta?.strategy_consensus?.rows || [];
  const marketRegime = payload.meta?.market_regime || {};
  const shouldRenderTables = rememberFingerprint("recommendations", {
    shortTerm,
    consensus,
    marketRegime,
  });
  state.lastRows.shortTerm = shortTerm;
  state.lastRows.consensus = consensus;
  state.marketRegime = marketRegime;
  renderMetrics(payload);
  if (shouldRenderTables) {
    renderOverviewRegime(state.marketRegime);
    renderDecisionDesk(state.lastRows.consensus);
    rerenderCurrentTables();
  }
  if (state.tomorrowLoaded) {
    loadTomorrowPicks({ background: true });
  }
  if (state.horizonLoaded) {
    loadHorizonPicks({ background: true });
  }
  if (shouldRenderTables) {
    const generatedAt = payload.meta?.generated_at || "最近快照";
    setStatus(`后端推送更新 ${generatedAt}`);
  }
}

async function loadRecommendations() {
  setStatus("刷新中...");
  const params = new URLSearchParams({
    top_n: String(window.APP_CONFIG.defaultTopN || 18),
    market: els.marketSelect.value,
  });
  try {
    const res = await fetch(`/api/recommendations?${params.toString()}`);
    const payload = await res.json();
    applyRecommendationsPayload(payload);
  } catch (err) {
    const message = `<tr><td colspan="16" class="empty">${escapeHtml(err.message)}</td></tr>`;
    els.shortTermBody.innerHTML = message;
    if (els.strategyConsensusBody) {
      els.strategyConsensusBody.innerHTML = '<tr><td colspan="8" class="empty">加载失败</td></tr>';
    }
    setStatus(`刷新失败：${err.message}`);
  }
}

async function loadLatestRecommendationSnapshot() {
  const params = new URLSearchParams({
    top_n: String(window.APP_CONFIG.defaultTopN || 18),
    market: els.marketSelect.value,
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
    market: els.marketSelect.value,
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
      state.streamRetryTimer = setTimeout(connectRecommendationStream, 3000);
    }
  };
}

async function startRecommendationStreamWithSnapshot() {
  await loadLatestRecommendationSnapshot();
  connectRecommendationStream();
}

async function loadStrategyOverview() {
  state.overviewLoaded = true;
  if (!els.strategyOverviewGrid || !els.strategyOverviewBody) {
    return;
  }
  els.strategyOverviewGrid.innerHTML = '<div class="empty">加载中...</div>';
  els.strategyOverviewBody.innerHTML = '<tr><td colspan="13" class="empty">加载中...</td></tr>';
  try {
    const res = await fetch("/api/strategy-overview?days=20");
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "接口返回异常");
    }
    renderStrategyOverview(payload);
  } catch (err) {
    els.strategyOverviewGrid.innerHTML = `<div class="empty">${escapeHtml(err.message)}</div>`;
    els.strategyOverviewBody.innerHTML = `<tr><td colspan="13" class="empty">${escapeHtml(err.message)}</td></tr>`;
  }
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
    renderValidationDeepseekReview(payload.deepseek_review || {});
    if (!options.skipAutoOutcomeUpdate) {
      autoFillMissingValidationOutcomes(payload.metrics || {}, payload.dates || []);
    }
    loadTuningLatest(strategy);
    loadValidationOverview();
  } catch (err) {
    /* 指标慢或失败不影响批次列表和明细查看 */
  }
}

function applyValidationPayload(payload) {
  if (payload.metrics) {
    state.validationMetrics = payload.metrics || {};
    renderValidationMetrics(state.validationMetrics);
  }
  if (payload.deepseek_review) {
    renderValidationDeepseekReview(payload.deepseek_review || {});
  }
  renderValidationDates(payload.dates || []);
  syncValidationSelection(payload.dates || []);
}

async function loadTuningLatest(strategy = currentValidationStrategy()) {
  if (!els.tuningBody) return;
  const params = new URLSearchParams({ strategy });
  try {
    const res = await fetch(`/api/strategy-validation/tuning?${params.toString()}`);
    const payload = await res.json();
    if (!payload.ok || strategy !== currentValidationStrategy()) {
      return;
    }
    renderTuningRun(payload.latest || null, strategy);
  } catch (err) {
    /* 调参建议不影响验证主流程 */
  }
}

async function generateTuningPlan() {
  const strategy = currentValidationStrategy();
  const days = els.validationDaysSelect.value;
  if (!els.tuningBody || !els.generateTuningBtn) return;
  els.generateTuningBtn.disabled = true;
  setOpsStatus(els.tuningStatus, "正在生成影子调参建议…", "pending");
  const params = new URLSearchParams({ strategy, days });
  try {
    const res = await fetch(`/api/strategy-validation/tuning?${params.toString()}`, { method: "POST" });
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "接口返回异常");
    }
    renderTuningRun(payload.latest || { plan: payload.plan, run_time: payload.saved?.run_time }, strategy);
    setOpsStatus(els.tuningStatus, "已保存影子调参建议，正式策略未修改。", "ok");
  } catch (err) {
    setOpsStatus(els.tuningStatus, `生成失败：${err.message}`, "bad");
  } finally {
    els.generateTuningBtn.disabled = false;
  }
}

function renderTuningRun(run, strategy = currentValidationStrategy()) {
  if (!els.tuningBody) return;
  if (els.tuningTitle) {
    els.tuningTitle.textContent = `${strategyLabel(strategy)}影子调参`;
  }
  const plan = run?.plan || run || null;
  if (!plan || !Object.keys(plan).length) {
    els.tuningBody.innerHTML = '<div class="empty">暂无调参建议</div>';
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
  els.tuningBody.innerHTML = `
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
  `;
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

function validationStrategyMeta(strategy) {
  const label = strategyLabel(strategy);
  if (strategy === "swing_picks") {
    return { label, horizon: "5日", focus: "2-5天样本", outcome: "5日净收益" };
  }
  return { label, horizon: "次日", focus: "次日样本", outcome: "次日净收益" };
}

function updateValidationChrome(strategy) {
  const meta = validationStrategyMeta(strategy);
  syncValidationStrategyTabs(strategy);
  if (els.validationTitle) {
    els.validationTitle.textContent = `${meta.label}复盘`;
  }
  if (els.validationSubtitle) {
    els.validationSubtitle.textContent = `查看${meta.label}已保存样本、真实回填和回放表现，主看${meta.outcome}与执行状态。`;
  }
  if (els.validationGuidePrimary) {
    els.validationGuidePrimary.textContent = `${meta.horizon}真实收益`;
  }
  if (els.validationOpsHint) {
    els.validationOpsHint.textContent = `后台每30分钟自动回填${meta.horizon}结果；无真实样本时会自动补齐再更新。`;
  }
  if (els.validationWinRateLabel) {
    els.validationWinRateLabel.textContent = `${meta.horizon}净胜率`;
  }
  if (els.validationAvgReturnLabel) {
    els.validationAvgReturnLabel.textContent = `${meta.horizon}净收益`;
  }
  if (els.nextDayCompareTitle) {
    els.nextDayCompareTitle.textContent = strategy === "tomorrow_picks" ? "次日涨跌对比" : "次日参考对比";
  }
  if (els.validationLineTitle) {
    els.validationLineTitle.textContent = `${meta.label}${meta.horizon}净胜率走势`;
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

// C2-4：各策略主周期方向命中率走势折线 + 顶部一眼结论记分牌。
async function loadValidationOverview() {
  try {
    const selected = currentValidationStrategy();
    const res = await fetch(`/api/validation-overview?days=${els.validationDaysSelect.value}&strategy=${encodeURIComponent(selected)}`);
    const payload = await res.json();
    if (!payload.ok) return;
    renderValidationScoreboard(payload.series || []);
    renderValidationLine(payload.series || []);
  } catch (err) {
    /* 静默失败：折线只是辅助视图，不影响验证主表 */
  }
}

// 一眼结论记分牌：每策略主周期方向表现徽章 + 一句话结论。
function renderValidationScoreboard(series) {
  if (!els.validationScoreboard) return;
  if (!series.length) {
    els.validationScoreboard.innerHTML = '<div class="empty">暂无验证数据。后台会每30分钟自动保存策略快照并回填真实结果。</div>';
    return;
  }
  els.validationScoreboard.innerHTML = series
    .map((s) => {
      const samples = Number(s.sample_count) || 0;
      const realSamples = Number(s.real_sample_count) || 0;
      const replaySamples = Number(s.replay_sample_count) || 0;
      const win = s.real_win_rate_primary_net ?? s.win_rate_primary_net;
      const avg = s.real_avg_primary_return_net ?? s.avg_primary_return_net;
      let level, verdict;
      if (realSamples < 10 && samples < 30) {
        level = "neutral";
        verdict = "先别信：样本不足";
      } else if (realSamples < 10) {
        level = "watch";
        verdict = "谨慎看：真实样本少";
      } else if (win == null) {
        level = "neutral";
        verdict = "暂无结论";
      } else if (win >= 55 && Number(avg || 0) > 0) {
        level = "good";
        verdict = "可观察：方向更稳定";
      } else if (win >= 50) {
        level = "watch";
        verdict = "一般：继续观察";
      } else {
        level = "bad";
        verdict = "暂不加权：表现偏弱";
      }
      const winText = win == null ? "-" : `${Number(win).toFixed(0)}%`;
      const avgText = avg == null ? "-" : `${formatNumber(avg, 2)}%`;
      const horizon = s.primary_horizon_label || "主周期";
      return `
        <div class="score-card score-${level}">
          <div class="score-card-head">
            <span class="score-strategy">${escapeHtml(s.label || s.strategy)}</span>
            <span class="score-badge">${winText}</span>
          </div>
          <div class="score-verdict">${verdict}</div>
          <div class="score-meta">${escapeHtml(horizon)} · 平均涨跌 ${avgText} · 真实 ${realSamples} 条，回放 ${replaySamples} 条</div>
        </div>`;
    })
    .join("");
}

function renderValidationLine(series) {
  const active = (series || []).filter((s) => (s.daily || []).length);
  if (!active.length) {
    renderChart("validationLine", {
      title: { text: "暂无验证数据（等待后台自动保存与回填）", left: "center", top: "middle", textStyle: { color: CHART_THEME.text, fontSize: 13 } },
    });
    return;
  }
  const allDates = [];
  active.forEach((s) => s.daily.forEach((d) => allDates.includes(d.date) || allDates.push(d.date)));
  allDates.sort();
  const lines = active.map((s) => {
    const byDate = {};
    s.daily.forEach((d) => (byDate[d.date] = d.win_rate));
    return {
      name: s.label,
      type: "line",
      smooth: true,
      connectNulls: true,
      data: allDates.map((date) => (byDate[date] == null ? null : Number(byDate[date]))),
    };
  });
  renderChart("validationLine", {
    grid: { left: 44, right: 18, top: 36, bottom: 48 },
    tooltip: { trigger: "axis", valueFormatter: (v) => (v == null ? "-" : `${Number(v).toFixed(1)}%`) },
    legend: { top: 0, type: "scroll", textStyle: { fontSize: 11 } },
    xAxis: { type: "category", data: allDates, axisLabel: { rotate: 30, fontSize: 10 } },
    yAxis: { type: "value", name: "方向命中率%", min: 0, max: 100 },
    series: lines,
  });
}

// 就地操作反馈：在操作块下方的状态行显示进度/成功/失败。
function setOpsStatus(el, text, level) {
  if (!el) return;
  el.textContent = text;
  el.className = "ops-status" + (level ? ` ops-${level}` : "");
}

async function loadStockPrediction() {
  const raw = els.stockPredictionInput.value.trim();
  const code = raw.replace(/\D/g, "").slice(0, 6);
  if (code.length !== 6) {
    setOpsStatus(els.stockPredictionStatus, "请输入 6 位股票代码。", "bad");
    els.stockPredictionResult.hidden = true;
    return;
  }
  const label = els.stockPredictionBtn.textContent;
  els.stockPredictionBtn.disabled = true;
  els.stockPredictionBtn.textContent = "预测中…";
  setOpsStatus(els.stockPredictionStatus, "正在读取当前行情并套用策略评分…", "pending");
  try {
    const res = await fetch(`/api/stock-prediction/${encodeURIComponent(code)}`);
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "无法给出预测");
    }
    renderStockPrediction(payload);
    setOpsStatus(els.stockPredictionStatus, "预测已更新。", "ok");
  } catch (err) {
    els.stockPredictionResult.hidden = false;
    els.stockPredictionResult.innerHTML = `
      <div class="prediction-empty">
        <strong>无法预测</strong>
        <p>${escapeHtml(err.message)}</p>
      </div>
    `;
    setOpsStatus(els.stockPredictionStatus, `预测失败：${err.message}`, "bad");
  } finally {
    els.stockPredictionBtn.disabled = false;
    els.stockPredictionBtn.textContent = label;
  }
}

function renderStockPrediction(payload) {
  const p = payload.prediction || {};
  const cls = predictionClass(p.direction);
  const horizons = payload.horizons || {};
  const hits = payload.strategy_hits || [];
  const missed = payload.missed_strategies || [];
  const riskFlags = payload.risk_flags || [];
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
  els.stockPredictionResult.hidden = false;
  els.stockPredictionResult.innerHTML = `
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
    <p class="prediction-disclaimer">${escapeHtml(payload.disclaimer || "")}</p>
  `;
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
  state.tomorrowLoaded = true;
  const background = Boolean(options.background);
  if (!background || !hasRows(state.lastRows.tomorrow)) {
    els.tomorrowBody.innerHTML = '<tr><td colspan="16" class="empty">加载中...</td></tr>';
  }
  const params = new URLSearchParams({
    top_n: String(window.APP_CONFIG.defaultTopN || 18),
    market: els.marketSelect.value,
  });
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
      els.tomorrowBody.innerHTML = `<tr><td colspan="16" class="empty">${escapeHtml(err.message)}</td></tr>`;
    }
    if (!background) {
      setStatus(`明天推荐加载失败：${err.message}`);
    }
  }
}

async function loadHorizonPicks(options = {}) {
  state.horizonLoaded = true;
  const background = Boolean(options.background);
  if (!background || !hasRows(state.lastRows.swing)) {
    els.swingBody.innerHTML = '<tr><td colspan="16" class="empty">加载中...</td></tr>';
  }
  const params = new URLSearchParams({
    top_n: String(window.APP_CONFIG.defaultTopN || 18),
    market: els.marketSelect.value,
  });
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
      els.swingBody.innerHTML = `<tr><td colspan="16" class="empty">${escapeHtml(err.message)}</td></tr>`;
    }
    if (!background) {
      setStatus(`2-5天加载失败：${err.message}`);
    }
  }
}

function renderMetrics(payload) {
  const health = payload.health || {};
  const meta = payload.meta || {};
  const marketSentiment = payload.market_sentiment || {};
  els.quoteSource.textContent = health.quotes_source || "-";
  els.sentimentSource.textContent = health.sentiment_source || "-";
  els.candidateCount.textContent = meta.candidate_count ?? "-";
  renderHardFilterStatus(meta.hard_filter_report);
  els.marketSentiment.textContent = marketSentiment.score ? `${marketSentiment.score}` : "-";
  renderRiskBlacklistStatus(meta.risk_blacklist || payload.risk_blacklist);
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

function renderStrategyOverview(payload) {
  const strategies = payload.strategies || [];
  const best = payload.best_strategy;
  const marketRegime = payload.market_regime || {};
  const verifiedCount = strategies.filter(row => Number(row.metrics?.sample_count || 0) > 0).length;
  const sampleCount = strategies.reduce((sum, row) => sum + Number(row.metrics?.sample_count || 0), 0);
  renderOverviewRegime(Object.keys(marketRegime).length ? marketRegime : state.marketRegime);
  if (!els.strategyOverviewGrid || !els.strategyOverviewBody || !els.overviewBestStrategy) {
    return;
  }
  els.overviewBestStrategy.textContent = best ? best.label : "暂无";
  els.overviewVerifiedCount.textContent = `${verifiedCount}/${strategies.length}`;
  els.overviewSampleCount.textContent = sampleCount;
  els.overviewDays.textContent = `近${payload.days || 20}个保存日`;

  if (!strategies.length) {
    els.strategyOverviewGrid.innerHTML = '<div class="empty">暂无策略</div>';
    els.strategyOverviewBody.innerHTML = '<tr><td colspan="13" class="empty">暂无策略</td></tr>';
    return;
  }

  els.strategyOverviewGrid.innerHTML = strategies.map(row => {
    const metrics = row.metrics || {};
    const status = row.status || {};
    return `
      <article class="strategy-card status-${escapeHtml(status.level || "pending")}">
        <div class="strategy-card-head">
          <h3>${escapeHtml(row.label)}</h3>
          <span>${escapeHtml(row.horizon)}</span>
        </div>
        <p>${escapeHtml(row.goal || "")}</p>
        <div class="strategy-card-metrics">
          <div><span>样本</span><strong>${metrics.sample_count ?? 0}</strong></div>
          <div><span>真/回</span><strong>${metrics.real_sample_count ?? 0}/${metrics.replay_sample_count ?? 0}</strong></div>
          <div><span>跳过</span><strong class="${Number(metrics.execution_skipped_count || 0) ? "risk-text" : ""}">${metrics.execution_skipped_count ?? 0}</strong></div>
          <div><span>${escapeHtml(metrics.primary_horizon_label || "主周期")}净</span><strong>${formatPercent(metrics.avg_primary_return_net)}</strong></div>
        </div>
        <div class="strategy-status">${escapeHtml(status.label || "待验证")}</div>
      </article>
    `;
  }).join("");
  els.strategyOverviewBody.innerHTML = strategies.map(row => {
    const metrics = row.metrics || {};
    const latest = row.latest_signal || {};
    const status = row.status || {};
    return `
      <tr>
        <td>${escapeHtml(row.label)}</td>
        <td>${escapeHtml(row.version)}</td>
        <td>${escapeHtml(row.horizon)}</td>
        <td class="num">${metrics.sample_count ?? 0}</td>
        <td class="num">${metrics.real_sample_count ?? 0}/${metrics.replay_sample_count ?? 0}</td>
        <td class="num ${Number(metrics.execution_skipped_count || 0) ? "risk-text" : ""}">${metrics.execution_skipped_count ?? 0}</td>
        <td class="num ${numberClass(metrics.avg_primary_return_net)}">${formatPercent(metrics.avg_primary_return_net)}</td>
        <td class="num">${formatPercent(metrics.win_rate_primary_net)}</td>
        <td class="num ${numberClass(metrics.real_avg_primary_return_net)}">${formatPercent(metrics.real_avg_primary_return_net)}</td>
        <td class="num ${numberClass(metrics.avg_max_drawdown_3d)}">${formatPercent(metrics.avg_max_drawdown_3d)}</td>
        <td>${escapeHtml(latest.signal_date || "-")}</td>
        <td><span class="tag ${status.level === "bad" ? "risk" : ""}">${escapeHtml(status.label || "待验证")}</span></td>
        <td>${escapeHtml(status.advice || "-")}</td>
      </tr>
    `;
  }).join("");
}

function renderOverviewRegime(regime) {
  if (els.overviewRegimeLabel) {
    els.overviewRegimeLabel.textContent = regime.label || "-";
    els.overviewRegimeScore.textContent = regime.score != null ? formatNumber(regime.score, 1) : "-";
    els.overviewRegimeBreadth.textContent = regime.breadth_pct != null ? `${formatNumber(regime.breadth_pct, 1)}%` : "-";
    els.overviewRegimeMedian.textContent = regime.median_pct_chg != null ? `${formatNumber(regime.median_pct_chg, 2)}%` : "-";
    els.overviewRegimeStrong.textContent = regime.strong_pct != null ? `${formatNumber(regime.strong_pct, 1)}%` : "-";
    els.overviewRegimeAdvice.textContent = regime.advice || "-";
  }
  els.decisionRegimeLabel.textContent = regime.label || "-";
  els.decisionAdvice.textContent = regime.advice || "等待行情刷新";
  renderRegimeGauge(regime);
}

// C2-1：市场环境仪表盘。
function renderRegimeGauge(regime) {
  const score = Number(regime.score);
  const safeScore = Number.isFinite(score) ? Math.max(0, Math.min(100, score)) : 0;
  const color = regime.label && regime.label.includes("进攻")
    ? CHART_THEME.positive
    : regime.label && regime.label.includes("防守")
    ? CHART_THEME.negative
    : CHART_THEME.accent;
  renderChart("regimeGauge", {
    series: [
      {
        type: "gauge",
        min: 0,
        max: 100,
        radius: "92%",
        progress: { show: true, width: 12, itemStyle: { color } },
        axisLine: { lineStyle: { width: 12, color: [[1, CHART_THEME.track]] } },
        axisTick: { show: false },
        splitLine: { length: 10, lineStyle: { color: CHART_THEME.split } },
        axisLabel: { distance: 14, fontSize: 10, color: CHART_THEME.text },
        pointer: { width: 4, itemStyle: { color } },
        detail: {
          valueAnimation: true,
          fontSize: 26,
          offsetCenter: [0, "62%"],
          formatter: () => `${safeScore.toFixed(0)}\n${regime.label || ""}`,
          color: CHART_THEME.strong,
        },
        data: [{ value: safeScore }],
      },
    ],
  });
}

function renderStrategyConsensus(rows) {
  if (!els.strategyConsensusBody) {
    return;
  }
  const displayRows = filterAndSortRows(rows, { consensus: true });
  if (!displayRows.length) {
    els.strategyConsensusBody.innerHTML = '<tr><td colspan="8" class="empty">暂无 2 策略以上共识标的</td></tr>';
    return;
  }
  els.strategyConsensusBody.innerHTML = displayRows.map(row => `
    <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
      <td class="num">${escapeHtml(row.code)}</td>
      <td>${escapeHtml(row.name)}</td>
      <td>${escapeHtml(row.market_label || "-")}</td>
      <td><span class="tag ${row.level === "high" ? "strategy" : row.level === "medium" ? "validation" : "stable"}">${escapeHtml(row.label || "-")}</span></td>
      <td class="num">${row.appearances}/${row.strategy_count}</td>
      <td><span class="tag ${actionTagClass(row.action_label)}">${escapeHtml(row.action_label || "-")}</span></td>
      <td class="reasons">${(row.strategies || []).map(text => `<span class="tag stable">${escapeHtml(text)}</span>`).join("")}</td>
      <td class="reasons">${consensusReasonTags(row)}</td>
    </tr>
  `).join("");
  bindSentimentRows(els.strategyConsensusBody);
  renderConsensusScatter(displayRows);
}

function consensusReasonTags(row) {
  const tags = [];
  const added = new Set();
  const add = (text, cls = "") => {
    const value = String(text || "").trim();
    if (!value || added.has(value) || tags.length >= 4) return;
    added.add(value);
    tags.push(`<span class="tag ${cls}">${escapeHtml(value)}</span>`);
  };
  const appearances = Number(row.appearances || 0);
  if (appearances >= 2) {
    add(`${appearances}个策略同时入选`, "stable");
  }
  (row.evidence || []).forEach((text) => {
    const value = String(text || "");
    if (value.includes("弱") || value.includes("风险")) return;
    add(value, "stable");
  });
  if (row.theme) {
    add(row.theme, "strategy");
  }
  if (isPriorityAction(row.action_label)) {
    add(row.action_label, "strategy");
  }
  if (tags.length < 2) {
    (row.strategies || []).slice(0, 2).forEach(text => add(`${text}入选`, "stable"));
  }
  if (!tags.length) {
    add(row.label || "共识入选", "stable");
  }
  return tags.join("");
}

// C2-3：共识热度散点。x=出现次数, y=推荐强度, 气泡大小=一致性(agreement)。
function renderConsensusScatter(rows) {
  const data = (rows || []).map((row) => ({
    name: row.name,
    value: [
      Number(row.appearances) || 0,
      Number(row.consensus_score) || 0,
      Number(row.agreement) || 0,
    ],
  }));
  renderChart("consensusScatter", {
    grid: { left: 44, right: 18, top: 18, bottom: 34 },
    tooltip: {
      formatter: (p) =>
        `${escapeHtml(p.data.name)}<br/>出现 ${p.value[0]} 次<br/>推荐强度 ${p.value[1].toFixed(1)}<br/>一致性 ${(p.value[2] * 100).toFixed(0)}%`,
    },
    xAxis: { name: "出现次数", min: 0, minInterval: 1, axisLine: { lineStyle: { color: CHART_THEME.axis } } },
    yAxis: { name: "推荐强度", min: 0, max: 100, axisLine: { lineStyle: { color: CHART_THEME.axis } } },
    series: [
      {
        type: "scatter",
        symbolSize: (val) => 12 + val[2] * 22,
        itemStyle: {
          color: (p) => (p.value[2] >= 0.6 ? CHART_THEME.positive : p.value[2] >= 0.3 ? CHART_THEME.accent : CHART_THEME.muted),
          opacity: 0.78,
        },
        data,
      },
    ],
  });
}

function renderDecisionDesk(rows) {
  const consensusRows = rows || [];
  const priorityRows = consensusRows.filter(row => isPriorityAction(row.action_label));
  const lowRiskRows = consensusRows.filter(row => Number(row.avg_risk) <= 45);
  els.decisionConsensusCount.textContent = consensusRows.length;
  els.decisionPriorityCount.textContent = priorityRows.length;
  els.decisionAvgRisk.textContent = lowRiskRows.length || "-";
}

function explanationTags(row) {
  const tags = [];
  const added = new Set();
  const pushTag = (label, text, cls = "") => {
    const value = String(text || "").trim();
    if (!value || added.has(`${label}:${value}`) || tags.length >= 5) return;
    added.add(`${label}:${value}`);
    tags.push(`<span class="tag ${cls}">${escapeHtml(label)}:${escapeHtml(value)}</span>`);
  };

  if (row.consensus_signal) {
    const consensus = row.consensus_signal;
    pushTag("共识", `${consensus.label || "-"} ${consensus.appearances || 0}/${consensus.strategy_count || 0}`, "stable");
  }

  if (row.rerank_source === "deepseek" || row.deepseek_action) {
    const actionLabel = row.deepseek_action === "priority"
      ? "优先"
      : row.deepseek_action === "avoid"
      ? "回避"
      : row.deepseek_action === "watch"
      ? "观察"
      : "复核";
    const cls = row.deepseek_action === "priority" ? "stable" : row.deepseek_action === "avoid" ? "risk" : "warning";
    pushTag("DeepSeek", `${actionLabel} ${formatNumber(row.deepseek_rank_score, 1)}`, cls);
  }
  (row.deepseek_risk_flags || []).slice(0, 2).forEach(text => pushTag("DS风险", text, "warning"));
  if (row.deepseek_reason) {
    pushTag("DS理由", row.deepseek_reason, row.deepseek_action === "avoid" ? "risk" : "validation");
  }
  (row.deepseek_profit_flags || []).slice(0, 2).forEach(text => pushTag("次日优势", text, "stable"));
  (row.reasons || []).slice(0, 3).forEach(text => pushTag("推荐", text));

  const profile = row.serenity_profile || {};
  (profile.evidence || []).slice(0, 1).forEach(item => pushTag("证据", item.label || "-", "stable"));

  if (!tags.length) {
    pushTag("推荐", "综合分靠前");
  }
  return tags.join("");
}

function riskTag(prefix, risk) {  if (!risk) {
    return `<span class="tag stable">${prefix}:未知</span>`;
  }
  const level = risk.level || "low";
  const cls = level === "high" ? "risk" : level === "medium" ? "warning" : "stable";
  const label = risk.label || (level === "high" ? "高" : level === "medium" ? "中" : "低");
  return `<span class="tag ${cls}">${escapeHtml(prefix)}:${escapeHtml(label)}</span>`;
}

function similarSignalStatsTag(stats) {
  if (!stats || !Number(stats.sample_count || 0)) {
    return '<span class="tag stable">同类验证:暂无主样本</span>';
  }
  const sample = Number(stats.sample_count || 0);
  const real = Number(stats.real_sample_count || 0);
  const replay = Number(stats.replay_sample_count || 0);
  const horizon = stats.primary_horizon_label || "主周期";
  const win = stats.win_rate_primary_net == null ? "-" : `${formatNumber(stats.win_rate_primary_net, 1)}%`;
  const avg = stats.avg_primary_return_net == null ? "-" : `${formatNumber(stats.avg_primary_return_net, 2)}%`;
  return `<span class="tag validation">同类${sample}主样本 ${escapeHtml(horizon)}净胜:${win} 净均:${avg} 真/回:${real}/${replay}</span>`;
}

function rowIndustryLabel(row) {
  return row.industry || row.theme || "行业未知";
}

function codeCell(row) {
  return `
    <td class="stock-cell">
      <span class="code-main">${escapeHtml(row.code)}</span>
      <span class="stock-name-inline">${escapeHtml(row.name || "-")}</span>
      <span class="code-sub">${escapeHtml(rowIndustryLabel(row))}</span>
    </td>`;
}

function marketCapCell(row) {
  const cap = Number(row.market_cap);
  return `<td class="num market-cap-cell">${Number.isFinite(cap) && cap > 0 ? formatMoney(cap) : "-"}</td>`;
}

function renderShortTermTable(rows) {
  const displayRows = filterAndSortRows(rows);
  if (!displayRows.length) {
    els.shortTermBody.innerHTML = '<tr><td colspan="16" class="empty">暂无符合条件的股票</td></tr>';
    return;
  }
  els.shortTermBody.innerHTML = displayRows.map(row => {
    const pctClass = row.pct_chg >= 0 ? "positive" : "negative";
    const explanation = `${stabilityTag(row)}${explanationTags(row)}`;
    return `
      <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
        <td class="num">${row.rank}</td>
        ${codeCell(row)}
        <td>${escapeHtml(row.name)}</td>
        <td>${escapeHtml(row.market_label)}</td>
        <td class="num">${formatNumber(row.price, 3)}</td>
        <td class="num ${pctClass}">${formatNumber(row.pct_chg, 2)}%</td>
        <td class="num">${formatNumber(row.speed || row.five_min_pct, 2)}%</td>
        <td class="num">${formatNumber(row.volume_ratio, 2)}</td>
        <td class="num">${formatNumber(row.turnover_rate, 2)}%</td>
        <td class="num">${formatMoney(row.turnover)}</td>
        ${marketCapCell(row)}
        <td>${escapeHtml(row.industry || row.theme || "-")}</td>
        <td class="num">${formatNumber(row.momentum_score, 1)}</td>
        <td class="num">${formatNumber(row.sentiment_score, 1)}</td>
        ${scoreCell(row)}
        <td class="reasons">${explanation}</td>
      </tr>
    `;
  }).join("");

  bindSentimentRows(els.shortTermBody);
}

function renderTomorrowTable(rows) {
  const displayRows = filterAndSortRows(rows);
  if (!displayRows.length) {
    els.tomorrowBody.innerHTML = '<tr><td colspan="16" class="empty">暂无符合条件的股票</td></tr>';
    return;
  }
  els.tomorrowBody.innerHTML = displayRows.map(row => {
    const pctClass = row.pct_chg >= 0 ? "positive" : "negative";
    const sixtyClass = row.sixty_day_pct >= 0 ? "positive" : "negative";
    const tier = row.tier_label ? `<span class="tag stable">${escapeHtml(row.tier_label)}</span>` : "";
    const explanation = `${tier}${explanationTags(row)}`;
    return `
      <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
        <td class="num">${row.rank}</td>
        ${codeCell(row)}
        <td>${escapeHtml(row.name)}</td>
        <td>${escapeHtml(row.market_label)}</td>
        <td class="num">${formatNumber(row.price, 3)}</td>
        <td class="num ${pctClass}">${formatNumber(row.pct_chg, 2)}%</td>
        <td class="num">${formatNumber(row.volume_ratio, 2)}</td>
        <td class="num">${formatNumber(row.turnover_rate, 2)}%</td>
        <td class="num">${formatMoney(row.turnover)}</td>
        ${marketCapCell(row)}
        <td class="num ${sixtyClass}">${formatNumber(row.sixty_day_pct, 2)}%</td>
        <td class="num">${formatNumber(row.liquidity_score, 1)}</td>
        <td class="num">${formatNumber(row.momentum_score, 1)}</td>
        <td class="num">${formatNumber(row.trend_score, 1)}</td>
        ${scoreCell(row)}
        <td class="reasons">${explanation}</td>
      </tr>
    `;
  }).join("");

  bindSentimentRows(els.tomorrowBody);
}

function renderSwingTable(rows) {
  const displayRows = filterAndSortRows(rows);
  if (!displayRows.length) {
    els.swingBody.innerHTML = '<tr><td colspan="16" class="empty">暂无符合条件的2-5天股票</td></tr>';
    return;
  }
  els.swingBody.innerHTML = displayRows.map(row => {
    const pctClass = row.pct_chg >= 0 ? "positive" : "negative";
    const ret5Class = row.ret_5d >= 0 ? "positive" : "negative";
    const ret10Class = row.ret_10d >= 0 ? "positive" : "negative";
    const ret20Class = row.ret_20d >= 0 ? "positive" : "negative";
    const explanation = explanationTags(row);
    return `
      <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
        <td class="num">${row.rank}</td>
        ${codeCell(row)}
        <td>${escapeHtml(row.name)}</td>
        <td>${escapeHtml(row.market_label)}</td>
        <td class="num">${formatNumber(row.price, 3)}</td>
        <td class="num ${pctClass}">${formatNumber(row.pct_chg, 2)}%</td>
        <td class="num ${ret5Class}">${formatNumber(row.ret_5d, 2)}%</td>
        <td class="num ${ret10Class}">${formatNumber(row.ret_10d, 2)}%</td>
        <td class="num ${ret20Class}">${formatNumber(row.ret_20d, 2)}%</td>
        <td class="num">${formatNumber(row.ma20_gap, 2)}%</td>
        <td class="num">${formatMoney(row.turnover)}</td>
        ${marketCapCell(row)}
        <td class="num">${formatNumber(row.momentum_score, 1)}</td>
        <td class="num">${formatNumber(row.trend_score, 1)}</td>
        ${scoreCell(row)}
        <td class="reasons">${explanation}</td>
      </tr>
    `;
  }).join("");
  bindSentimentRows(els.swingBody);
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
  if (els.validationHit3) {
    els.validationHit3.textContent = `真 ${real} / 回 ${replay}`;
  }
  els.validationAvgReturn.textContent = avgReturn != null ? `${horizon} ${formatNumber(avgReturn, 2)}%` : "-";
  if (els.validationExecutionSkipped) {
    els.validationExecutionSkipped.textContent = executionSkipped ? `${executionSkipped}` : "0";
    els.validationExecutionSkipped.className = executionSkipped ? "risk-text" : "";
  }
  if (els.validationPendingOutcome) {
    const coverageText = coverage != null ? ` / ${formatNumber(coverage, 0)}%` : "";
    els.validationPendingOutcome.textContent = pendingOutcome ? `${pendingOutcome}${coverageText}` : `0${coverageText}`;
    els.validationPendingOutcome.className = pendingOutcome ? "warning-text" : "";
  }
  renderValidationSimpleDecision({ sample, outcome, real, replay, winRate, avgReturn, horizon, executionSkipped, pendingOutcome });
}

function renderValidationBatchSummary(rows, date, strategy, summary = null) {
  const meta = validationStrategyMeta(strategy);
  const localSummary = summary || validationBatchSummaryFromRows(rows);
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

function validationBatchSummaryFromRows(rows) {
  const validRows = (rows || [])
    .map(row => ({ row, netReturn: primaryValidationNetReturn(row) }))
    .filter(item =>
      Number.isFinite(item.netReturn) &&
      item.row.outcome_updated_at &&
      !validationSkipReason(item.row.skip_reason)
    );
  const sample = validRows.length;
  const up = validRows.filter(item => item.netReturn > 0).length;
  const down = validRows.filter(item => item.netReturn < 0).length;
  const flat = sample - up - down;
  return {
    sample_count: sample,
    up_count: up,
    down_count: down,
    flat_count: flat,
    win_rate: sample > 0 ? (up / sample) * 100 : null,
    avg_return: sample > 0 ? validRows.reduce((sum, item) => sum + item.netReturn, 0) / sample : null,
  };
}

function renderNextDayCompare(compare, replayCompare = {}) {
  if (!els.nextDayCompareGrid) return;
  const cards = [
    ["信号到次收", compare.avg_signal_to_next_close],
    ["开盘到收盘", compare.avg_next_open_to_close],
    ["次日涨跌", compare.avg_signal_to_next_close_net],
    ["次日命中率", compare.win_rate_signal_to_next_close, "%"],
  ];
  if (!Number(compare.sample_count || 0)) {
    if (Number(replayCompare.sample_count || 0)) {
      els.nextDayCompareGrid.innerHTML = `
        <div class="score-card score-neutral">
          <div class="score-card-head">
            <span class="score-strategy">真实样本</span>
            <span class="score-badge">等待</span>
          </div>
          <div class="score-meta">暂无真实次日对比，回放仅作参考</div>
        </div>
        <div class="score-card score-watch">
          <div class="score-card-head">
            <span class="score-strategy">回放参考涨跌</span>
            <span class="score-badge ${numberClass(Number(replayCompare.avg_signal_to_next_close_net || 0))}">${formatNumber(replayCompare.avg_signal_to_next_close_net, 2)}%</span>
          </div>
          <div class="score-meta">回放样本 ${replayCompare.sample_count || 0} 条，仅作参考</div>
        </div>
        <div class="score-card score-watch">
          <div class="score-card-head">
            <span class="score-strategy">回放参考命中率</span>
            <span class="score-badge">${formatNumber(replayCompare.win_rate_signal_to_next_close, 1)}%</span>
          </div>
          <div class="score-meta">真实样本优先，继续等待自动回填</div>
        </div>
      `;
      return;
    }
    els.nextDayCompareGrid.innerHTML = '<div class="empty">暂无次日对比数据</div>';
    return;
  }
  const replayReference = Number(replayCompare.sample_count || 0)
    ? `
      <div class="score-card score-neutral">
        <div class="score-card-head">
          <span class="score-strategy">回放参考</span>
          <span class="score-badge">${formatNumber(replayCompare.avg_signal_to_next_close_net, 2)}%</span>
        </div>
        <div class="score-meta">回放 ${replayCompare.sample_count || 0} 条，仅作参考</div>
      </div>
    `
    : "";
  els.nextDayCompareGrid.innerHTML = cards.map(([label, value, suffix]) => {
    const num = Number(value || 0);
    return `
      <div class="score-card ${num >= 0 ? "score-good" : "score-bad"}">
        <div class="score-card-head">
          <span class="score-strategy">${escapeHtml(label)}</span>
          <span class="score-badge ${numberClass(num)}">${formatNumber(num, 2)}${suffix || "%"}</span>
        </div>
        <div class="score-meta">样本 ${compare.sample_count || 0} 条</div>
      </div>
    `;
  }).join("") + replayReference;
}

function renderValidationSimpleDecision({ sample, outcome, real, replay, winRate, avgReturn, horizon, executionSkipped, pendingOutcome }) {
  if (!els.validationSimpleDecision) return;
  let level = "neutral";
  let text = "结论：数据正在更新，先关注锚点方向与锚点到现在变化。";
  if (Number(pendingOutcome || 0) > 0 && sample <= 0) {
    text = `结论：还有 ${pendingOutcome} 条信号待回填，当前先不要用胜率下结论。`;
  } else if (outcome <= 0 && sample <= 0) {
    if (replay > 0) {
      text = `结论：当前仅有回放样本 ${replay} 条，不能作为主判断；请等待快照与实时回填。`;
    } else {
      text = "结论：暂无真实回填结果。系统会自动回填最新锚点样本，稍后自动更新。";
    }
  } else if (sample <= 0 && outcome > 0) {
    text = `结论：已有 ${outcome} 条回填结果，但主周期样本未成熟，先不要据此下结论。`;
  } else if (sample < 30) {
    text = `结论：先别信胜率。当前有效样本 ${sample} 条，少于 30 条，只能观察。`;
  } else if (real < 10) {
    level = "watch";
    text = `结论：谨慎看。有效样本 ${sample} 条，但真实前瞻只有 ${real} 条，回放 ${replay} 条只能粗筛。`;
  } else if (winRate == null || avgReturn == null) {
    text = "结论：统计字段不完整，等待自动更新结果。";
  } else if (Number(executionSkipped || 0) > 0 && sample < 30) {
    text = `结论：先观察。当前有效样本 ${sample} 条，另有 ${executionSkipped} 条不可执行/被剔除样本，先不要据此加权。`;
  } else if (winRate >= 55 && avgReturn > 0) {
    level = "good";
    text = `结论：可观察。${horizon}净胜率 ${formatNumber(winRate, 1)}%，${horizon}平均净收益 ${formatNumber(avgReturn, 2)}%。`;
  } else if (winRate >= 50 && avgReturn >= 0) {
    level = "watch";
    text = `结论：一般，继续观察。${horizon}表现不弱不强，暂不建议提高权重。`;
  } else {
    level = "bad";
    text = `结论：暂不加权。${horizon}表现偏弱，先不要依赖这个策略。`;
  }
  els.validationSimpleDecision.className = `validation-current-decision decision-${level}`;
  els.validationSimpleDecision.textContent = text;
}

function renderValidationDeepseekReview(review) {
  if (!els.validationDeepseekReview) return;
  if (!review || review.enabled === false) {
    const status = review?.status || "disabled";
    els.validationDeepseekReview.className = "validation-current-decision decision-neutral";
    els.validationDeepseekReview.textContent = status === "strategy_not_supported"
      ? "DeepSeek 复盘暂不支持该策略。"
      : status === "runtime_disabled"
      ? "DeepSeek 复盘本地已关闭，当前只使用本地验证指标。"
      : "DeepSeek 复盘暂未启用。";
    return;
  }
  if (review.status === "fallback") {
    els.validationDeepseekReview.className = "validation-current-decision decision-watch";
    els.validationDeepseekReview.textContent = `DeepSeek 复盘暂不可用：${review.error || "接口回退"}`;
    return;
  }
  const avoid = Array.isArray(review.avoid_conditions) ? review.avoid_conditions.slice(0, 4) : [];
  const filters = Array.isArray(review.suggested_filters) ? review.suggested_filters.slice(0, 4) : [];
  const penalties = Array.isArray(review.suggested_penalties) ? review.suggested_penalties.slice(0, 3) : [];
  const rules = Array.isArray(review.rule_candidates) ? review.rule_candidates.slice(0, 3) : [];
  const penaltyText = penalties
    .map(item => `${escapeHtml(item.condition || "-")} 扣${escapeHtml(String(item.penalty ?? "-"))}`)
    .join("；");
  const ruleText = rules
    .map(item => {
      const field = item.field || "-";
      const operator = item.operator || "";
      const threshold = item.threshold ?? "-";
      const penalty = item.penalty ?? "-";
      const reason = item.reason ? `：${item.reason}` : "";
      return `${escapeHtml(field)} ${escapeHtml(operator)} ${escapeHtml(String(threshold))} 扣${escapeHtml(String(penalty))}${escapeHtml(reason)}`;
    })
    .join("；");
  els.validationDeepseekReview.className = "validation-current-decision decision-watch";
  els.validationDeepseekReview.innerHTML = `
    <strong>DeepSeek 反推荐复盘：</strong>${escapeHtml(review.summary || review.decision || "暂无总结")}
    ${avoid.length ? `<br><span>规避条件：${avoid.map(escapeHtml).join("；")}</span>` : ""}
    ${filters.length ? `<br><span>建议过滤：${filters.map(escapeHtml).join("；")}</span>` : ""}
    ${penaltyText ? `<br><span>建议扣分：${penaltyText}</span>` : ""}
    ${ruleText ? `<br><span>规则候选：${ruleText}</span>` : ""}
  `;
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
  return Math.max(1, Math.ceil(state.validationDateRows.length / VALIDATION_DATE_PAGE_SIZE));
}

function clampValidationDatePage() {
  const maxPage = validationDatePageCount() - 1;
  state.validationDatePage = Math.min(Math.max(0, state.validationDatePage), maxPage);
}

function renderValidationDatePage() {
  clampValidationDatePage();
  const start = state.validationDatePage * VALIDATION_DATE_PAGE_SIZE;
  const pageRows = state.validationDateRows.slice(start, start + VALIDATION_DATE_PAGE_SIZE);
  els.validationDatesBody.innerHTML = pageRows.map(row => `
    <tr data-date="${escapeHtml(row.signal_date)}" data-strategy="${escapeHtml(row.strategy_name)}" data-sample-type="${escapeHtml(row.sample_type || "")}">
	      <td>${escapeHtml(row.signal_date)}</td>
	      <td>${validationSampleTypeBadge(row)}</td>
	      <td class="num">${row.count}</td>
	      <td>${escapeHtml(row.signal_time || "-")}</td>
    </tr>
  `).join("");
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
  els.validationDetailBody.innerHTML = rows.map(row => {
    const anchorPrice = Number(row.price_at_signal);
    const anchorChange = row.pct_chg_at_signal;
    const todayChange = row.current_pct_chg;
    const anchorToNow = row.anchor_to_now_return;
    const primaryLabel = primaryValidationLabel(row);
    const primaryReturn = primaryValidationReturn(row);
    const tradeCost = row.trade_cost_pct;
    const skipReason = validationSkipReason(row.skip_reason);
    const executionText = skipReason || (row.outcome_updated_at ? "已回填" : "待回填");
    const executionClass = skipReason ? "risk" : row.outcome_updated_at ? "stable" : "warning";
    const isReplay = String(row.strategy_version || "").toLowerCase().includes("replay");
    const pctText = (value) => {
      const num = Number(value);
      return Number.isFinite(num) ? `${formatNumber(num, 2)}%` : "-";
    };
    const anchorPriceText = Number.isFinite(anchorPrice) && anchorPrice > 0 ? formatNumber(anchorPrice, 3) : "-";
    return `
      <tr class="${isReplay ? "validation-replay-row" : ""}" data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
        <td class="num">${row.rank}</td>
        <td>${isReplay ? '<span class="tag warning">回放</span>' : '<span class="tag stable">真实</span>'}</td>
        <td class="validation-stock-cell">
          <span class="validation-stock-name">${escapeHtml(row.name || "-")}</span>
          <span class="validation-stock-code">${escapeHtml(row.code)}</span>
        </td>
        <td class="num">${anchorPriceText}</td>
        <td class="num ${numberClass(anchorChange)}">${pctText(anchorChange)}</td>
        <td>${escapeHtml(primaryLabel)}</td>
        <td class="num ${numberClass(primaryReturn)}">${pctText(primaryReturn)}</td>
        <td class="num">${tradeCost != null ? `${formatNumber(tradeCost, 2)}%` : "-"}</td>
        <td><span class="tag ${executionClass}">${escapeHtml(executionText)}</span></td>
        <td class="num ${numberClass(todayChange)}" data-validation-field="current_pct_chg">${pctText(todayChange)}</td>
        <td class="num ${numberClass(anchorToNow)}" data-validation-field="anchor_to_now_return">${pctText(anchorToNow)}</td>
      </tr>
    `;
  }).join("");
  bindSentimentRows(els.validationDetailBody);
}

function patchValidationQuoteColumns(rows) {
  const lookup = new Map((rows || []).map(row => [String(row.code || ""), row]));
  [...els.validationDetailBody.querySelectorAll("tr[data-code]")].forEach(tr => {
    const row = lookup.get(String(tr.dataset.code || ""));
    if (!row) return;
    updateValidationPctCell(tr.querySelector('[data-validation-field="current_pct_chg"]'), row.current_pct_chg);
    updateValidationPctCell(tr.querySelector('[data-validation-field="anchor_to_now_return"]'), row.anchor_to_now_return);
  });
}

function updateValidationPctCell(cell, value) {
  if (!cell) return;
  const num = Number(value);
  cell.className = `num ${numberClass(num)}`;
  cell.textContent = Number.isFinite(num) ? `${formatNumber(num, 2)}%` : "-";
}

function validationSkipReason(value) {
  const reason = String(value || "").trim();
  if (!reason) return "";
  if (reason === "unbuyable_limit_up") return "涨停不可买";
  if (reason === "excluded") return "执行剔除";
  return reason;
}

function primaryValidationLabel(row) {
  const strategy = row.strategy_name || "";
  if (strategy === "swing_picks") return "5日";
  return "次日";
}

function primaryValidationReturn(row) {
  const strategy = row.strategy_name || "";
  if (strategy === "swing_picks") {
    return row.signal_hold_5d_return ?? row.hold_5d_return ?? row.signal_hold_3d_return ?? row.hold_3d_return;
  }
  return row.signal_next_close_return ?? row.next_close_return;
}

function primaryValidationNetReturn(row) {
  const rawValue = primaryValidationReturn(row);
  if (rawValue === null || rawValue === undefined || rawValue === "") return null;
  const rawReturn = Number(rawValue);
  if (!Number.isFinite(rawReturn)) return null;
  const tradeCost = Number(row.trade_cost_pct);
  return Number.isFinite(tradeCost) ? rawReturn - tradeCost : rawReturn;
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

function validationSampleTypeBadge(row) {
  const real = Number(row.real_count || 0);
  const replay = Number(row.replay_count || 0);
  const count = Number(row.count || 0);
  if (row.sample_type === "empty" || (!count && !real && !replay)) {
    return `<span class="tag muted">空批次</span>`;
  }
  if (real > 0 && replay > 0) {
    return `<span class="tag validation">真${real}/回${replay}</span>`;
  }
  if (real > 0) {
    return `<span class="tag stable">真实</span>`;
  }
  return `<span class="tag warning">回放</span>`;
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

function bindSentimentRows(container) {
  [...container.querySelectorAll("tr")].forEach(row => {
    row.addEventListener("click", () => showSentiment(row.dataset.code, row.dataset.name));
  });
}

async function showSentiment(code, name) {
  if (!code) {
    return;
  }
  const localRow = findLocalRow(code) || { code, name };
  els.detailsPanel.hidden = false;
  els.detailsTitle.textContent = `${code} ${name || localRow.name || ""} 股票详情`;
  els.detailsSummary.innerHTML = stockProfileHtml(localRow);
  renderDetailRadar(localRow);
  els.newsList.innerHTML = '<div class="news-item"><p>舆情加载中...</p></div>';
  try {
    const params = new URLSearchParams({ name: name || localRow.name || "" });
    const res = await fetch(`/api/sentiment/${encodeURIComponent(code)}?${params.toString()}`);
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "舆情接口异常");
    }
    const sentiment = payload.sentiment || {};
    const triggers = (sentiment.trigger_words || []).join("、") || "无";
    const items = sentiment.items || [];
    const sentimentHtml = `
      <section class="drawer-section">
        <div class="section-label">舆情</div>
        <p>舆情分 ${escapeHtml(sentiment.score ?? "-")}；${escapeHtml(sentiment.summary || "-")}；关键词：${escapeHtml(triggers)}</p>
      </section>
    `;
    els.detailsSummary.innerHTML = stockProfileHtml(localRow) + sentimentHtml;
    if (!items.length) {
      els.newsList.innerHTML = '<div class="news-item"><p>暂无相关新闻。</p></div>';
      return;
    }
    els.newsList.innerHTML = items.map(item => `
      <article class="news-item">
        <h3>${escapeHtml(item.title || "-")}</h3>
        <p>${escapeHtml(item.content || "")}</p>
        <div class="news-meta">${escapeHtml(item.source || "-")} · ${escapeHtml(item.publish_time || "-")}</div>
      </article>
    `).join("");
  } catch (err) {
    els.newsList.innerHTML = `<div class="news-item"><p>舆情加载失败：${escapeHtml(err.message)}</p></div>`;
  }
}

function rerenderCurrentTables() {
  renderStrategyConsensus(state.lastRows.consensus);
  renderDecisionDesk(state.lastRows.consensus);
  renderShortTermTable(state.lastRows.shortTerm);
  if (state.tomorrowLoaded) {
    renderTomorrowTable(state.lastRows.tomorrow);
  }
  if (state.horizonLoaded) {
    renderSwingTable(state.lastRows.swing);
  }
}

function filterAndSortRows(rows, options = {}) {
  const actionFilter = els.actionFilterSelect.value;
  const sortMode = els.sortSelect.value;
  const filtered = (rows || []).filter(row => rowMatchesAction(row, actionFilter));
  return [...filtered].sort((left, right) => compareRows(left, right, sortMode, options));
}

function rowMatchesAction(row, actionFilter) {
  if (actionFilter === "all") {
    return true;
  }
  const label = rowActionLabel(row);
  if (actionFilter === "priority") {
    return isPriorityAction(label);
  }
  if (actionFilter === "watch") {
    return label.includes("观察") && !label.includes("只观察");
  }
  if (actionFilter === "wait") {
    return label.includes("等待");
  }
  if (actionFilter === "observe") {
    return label.includes("只观察");
  }
  return true;
}

function compareRows(left, right, sortMode, options = {}) {
  if (sortMode === "quality") {
    return rowQuality(right) - rowQuality(left);
  }
  if (sortMode === "risk") {
    return rowRisk(left) - rowRisk(right);
  }
  if (sortMode === "score") {
    return rowScore(right) - rowScore(left);
  }
  if (sortMode === "turnover") {
    return Number(right.turnover || 0) - Number(left.turnover || 0);
  }
  if (options.consensus) {
    return Number(left.best_rank || left.avg_rank || 999) - Number(right.best_rank || right.avg_rank || 999);
  }
  return Number(left.rank || 999) - Number(right.rank || 999);
}

function rowActionLabel(row) {
  return String(row.action_label || row.serenity_profile?.action_label || row.consensus_signal?.action_label || "");
}

function isPriorityAction(label) {
  return String(label || "").includes("优先");
}

function rowQuality(row) {
  return Number(row.serenity_profile?.quality_score ?? row.avg_quality ?? row.score ?? 0);
}

function rowRisk(row) {
  return Number(row.serenity_profile?.risk_score ?? row.avg_risk ?? 999);
}

function rowScore(row) {
  return Number(row.score ?? row.consensus_score ?? row.avg_score ?? 0);
}

function findLocalRow(code) {
  const normalized = String(code || "");
  const groups = [
    state.lastRows.consensus,
    state.lastRows.shortTerm,
    state.lastRows.tomorrow,
    state.lastRows.swing,
  ];
  const matches = groups
    .flat()
    .filter(row => String(row?.code || "") === normalized);
  if (!matches.length) {
    return null;
  }
  return matches.reduce((best, row) => mergeRowDetails(best, row), {});
}

function mergeRowDetails(base, row) {
  const result = { ...base, ...row };
  if (base.appearances && !result.consensus_signal) {
    result.consensus_signal = base;
  }
  if (base.consensus_signal && !result.consensus_signal) {
    result.consensus_signal = base.consensus_signal;
  }
  if (row.action_label && !result.consensus_signal) {
    result.consensus_signal = row;
  }
  if (base.serenity_profile && !result.serenity_profile) {
    result.serenity_profile = base.serenity_profile;
  }
  return result;
}

// C3：verdict 评级徽章。row.verdict 由后端 _verdict_tier 产出。
function verdictBadge(verdict) {
  if (!verdict || !verdict.tier) {
    return "";
  }
  const note = verdict.note
    ? `<span class="verdict-note">${escapeHtml(verdict.note)}</span>`
    : "";
  return `<span class="verdict verdict-${escapeHtml(verdict.tier)}">${escapeHtml(verdict.label || verdict.tier)}</span>${note}`;
}

// C3：表格综合分单元格 = verdict 徽章 + “综合分 / 买入安全”。
function scoreCell(row, entrySafetyValue = row.execution_score) {
  const score = scorePairValue(row.score, 1, "综合分");
  const entry = scorePairValue(entrySafetyValue, 0, executionScoreHint(entrySafetyValue), "买入安全 ");
  const separator = entry ? '<span class="score-pair-separator">/</span>' : "";
  const deepseek = row.deepseek_rank_score != null
    ? `<span class="score-pair-separator">/</span>${scorePairValue(row.deepseek_rank_score, 1, "DeepSeek复核后最终分", "DS ")}`
    : "";
  const number = `<div class="score-pair" title="综合分 / 买入安全 / DeepSeek复核分">${score}${separator}${entry}${deepseek}</div>`;
  const tier = row.verdict?.tier ? ` score-${escapeHtml(row.verdict.tier)}` : "";
  return `<td class="num score${tier}">${number}</td>`;
}

function scorePairValue(value, digits, title, label = "") {
  const num = Number(value);
  const prefix = escapeHtml(label);
  if (!Number.isFinite(num)) {
    return `<span class="score-pair-value score-grade-empty" title="${escapeHtml(title || "暂无数据")}">${prefix}-</span>`;
  }
  return `<span class="score-pair-value ${scoreGradeClass(num)}" title="${escapeHtml(title || "")}">${prefix}${formatNumber(num, digits)}</span>`;
}

// C3：多空双进度条。优先用行顶层 bull_score/bear_score（A2），回退委员会字段。
function bullBearBars(row) {
  const committee = row.agent_committee || {};
  const bull = Number(row.bull_score ?? committee.bull_researcher_score);
  const bear = Number(row.bear_score ?? committee.bear_researcher_score);
  if (!Number.isFinite(bull) && !Number.isFinite(bear)) {
    return "";
  }
  const b = Number.isFinite(bull) ? Math.max(0, Math.min(100, bull)) : 0;
  const r = Number.isFinite(bear) ? Math.max(0, Math.min(100, bear)) : 0;
  return `
    <div class="bull-bear">
      <div class="bb-row"><span>多</span><div class="bb-track"><div class="bb-fill bb-fill-bull" style="width:${b}%"></div></div><span class="bb-value">${b.toFixed(0)}</span></div>
      <div class="bb-row"><span>空</span><div class="bb-track"><div class="bb-fill bb-fill-bear" style="width:${r}%"></div></div><span class="bb-value">${r.toFixed(0)}</span></div>
    </div>
  `;
}

// C2-2：详情抽屉策略子分雷达图。
function renderDetailRadar(row) {
  const dims = [
    ["动量", row.momentum_score],
    ["趋势", row.trend_score],
    ["流动性", row.liquidity_score],
    ["买入安全", row.execution_score],
    ["主题", row.theme_score],
    ["不过热", row.not_overextended_score],
  ];
  const present = dims.filter(([, v]) => v != null && Number.isFinite(Number(v)));
  if (present.length < 3) {
    renderChart("detailRadar", {
      title: { text: "子分数据不足", left: "center", top: "middle", textStyle: { color: CHART_THEME.text, fontSize: 12 } },
    });
    return;
  }
  renderChart("detailRadar", {
    radar: {
      indicator: present.map(([name]) => ({ name, max: 100 })),
      radius: "62%",
      axisName: { fontSize: 10, color: CHART_THEME.text },
      splitLine: { lineStyle: { color: CHART_THEME.split } },
      splitArea: { areaStyle: { color: CHART_THEME.areaFill } },
    },
    series: [
      {
        type: "radar",
        data: [
          {
            value: present.map(([, v]) => Number(v)),
            areaStyle: { color: "rgba(79,140,247,0.18)" },
            lineStyle: { color: CHART_THEME.accent },
            itemStyle: { color: CHART_THEME.accent },
          },
        ],
      },
    ],
  });
}

function stockProfileHtml(row) {
  const profile = row.serenity_profile || {};
  const committee = row.agent_committee || {};
  const consensus = row.consensus_signal || (row.appearances ? row : null);
  const pctClass = Number(row.pct_chg || 0) >= 0 ? "positive" : "negative";
  const evidence = [
    ...(profile.evidence || []).map(item => item.label),
    ...(committee.bull_cases || []),
    ...(row.evidence || []),
    ...(row.reasons || []).slice(0, 3),
  ].filter(Boolean);
  const risks = [
    ...(profile.risk_reasons || []),
    ...(committee.bear_cases || []),
    ...(row.failure_reasons || []),
    ...(row.risk_words || []),
  ].filter(Boolean);
  const strategyTags = consensus
    ? (consensus.strategies || []).map(text => `<span class="tag stable">${escapeHtml(text)}</span>`).join("")
    : "";
  const evidenceTags = evidence.length
    ? evidence.slice(0, 6).map(text => `<span class="tag stable">${escapeHtml(text)}</span>`).join("")
    : '<span class="tag stable">暂无结构化证据</span>';
  const riskTags = risks.length
    ? risks.slice(0, 6).map(text => `<span class="tag risk">${escapeHtml(text)}</span>`).join("")
    : '<span class="tag stable">未发现高风险标签</span>';
  return `
    <section class="drawer-section">
      <div class="drawer-kpis">
        <div><span>最新价</span><strong>${formatNumber(row.price, 3)}</strong></div>
        <div><span>涨跌幅</span><strong class="${pctClass}">${row.pct_chg == null ? "-" : `${formatNumber(row.pct_chg, 2)}%`}</strong></div>
        <div><span>综合分</span><strong>${formatNumber(rowScore(row), 1)}</strong></div>
      </div>
      <div class="drawer-kpis compact">
        <div><span>质量</span><strong>${formatNumber(profile.quality_score ?? row.avg_quality, 1)}</strong></div>
        <div><span>风险</span><strong class="${riskNumberClass(profile.risk_score ?? row.avg_risk)}">${formatNumber(profile.risk_score ?? row.avg_risk, 1)}</strong></div>
        <div><span>置信</span><strong>${formatNumber(profile.confidence_score ?? row.avg_confidence, 1)}</strong></div>
      </div>
      <div class="drawer-kpis compact">
        <div><span>Agent委员会</span><strong>${formatNumber(committee.final_score ?? profile.agent_committee_score ?? row.avg_agent_score, 1)}</strong></div>
        <div><span>交易员</span><strong>${formatNumber(committee.trader_score, 1)}</strong></div>
        <div><span>风控</span><strong class="${riskNumberClass(committee.risk_manager_score)}">${formatNumber(committee.risk_manager_score, 1)}</strong></div>
      </div>
    </section>
    <section class="drawer-section">
      <div class="section-label">动作建议</div>
      <p>
        ${verdictBadge(row.verdict)}
        <span class="tag ${actionTagClass(rowActionLabel(row))}">${escapeHtml(rowActionLabel(row) || "等待确认")}</span>
        <span class="tag validation">Agent:${escapeHtml(committee.final_action_label || "未生成")}</span>
      </p>
      ${bullBearBars(row)}
    </section>
    <section class="drawer-section">
      <div class="section-label">共识来源</div>
      <p>${consensus ? `${escapeHtml(consensus.label || "共识")}，${escapeHtml(String(consensus.appearances || "-"))}/${escapeHtml(String(consensus.strategy_count || "-"))} 个策略入选` : "暂无多策略共识"}</p>
      <p>${strategyTags || '<span class="tag stable">暂无来源策略</span>'}</p>
    </section>
    <section class="drawer-section">
      <div class="section-label">证据</div>
      <p>${evidenceTags}</p>
    </section>
    <section class="drawer-section">
      <div class="section-label">风险</div>
      <p>${riskTags}</p>
    </section>
  `;
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

function formatPercent(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) {
    return "-";
  }
  return `${num.toFixed(2)}%`;
}

function numberClass(value) {
  const num = Number(value);
  if (!Number.isFinite(num) || num === 0) {
    return "";
  }
  return num >= 0 ? "positive" : "negative";
}

function riskNumberClass(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) {
    return "";
  }
  if (num >= 70) return "risk-text";
  if (num <= 45) return "safe-text";
  return "";
}

function actionTagClass(label) {
  if (!label) return "stable";
  if (label.includes("优先")) return "strategy";
  if (label.includes("只观察")) return "risk";
  if (label.includes("等待")) return "warning";
  return "validation";
}

function stabilityTag(row) {
  const status = row.stability_status === "new" ? "新进" : "留存";
  const cls = row.stability_status === "new" ? "tag new" : "tag stable";
  const streak = Number(row.streak || 1);
  return `<span class="${cls}">${status} ${streak}</span>`;
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

function closeGlossaryPopover() {
  if (!els.glossaryPopover || els.glossaryPopover.hidden) {
    return;
  }
  els.glossaryPopover.hidden = true;
  els.glossaryToggle?.setAttribute("aria-expanded", "false");
}

function toggleGlossaryPopover() {
  if (!els.glossaryPopover || !els.glossaryToggle) {
    return;
  }
  const nextOpen = els.glossaryPopover.hidden;
  els.glossaryPopover.hidden = !nextOpen;
  els.glossaryToggle.setAttribute("aria-expanded", String(nextOpen));
}

function activePoolFilter() {
  return document.querySelector(".pool-tab.active")?.dataset.poolFilter || "all";
}

function applyRecommendationPoolFilter(filter = activePoolFilter()) {
  els.poolTabs.forEach(tab => {
    tab.classList.toggle("active", tab.dataset.poolFilter === filter);
  });
  els.poolGroups.forEach(group => {
    const matched = filter === "all" || group.dataset.poolGroup === filter;
    group.hidden = !matched;
  });
}

function ensureRecommendationPoolData(options = {}) {
  const background = Boolean(options.background);
  const filter = activePoolFilter();
  if ((filter === "all" || filter === "next") && !state.tomorrowLoaded) {
    loadTomorrowPicks({ background });
  }
  if ((filter === "all" || filter === "swing") && !state.horizonLoaded) {
    loadHorizonPicks({ background });
  }
}

els.glossaryToggle?.addEventListener("click", (event) => {
  event.stopPropagation();
  toggleGlossaryPopover();
});
els.glossaryPopover?.addEventListener("click", (event) => {
  event.stopPropagation();
});
document.addEventListener("click", closeGlossaryPopover);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeGlossaryPopover();
  }
});

els.refreshButton.addEventListener("click", startRecommendationStreamWithSnapshot);
els.stockPredictionBtn.addEventListener("click", loadStockPrediction);
els.stockPredictionInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    loadStockPrediction();
  }
});
els.marketSelect.addEventListener("change", () => {
  state.tomorrowLoaded = false;
  state.horizonLoaded = false;
  if (document.getElementById("todayPanel")?.classList.contains("active")) {
    startRecommendationStreamWithSnapshot();
  } else {
    stopRecommendationStream();
  }
  applyRecommendationPoolFilter();
  if (document.getElementById("todayPanel")?.classList.contains("active")) {
    ensureRecommendationPoolData();
  }
});
els.actionFilterSelect.addEventListener("change", rerenderCurrentTables);
els.sortSelect.addEventListener("change", rerenderCurrentTables);
els.poolTabs.forEach(button => {
  button.addEventListener("click", () => {
    applyRecommendationPoolFilter(button.dataset.poolFilter || "all");
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
    } else {
      stopRecommendationStream();
    }
    if (button.dataset.tab === "validationPanel" && !state.validationLoaded) {
      loadValidation();
    }
    if (button.dataset.tab === "validationPanel") {
      startValidationAutoRefreshLoop();
    } else {
      stopValidationAutoRefreshLoop();
    }
    if (button.dataset.tab === "overviewPanel" && !state.overviewLoaded) {
      loadStrategyOverview();
    }
  });
});
els.validationStrategySelect?.addEventListener("change", () => {
  state.selectedValidation = { date: "", strategy: "" };
  state.validationAutoRefreshDate = "";
  state.validationDatePage = 0;
  renderTuningRun(null, currentValidationStrategy());
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
    renderTuningRun(null, strategy);
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
els.closeDetails.addEventListener("click", () => {
  els.detailsPanel.hidden = true;
});

applyRecommendationPoolFilter();
startRecommendationStreamWithSnapshot();
