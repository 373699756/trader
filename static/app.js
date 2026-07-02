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
    longTerm: [],
    consensus: [],
    tomorrow: [],
    tech: [],
    chokepoint: [],
    reversal: [],
    smallcap: [],
    breakout: [],
    swing: [],
    position: [],
  },
  tomorrowLoaded: false,
  techLoaded: false,
  chokepointLoaded: false,
  reversalLoaded: false,
  smallcapLoaded: false,
  breakoutLoaded: false,
  horizonLoaded: false,
  overviewLoaded: false,
  validationLoaded: false,
  portfolioLoaded: false,
  marketRegime: {},
  chokepointIndustryMap: [],
  chokepointMeta: {},
  chokepointActiveSegment: "all",
  selectedValidation: {
    date: "",
    strategy: "",
  },
  charts: {},
};

const VALIDATION_AUTO_REFRESH_MS = 30 * 60 * 1000;

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
  longTermBody: document.getElementById("longTermBody"),
  tomorrowBody: document.getElementById("tomorrowBody"),
  tomorrowStrategyVersion: document.getElementById("tomorrowStrategyVersion"),
  tomorrowDataStatus: document.getElementById("tomorrowDataStatus"),
  tomorrowCandidateCount: document.getElementById("tomorrowCandidateCount"),
  tomorrowBuyableFilter: document.getElementById("tomorrowBuyableFilter"),
  tomorrowValidationSamples: document.getElementById("tomorrowValidationSamples"),
  tomorrowValidationHit3: document.getElementById("tomorrowValidationHit3"),
  tomorrowPredictionCaution: document.getElementById("tomorrowPredictionCaution"),
  techBody: document.getElementById("techBody"),
  chokepointBody: document.getElementById("chokepointBody"),
  chokepointChainMap: document.getElementById("chokepointChainMap"),
  reversalBody: document.getElementById("reversalBody"),
  smallcapBody: document.getElementById("smallcapBody"),
  breakoutBody: document.getElementById("breakoutBody"),
  portfolioStrategySelect: document.getElementById("portfolioStrategySelect"),
  loadPortfolioBtn: document.getElementById("loadPortfolioBtn"),
  portfolioStatus: document.getElementById("portfolioStatus"),
  portfolioSummary: document.getElementById("portfolioSummary"),
  portfolioBody: document.getElementById("portfolioBody"),
  swingBody: document.getElementById("swingBody"),
  positionBody: document.getElementById("positionBody"),
  updateStatus: document.getElementById("updateStatus"),
  validationScoreboard: document.getElementById("validationScoreboard"),
  validationSimpleDecision: document.getElementById("validationSimpleDecision"),
  validationDaysSelect: document.getElementById("validationDaysSelect"),
  validationSelectionLabel: document.getElementById("validationSelectionLabel"),
  validationSampleCount: document.getElementById("validationSampleCount"),
  validationWinRate: document.getElementById("validationWinRate"),
  validationHit3: document.getElementById("validationHit3"),
  validationAvgReturn: document.getElementById("validationAvgReturn"),
  nextDayCompareGrid: document.getElementById("nextDayCompareGrid"),
  validationDatesBody: document.getElementById("validationDatesBody"),
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
  const longTerm = recommendations.long_term || [];
  const consensus = payload.meta?.strategy_consensus?.rows || [];
  const marketRegime = payload.meta?.market_regime || {};
  const shouldRenderTables = rememberFingerprint("recommendations", {
    shortTerm,
    longTerm,
    consensus,
    marketRegime,
  });
  state.lastRows.shortTerm = shortTerm;
  state.lastRows.longTerm = longTerm;
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
  if (state.techLoaded) {
    loadTechPotential({ background: true });
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
    top_n: "30",
    market: els.marketSelect.value,
  });
  try {
    const res = await fetch(`/api/recommendations?${params.toString()}`);
    const payload = await res.json();
    applyRecommendationsPayload(payload);
  } catch (err) {
    const message = `<tr><td colspan="16" class="empty">${escapeHtml(err.message)}</td></tr>`;
    els.shortTermBody.innerHTML = message;
    els.longTermBody.innerHTML = message;
    els.strategyConsensusBody.innerHTML = '<tr><td colspan="12" class="empty">加载失败</td></tr>';
    setStatus(`刷新失败：${err.message}`);
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
    top_n: "30",
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

async function loadStrategyOverview() {
  state.overviewLoaded = true;
  els.strategyOverviewGrid.innerHTML = '<div class="empty">加载中...</div>';
  els.strategyOverviewBody.innerHTML = '<tr><td colspan="12" class="empty">加载中...</td></tr>';
  try {
    const res = await fetch("/api/strategy-overview?days=20");
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "接口返回异常");
    }
    renderStrategyOverview(payload);
  } catch (err) {
    els.strategyOverviewGrid.innerHTML = `<div class="empty">${escapeHtml(err.message)}</div>`;
    els.strategyOverviewBody.innerHTML = `<tr><td colspan="12" class="empty">${escapeHtml(err.message)}</td></tr>`;
  }
}

async function loadValidation() {
  state.validationLoaded = true;
  els.validationDatesBody.innerHTML = '<tr><td colspan="4" class="empty">加载中...</td></tr>';
  const options = arguments[0] || {};
  const isSilent = Boolean(options.silent);
  const fromAutoRefresh = Boolean(options.fromAutoRefresh);
  const skipAutoOutcomeUpdate = Boolean(options.skipAutoOutcomeUpdate);
  const params = new URLSearchParams({
    strategy: "tomorrow_picks",
    days: els.validationDaysSelect.value,
  });
  try {
    const res = await fetch(`/api/strategy-validation?${params.toString()}`);
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "接口返回异常");
    }
    renderValidationMetrics(payload.metrics || {});
    renderValidationDates(payload.dates || []);
    syncValidationSelection(payload.dates || []);
    loadValidationOverview();
    if (!skipAutoOutcomeUpdate) {
      autoFillMissingValidationOutcomes(payload.metrics || {}, payload.dates || []);
    }
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
    els.validationDatesBody.innerHTML = `<tr><td colspan="4" class="empty">${escapeHtml(err.message)}</td></tr>`;
    setStatus(`策略验证加载失败：${err.message}`);
  }
}

function isValidationPanelActive() {
  const panel = document.getElementById("validationPanel");
  return Boolean(panel && panel.classList.contains("active"));
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
  const now = Date.now();
  if (state.validationAutoRefreshDate === latestDate && now - state.validationAutoRefreshAt < VALIDATION_AUTO_REFRESH_MS) {
    return;
  }
  state.validationAutoRefreshInFlight = true;
  state.validationAutoRefreshDate = latestDate;
  state.validationAutoRefreshAt = now;
  try {
    setOpsStatus(els.updateStatus, `检测到 ${latestDate} 无真实回填结果，已触发后台自动更新`, "pending");
    const params = new URLSearchParams({ date: latestDate });
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
      `已触发 ${latestDate} 回填，新增 ${result.updated || 0} 条，跳过 ${result.skipped || 0} 条`,
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

async function loadPortfolio() {
  state.portfolioLoaded = true;
  const strategy = els.portfolioStrategySelect.value || "tomorrow_picks";
  els.portfolioSummary.innerHTML = '<div class="empty">组合生成中...</div>';
  els.portfolioBody.innerHTML = '<tr><td colspan="7" class="empty">组合生成中...</td></tr>';
  setOpsStatus(els.portfolioStatus, "正在读取最近保存快照并执行仓位约束…", "pending");
  try {
    const params = new URLSearchParams({ strategy });
    const res = await fetch(`/api/portfolio?${params.toString()}`);
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "组合接口返回异常");
    }
    renderPortfolio(payload);
    const count = Number(payload.summary?.position_count || 0);
    if (!count) {
      setOpsStatus(els.portfolioStatus, payload.no_trade_reason || payload.empty_reason || "暂无可生成组合的保存快照。", "pending");
      return;
    }
    if (payload.summary?.constraints_feasible === false) {
      setOpsStatus(els.portfolioStatus, payload.no_trade_reason || "标的或主题分散度不足，剩余权重保留为现金，不强行顶破约束。", "pending");
      return;
    }
    setOpsStatus(els.portfolioStatus, `已按 ${strategyLabel(strategy)} 最近快照生成 ${count} 只组合。`, "ok");
  } catch (err) {
    els.portfolioSummary.innerHTML = `<div class="empty">${escapeHtml(err.message)}</div>`;
    els.portfolioBody.innerHTML = `<tr><td colspan="7" class="empty">${escapeHtml(err.message)}</td></tr>`;
    setOpsStatus(els.portfolioStatus, `组合生成失败：${err.message}`, "bad");
  }
}

// C2-4：各策略主周期方向命中率走势折线 + 顶部一眼结论记分牌。
async function loadValidationOverview() {
  try {
    const res = await fetch(`/api/validation-overview?days=${els.validationDaysSelect.value}`);
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
    els.validationScoreboard.innerHTML = '<div class="empty">暂无验证数据。后台会每30分钟自动保存明天预测并回填真实结果。</div>';
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
      ${renderPredictionHorizonCard(horizons.long, "long")}
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
      setOpsStatus(els.updateStatus, joinStatusText(["后台历史自动更新已关闭", snapshotText]), "pending");
      return;
    }
    if (status.running) {
      setOpsStatus(els.updateStatus, joinStatusText(["后台正在分批更新历史K线和验证结果…", snapshotText]), "pending");
      return;
    }
    const result = status.last_result || {};
    const totals = result.totals || {};
    if (status.last_error) {
      setOpsStatus(els.updateStatus, joinStatusText([`后台自动更新上次失败：${status.last_error}`, snapshotText]), "bad");
      return;
    }
    if (status.last_finished_at) {
      setOpsStatus(
        els.updateStatus,
        joinStatusText([
          `后台自动更新 ${status.last_finished_at} 完成：批次 ${totals.batches || 0}，更新 ${totals.updated || 0}，缓存 ${totals.cached || 0}，下载 ${totals.downloaded || 0}，失败 ${totals.failed || 0}`,
          snapshotText,
        ]),
        "ok"
      );
      return;
    }
    setOpsStatus(
      els.updateStatus,
      joinStatusText([
        `后台自动更新已启动：约 ${Math.round((config.initial_delay_seconds || 0) / 60)} 分钟后首轮，之后每 ${Math.round((config.interval_seconds || 0) / 60)} 分钟分批更新`,
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
    return "15:00 自动保存已关闭";
  }
  if (snapshot.running) {
    return "正在自动保存今天明天预测";
  }
  if (snapshot.last_error) {
    return `15:00 自动保存上次失败：${snapshot.last_error}`;
  }
  const saved = snapshot.last_result?.saved;
  if (saved?.signal_date) {
    return `已自动保存 ${saved.signal_date} 明天预测 ${saved.saved || 0} 条`;
  }
  if (snapshot.next_run_at) {
    return `下次自动保存 ${snapshot.next_run_at}`;
  }
  return "15:00 自动保存已启动";
}

async function loadValidationDaily(date, strategy) {
  state.selectedValidation = { date, strategy };
  renderValidationSelection();
  markSelectedValidationRow();
  els.validationDetailBody.innerHTML = '<tr><td colspan="7" class="empty">加载中...</td></tr>';
  const params = new URLSearchParams({ date, strategy });
  try {
    const res = await fetch(`/api/strategy-validation/daily?${params.toString()}`);
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "接口返回异常");
    }
    renderValidationDetail(payload.data || []);
  } catch (err) {
    els.validationDetailBody.innerHTML = `<tr><td colspan="7" class="empty">${escapeHtml(err.message)}</td></tr>`;
  }
}

async function loadTechPotential(options = {}) {
  state.techLoaded = true;
  const background = Boolean(options.background);
  if (!background || !hasRows(state.lastRows.tech)) {
    els.techBody.innerHTML = '<tr><td colspan="16" class="empty">加载中...</td></tr>';
  }
  const params = new URLSearchParams({
    top_n: "50",
    market: els.marketSelect.value,
  });
  try {
    const res = await fetch(`/api/tech-potential?${params.toString()}`);
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "接口返回异常");
    }
    const rows = payload.data || [];
    const shouldRender = rememberFingerprint("tech", rows);
    state.lastRows.tech = rows;
    renderMetrics({ health: payload.health, meta: payload.meta, market_sentiment: {} });
    if (shouldRender) {
      renderTechTable(state.lastRows.tech);
    }
    if (!background) {
      setStatus(`科技潜力榜更新时间 ${payload.meta.generated_at}`);
    }
  } catch (err) {
    if (!background || !hasRows(state.lastRows.tech)) {
      els.techBody.innerHTML = `<tr><td colspan="16" class="empty">${escapeHtml(err.message)}</td></tr>`;
    }
    if (!background) {
      setStatus(`科技潜力榜加载失败：${err.message}`);
    }
  }
}

async function loadTomorrowPicks(options = {}) {
  state.tomorrowLoaded = true;
  const background = Boolean(options.background);
  if (!background || !hasRows(state.lastRows.tomorrow)) {
    els.tomorrowBody.innerHTML = '<tr><td colspan="16" class="empty">加载中...</td></tr>';
  }
  const params = new URLSearchParams({
    top_n: "36",
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
      renderTomorrowPredictionStrip(payload);
      renderTomorrowTable(state.lastRows.tomorrow);
    }
    if (!background) {
      loadTomorrowValidationMetrics();
    }
    if (!background) {
      setStatus(`明天预测更新时间 ${payload.meta.generated_at || "最近快照"}`);
    }
  } catch (err) {
    if (!background || !hasRows(state.lastRows.tomorrow)) {
      els.tomorrowBody.innerHTML = `<tr><td colspan="16" class="empty">${escapeHtml(err.message)}</td></tr>`;
      resetTomorrowPredictionStrip(err.message);
    }
    if (!background) {
      setStatus(`明天预测加载失败：${err.message}`);
    }
  }
}

async function loadHorizonPicks(options = {}) {
  state.horizonLoaded = true;
  const background = Boolean(options.background);
  if (!background || !hasRows(state.lastRows.swing)) {
    els.swingBody.innerHTML = '<tr><td colspan="16" class="empty">加载中...</td></tr>';
  }
  if (!background || !hasRows(state.lastRows.position)) {
    els.positionBody.innerHTML = '<tr><td colspan="17" class="empty">加载中...</td></tr>';
  }
  const params = new URLSearchParams({
    top_n: "30",
    market: els.marketSelect.value,
  });
  try {
    const [swingRes, positionRes] = await Promise.all([
      fetch(`/api/swing-picks?${params.toString()}`),
      fetch(`/api/position-picks?${params.toString()}`),
    ]);
    const swingPayload = await swingRes.json();
    const positionPayload = await positionRes.json();
    if (!swingPayload.ok) {
      throw new Error(swingPayload.error || "波段接口返回异常");
    }
    if (!positionPayload.ok) {
      throw new Error(positionPayload.error || "中长期接口返回异常");
    }
    const swingRows = swingPayload.data || [];
    const positionRows = positionPayload.data || [];
    const shouldRenderSwing = rememberFingerprint("swing", swingRows);
    const shouldRenderPosition = rememberFingerprint("position", positionRows);
    state.lastRows.swing = swingRows;
    state.lastRows.position = positionRows;
    renderMetrics({ health: swingPayload.health, meta: swingPayload.meta, market_sentiment: {} });
    if (shouldRenderSwing) {
      renderSwingTable(state.lastRows.swing);
    }
    if (shouldRenderPosition) {
      renderPositionTable(state.lastRows.position);
    }
    if (!background) {
      setStatus(`波段/中长期更新时间 ${swingPayload.meta.generated_at}`);
    }
  } catch (err) {
    if (!background || !hasRows(state.lastRows.swing)) {
      els.swingBody.innerHTML = `<tr><td colspan="16" class="empty">${escapeHtml(err.message)}</td></tr>`;
    }
    if (!background || !hasRows(state.lastRows.position)) {
      els.positionBody.innerHTML = `<tr><td colspan="17" class="empty">${escapeHtml(err.message)}</td></tr>`;
    }
    if (!background) {
      setStatus(`波段/中长期加载失败：${err.message}`);
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
  els.overviewBestStrategy.textContent = best ? best.label : "暂无";
  els.overviewVerifiedCount.textContent = `${verifiedCount}/${strategies.length}`;
  els.overviewSampleCount.textContent = sampleCount;
  els.overviewDays.textContent = `近${payload.days || 20}个保存日`;
  renderOverviewRegime(Object.keys(marketRegime).length ? marketRegime : state.marketRegime);

  if (!strategies.length) {
    els.strategyOverviewGrid.innerHTML = '<div class="empty">暂无策略</div>';
    els.strategyOverviewBody.innerHTML = '<tr><td colspan="12" class="empty">暂无策略</td></tr>';
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

function renderPortfolio(payload) {
  const rows = payload.data || [];
  const summary = payload.summary || {};
  const perf = payload.performance?.metrics || {};
  const exposure = payload.exposure || {};
  const exposureTags = Object.entries(exposure).length
    ? Object.entries(exposure)
        .map(([theme, weight]) => `<span class="portfolio-chip">${escapeHtml(theme)} ${formatNumber(weight, 1)}%</span>`)
        .join("")
    : '<span class="portfolio-chip muted">暂无主题暴露</span>';
  els.portfolioSummary.innerHTML = `
    <div class="portfolio-summary-card">
      <span>策略</span>
      <strong>${escapeHtml(strategyLabel(payload.strategy))}</strong>
    </div>
    <div class="portfolio-summary-card">
      <span>持仓数</span>
      <strong>${summary.position_count ?? 0}</strong>
    </div>
    <div class="portfolio-summary-card">
      <span>总仓位</span>
      <strong>${formatNumber(summary.total_weight, 1)}%</strong>
    </div>
    <div class="portfolio-summary-card">
      <span>现金保留</span>
      <strong>${formatNumber(summary.cash_pct, 1)}%</strong>
    </div>
    <div class="portfolio-summary-card">
      <span>总仓上限</span>
      <strong>${formatNumber(summary.gross_exposure_pct, 1)}%</strong>
    </div>
    <div class="portfolio-summary-card">
      <span>市况系数</span>
      <strong>${formatNumber(summary.regime_factor, 2)}</strong>
    </div>
    <div class="portfolio-summary-card">
      <span>回撤系数</span>
      <strong>${formatNumber(summary.drawdown_factor, 2)}</strong>
    </div>
    <div class="portfolio-summary-card">
      <span>单票上限</span>
      <strong>${formatNumber(summary.single_cap_pct, 1)}%</strong>
    </div>
    <div class="portfolio-summary-card">
      <span>主题上限</span>
      <strong>${formatNumber(summary.theme_cap_pct, 1)}%</strong>
    </div>
    <div class="portfolio-summary-card">
      <span>纸面收益</span>
      <strong class="${numberClass(perf.total_return_pct)}">${formatNumber(perf.total_return_pct, 2)}%</strong>
    </div>
    <div class="portfolio-summary-card">
      <span>最大回撤</span>
      <strong class="${numberClass(perf.max_drawdown_pct)}">${formatNumber(perf.max_drawdown_pct, 2)}%</strong>
    </div>
    <div class="portfolio-summary-card">
      <span>净胜率</span>
      <strong>${formatNumber(perf.win_rate_pct, 1)}%</strong>
    </div>
    <div class="portfolio-summary-card">
      <span>纸面交易</span>
      <strong>${perf.closed_count ?? 0}/${perf.trade_count ?? 0}</strong>
    </div>
    <div class="portfolio-exposure">
      <span>主题暴露</span>
      <div>${exposureTags}</div>
      <small>${(summary.gross_reasons || []).map(text => escapeHtml(text)).join("；")}</small>
    </div>
  `;
  if (!rows.length) {
    els.portfolioBody.innerHTML = `<tr><td colspan="7" class="empty">${escapeHtml(payload.empty_reason || "暂无保存快照")}</td></tr>`;
    return;
  }
  els.portfolioBody.innerHTML = rows.map(row => {
    const profile = row.serenity_profile || {};
    const quality = profile.quality_score ?? row.avg_quality ?? row.score;
    const risk = profile.risk_score ?? row.avg_risk ?? row.risk_score;
    const action = rowActionLabel(row) || profile.action_label || "等待确认";
    return `
      <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
        <td class="num">${escapeHtml(row.code)}</td>
        <td>${escapeHtml(row.name)}</td>
        <td>${escapeHtml(row.portfolio_theme || row.theme || row.industry || "-")}</td>
        <td class="num">${formatNumber(quality, 1)}</td>
        <td class="num ${riskNumberClass(risk)}">${formatNumber(risk, 1)}</td>
        <td class="num score">${formatNumber(row.suggested_weight, 2)}%</td>
        <td><span class="tag ${actionTagClass(action)}">${escapeHtml(action)}</span></td>
      </tr>
    `;
  }).join("");
  bindSentimentRows(els.portfolioBody);
}

function renderOverviewRegime(regime) {
  els.overviewRegimeLabel.textContent = regime.label || "-";
  els.overviewRegimeScore.textContent = regime.score != null ? formatNumber(regime.score, 1) : "-";
  els.overviewRegimeBreadth.textContent = regime.breadth_pct != null ? `${formatNumber(regime.breadth_pct, 1)}%` : "-";
  els.overviewRegimeMedian.textContent = regime.median_pct_chg != null ? `${formatNumber(regime.median_pct_chg, 2)}%` : "-";
  els.overviewRegimeStrong.textContent = regime.strong_pct != null ? `${formatNumber(regime.strong_pct, 1)}%` : "-";
  els.overviewRegimeAdvice.textContent = regime.advice || "-";
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
  const displayRows = filterAndSortRows(rows, { consensus: true });
  if (!displayRows.length) {
    els.strategyConsensusBody.innerHTML = '<tr><td colspan="12" class="empty">暂无 2 策略以上共识标的</td></tr>';
    return;
  }
  els.strategyConsensusBody.innerHTML = displayRows.map(row => `
    <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
      <td class="num">${escapeHtml(row.code)}</td>
      <td>${escapeHtml(row.name)}</td>
      <td>${escapeHtml(row.market_label || "-")}</td>
      <td><span class="tag ${row.level === "high" ? "strategy" : row.level === "medium" ? "validation" : "stable"}">${escapeHtml(row.label || "-")}</span></td>
      <td class="num">${row.appearances}/${row.strategy_count}</td>
      <td class="num">${formatNumber(row.avg_rank, 2)}</td>
      <td class="num">${formatNumber(row.avg_score, 1)}</td>
      <td class="num">${formatNumber(row.avg_quality, 1)}</td>
      <td class="num ${riskNumberClass(row.avg_risk)}">${formatNumber(row.avg_risk, 1)}</td>
      <td class="num score">${formatNumber(row.consensus_score, 1)}</td>
      <td><span class="tag ${actionTagClass(row.action_label)}">${escapeHtml(row.action_label || "-")}</span></td>
      <td class="reasons">${(row.strategies || []).map(text => `<span class="tag stable">${escapeHtml(text)}</span>`).join("")}</td>
    </tr>
  `).join("");
  bindSentimentRows(els.strategyConsensusBody);
  renderConsensusScatter(displayRows);
}

// C2-3：共识热度散点。x=出现次数, y=共识分, 气泡大小=一致性(agreement)。
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
        `${escapeHtml(p.data.name)}<br/>出现 ${p.value[0]} 次<br/>共识分 ${p.value[1].toFixed(1)}<br/>一致性 ${(p.value[2] * 100).toFixed(0)}%`,
    },
    xAxis: { name: "出现次数", min: 0, minInterval: 1, axisLine: { lineStyle: { color: CHART_THEME.axis } } },
    yAxis: { name: "共识分", min: 0, max: 100, axisLine: { lineStyle: { color: CHART_THEME.axis } } },
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
  const riskValues = consensusRows
    .map(row => Number(row.avg_risk))
    .filter(Number.isFinite);
  const avgRisk = riskValues.length
    ? riskValues.reduce((sum, value) => sum + value, 0) / riskValues.length
    : null;
  els.decisionConsensusCount.textContent = consensusRows.length;
  els.decisionPriorityCount.textContent = priorityRows.length;
  els.decisionAvgRisk.textContent = avgRisk == null ? "-" : formatNumber(avgRisk, 1);
}

function renderTomorrowPredictionStrip(payload) {
  const health = payload.health || {};
  const meta = payload.meta || {};
  const policy = meta.policy || {};
  const dataStatus = meta.fallback === "saved_snapshot" ? "保存快照" : (health.quotes_source || "实时行情");
  const minTurnover = policy.min_turnover != null ? formatMoney(policy.min_turnover) : "-";
  els.tomorrowStrategyVersion.textContent = meta.strategy_version || "tomorrow_picks_v4";
  els.tomorrowDataStatus.textContent = dataStatus;
  const displayCount = meta.display_count ?? (payload.data || []).length;
  const displayLimit = meta.display_limit ?? meta.top_n ?? "-";
  els.tomorrowCandidateCount.textContent = `${meta.screened_count ?? meta.candidate_count ?? "-"} / 展示${displayCount}`;
  els.tomorrowBuyableFilter.textContent = `最多${displayLimit}支，最低分≥${formatNumber(meta.min_score, 1)}；成交额≥${minTurnover}`;
  if (els.tomorrowPredictionCaution) {
    const primaryCount = meta.primary_watch_count ?? Math.min(5, displayCount || 0);
    const backupCount = meta.backup_watch_count ?? Math.max(0, (displayCount || 0) - primaryCount);
    const tierText = primaryCount > 0
      ? `重点观察 ${primaryCount} 支，备选观察 ${backupCount} 支`
      : `暂无重点观察，展示 ${backupCount || displayCount || 0} 支备选观察`;
    els.tomorrowPredictionCaution.textContent = `${meta.score_note || "综合分不是上涨概率。"} ${meta.gate_reason || ""} ${tierText}；不保证上涨。`;
  }
  els.tomorrowValidationSamples.textContent = "读取中";
  els.tomorrowValidationHit3.textContent = "读取中";
}

function resetTomorrowPredictionStrip(message) {
  els.tomorrowStrategyVersion.textContent = "-";
  els.tomorrowDataStatus.textContent = message || "-";
  els.tomorrowCandidateCount.textContent = "-";
  els.tomorrowBuyableFilter.textContent = "-";
  els.tomorrowValidationSamples.textContent = "-";
  els.tomorrowValidationHit3.textContent = "-";
  if (els.tomorrowPredictionCaution) {
    els.tomorrowPredictionCaution.textContent = "观察池不保证上涨；真实前瞻样本不足时只观察，不作为重仓依据。";
  }
}

async function loadTomorrowValidationMetrics() {
  try {
    const res = await fetch("/api/strategy-validation?strategy=tomorrow_picks&days=20");
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "接口返回异常");
    }
    const metrics = payload.metrics || {};
    els.tomorrowValidationSamples.textContent = metrics.sample_count ?? "0";
    els.tomorrowValidationHit3.textContent = metrics.win_rate_primary_net != null ? `${formatNumber(metrics.win_rate_primary_net, 1)}%` : "-";
  } catch (err) {
    els.tomorrowValidationSamples.textContent = "-";
    els.tomorrowValidationHit3.textContent = "-";
  }
}

function explanationTags(row) {
  const tags = [];
  const strategy = row.strategy_label || strategyLabel(row.strategy_name) || "-";
  const signal = row.signal_label ? ` / ${row.signal_label}` : "";
  tags.push(`<span class="tag strategy">策略:${escapeHtml(strategy)}${escapeHtml(signal)}</span>`);
  if (row.consensus_signal) {
    const consensus = row.consensus_signal;
    tags.push(
      `<span class="tag validation">共识:${escapeHtml(consensus.label || "-")} ${escapeHtml(String(consensus.appearances || 0))}/${escapeHtml(String(consensus.strategy_count || 0))}</span>`
    );
  }
  const profile = row.serenity_profile || {};
  const quality = profile.quality_score ?? row.avg_quality ?? row.score;
  const risk = profile.risk_score ?? row.avg_risk;
  const confidence = profile.confidence_score ?? row.avg_confidence;
  const coverage = profile.data_coverage;
  const verdict = row.verdict || {};
  const committee = row.agent_committee || {};
  const action = rowActionLabel(row) || profile.action_label || "-";
  if (verdict.label) {
    tags.push(`<span class="tag validation">评级:${escapeHtml(verdict.label)}</span>`);
  }
  if (quality != null) {
    tags.push(`<span class="tag ${actionTagClass(action)}">动作:${escapeHtml(action)} 质量${formatNumber(quality, 1)}</span>`);
  }
  if (confidence != null) {
    tags.push(`<span class="tag stable">置信:${formatNumber(confidence, 1)}</span>`);
  }
  if (coverage != null) {
    tags.push(`<span class="tag validation">覆盖:${formatNumber(Number(coverage) * 100, 0)}%</span>`);
  }
  if (verdict.note) {
    tags.push(`<span class="tag warning">降级:${escapeHtml(verdict.note)}</span>`);
  }
  if (committee.final_action_label) {
    tags.push(`<span class="tag validation">Agent:${escapeHtml(committee.final_action_label)}</span>`);
  }
  tags.push(similarSignalStatsTag(row.similar_signal_stats));
  (profile.evidence || []).slice(0, 2).forEach(item => {
    tags.push(`<span class="tag stable">证据:${escapeHtml(item.label || "-")}</span>`);
  });
  if (risk != null) {
    const level = Number(risk) >= 70 ? "高" : Number(risk) >= 50 ? "中" : "低";
    const cls = Number(risk) >= 70 ? "risk" : Number(risk) >= 50 ? "warning" : "stable";
    tags.push(`<span class="tag ${cls}">风险:${level}</span>`);
  } else if (row.chase_risk || row.overextension) {
    tags.push(riskTag("风险", row.chase_risk || row.overextension));
  }
  (row.reasons || []).slice(0, 3).forEach(text => {
    tags.push(`<span class="tag">推荐:${escapeHtml(text)}</span>`);
  });
  (profile.risk_reasons || []).slice(0, 2).forEach(text => {
    tags.push(`<span class="tag risk">风控:${escapeHtml(text)}</span>`);
  });
  tags.push(riskTag("追高", row.chase_risk));
  tags.push(riskTag("透支", row.overextension));
  (row.failure_reasons || []).slice(0, 3).forEach(text => {
    tags.push(`<span class="tag risk">失败:${escapeHtml(text)}</span>`);
  });
  (row.risk_words || []).slice(0, 2).forEach(text => {
    tags.push(`<span class="tag risk">舆情:${escapeHtml(text)}</span>`);
  });
  return tags.filter(Boolean).join("");
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
  return row.industry || "行业未知";
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
        <td>${escapeHtml(row.industry || "-")}</td>
        <td class="num">${formatNumber(row.momentum_score, 1)}</td>
        <td class="num">${formatNumber(row.sentiment_score, 1)}</td>
        ${scoreCell(row)}
        <td class="reasons">${explanation}</td>
      </tr>
    `;
  }).join("");

  bindSentimentRows(els.shortTermBody);
}

function renderLongTermTable(rows) {
  const displayRows = filterAndSortRows(rows);
  if (!displayRows.length) {
    els.longTermBody.innerHTML = '<tr><td colspan="16" class="empty">暂无符合条件的股票</td></tr>';
    return;
  }
  els.longTermBody.innerHTML = displayRows.map(row => {
    const pctClass = row.pct_chg >= 0 ? "positive" : "negative";
    const sixtyClass = row.sixty_day_pct >= 0 ? "positive" : "negative";
    const ytdClass = row.ytd_pct >= 0 ? "positive" : "negative";
    const explanation = `${stabilityTag(row)}${explanationTags(row)}`;
    return `
      <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
        <td class="num">${row.rank}</td>
        ${codeCell(row)}
        <td>${escapeHtml(row.name)}</td>
        <td>${escapeHtml(row.market_label)}</td>
        <td class="num">${formatNumber(row.price, 3)}</td>
        <td class="num ${pctClass}">${formatNumber(row.pct_chg, 2)}%</td>
        <td class="num ${sixtyClass}">${formatNumber(row.sixty_day_pct, 2)}%</td>
        <td class="num ${ytdClass}">${formatNumber(row.ytd_pct, 2)}%</td>
        <td class="num">${formatNumber(row.turnover_rate, 2)}%</td>
        <td class="num">${formatMoney(row.turnover)}</td>
        ${marketCapCell(row)}
        <td>${escapeHtml(row.industry || "-")}</td>
        <td class="num">${formatNumber(row.trend_score, 1)}</td>
        <td class="num">${formatNumber(row.sentiment_score, 1)}</td>
        ${scoreCell(row)}
        <td class="reasons">${explanation}</td>
      </tr>
    `;
  }).join("");

  bindSentimentRows(els.longTermBody);
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

function renderTechTable(rows) {
  renderStandardStrategyTable(rows, els.techBody, "暂无符合条件的科技潜力股票", {
    liquidity: ["liquidity_score"],
    momentum: ["theme_score", "chokepoint_score"],
    trend: ["early_trend_score", "not_overextended_score"],
    execution: ["execution_score"],
  });
}

function renderStandardStrategyTable(rows, bodyEl, emptyMessage, scoreKeys = {}) {
  const displayRows = filterAndSortRows(rows);
  if (!displayRows.length) {
    bodyEl.innerHTML = `<tr><td colspan="16" class="empty">${escapeHtml(emptyMessage)}</td></tr>`;
    return;
  }
  bodyEl.innerHTML = displayRows.map((row, index) => standardStrategyRow(row, index, scoreKeys)).join("");
  bindSentimentRows(bodyEl);
}

function standardStrategyRow(row, index, scoreKeys = {}) {
  const pctClass = row.pct_chg >= 0 ? "positive" : "negative";
  const sixtyClass = row.sixty_day_pct >= 0 ? "positive" : "negative";
  const executionKeys = scoreKeys.execution || ["execution_score"];
  const executionValue = strategyScoreValue(row, executionKeys);
  return `
    <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
      <td class="num">${row.rank || index + 1}</td>
      ${codeCell(row)}
      <td>${escapeHtml(row.name)}</td>
      <td>${escapeHtml(row.market_label || "-")}</td>
      <td class="num">${formatNumber(row.price, 3)}</td>
      <td class="num ${pctClass}">${formatNumber(row.pct_chg, 2)}%</td>
      <td class="num">${formatNumber(row.volume_ratio, 2)}</td>
      <td class="num">${formatNumber(row.turnover_rate, 2)}%</td>
      <td class="num">${formatMoney(row.turnover)}</td>
      ${marketCapCell(row)}
      <td class="num ${sixtyClass}">${formatNumber(row.sixty_day_pct, 2)}%</td>
      <td class="num">${formatStrategyScore(row, scoreKeys.liquidity || ["liquidity_score"])}</td>
      <td class="num">${formatStrategyScore(row, scoreKeys.momentum || ["momentum_score"])}</td>
      <td class="num">${formatStrategyScore(row, scoreKeys.trend || ["trend_score"])}</td>
      ${scoreCell(row, executionValue)}
      <td class="reasons">${explanationTags(row)}</td>
    </tr>`;
}

function formatStrategyScore(row, keys) {
  const value = strategyScoreValue(row, keys);
  return value === undefined ? "-" : formatNumber(value, 1);
}

function strategyScoreValue(row, keys) {
  for (const key of keys || []) {
    if (row[key] === undefined || row[key] === null || row[key] === "") continue;
    return row[key];
  }
  return undefined;
}

function scoreGradeClass(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "score-grade-empty";
  if (num >= 80) return "score-grade-high";
  if (num >= 70) return "score-grade-mid";
  if (num >= 55) return "score-grade-low";
  return "score-grade-bad";
}

function executionScoreHint(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "买入安全：暂无数据";
  if (num >= 80) return "买入安全高：涨幅未明显过热，当前追高风险较低";
  if (num >= 70) return "买入安全中：仍可观察，但不要追高";
  if (num >= 55) return "买入安全偏低：涨幅靠近风控区，谨慎";
  return "买入安全低：当前不适合追买或缺少上涨确认";
}

async function loadChokepointPicks() {
  state.chokepointLoaded = true;
  els.chokepointBody.innerHTML = '<tr><td colspan="16" class="empty">加载中...</td></tr>';
  els.chokepointChainMap.innerHTML = '<div class="empty">加载中...</div>';
  const params = new URLSearchParams({ top_n: "30", market: els.marketSelect.value });
  try {
    const res = await fetch(`/api/chokepoint-picks?${params.toString()}`);
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "接口返回异常");
    }
    state.lastRows.chokepoint = payload.data || [];
    state.chokepointIndustryMap = payload.meta?.industry_map || [];
    state.chokepointMeta = payload.meta || {};
    renderMetrics({ health: payload.health, meta: payload.meta, market_sentiment: {} });
    renderChainMap(state.chokepointIndustryMap, payload.meta || {});
    renderChokepointTable(state.lastRows.chokepoint, payload.meta || {});
    setStatus(`卡脖子榜更新时间 ${payload.meta.generated_at}`);
  } catch (err) {
    els.chokepointBody.innerHTML = `<tr><td colspan="16" class="empty">${escapeHtml(err.message)}</td></tr>`;
    els.chokepointChainMap.innerHTML = `<div class="empty">${escapeHtml(err.message)}</div>`;
    setStatus(`卡脖子榜加载失败：${err.message}`);
  }
}

// 产业链/行业目录：固定展示关键上游环节和代表龙头，点击行业 Tab 联动下方榜单。
function renderChainMap(industryMap, meta = {}) {
  const groups = industryMap || [];
  if (!groups.length) {
    els.chokepointChainMap.innerHTML = `<div class="empty">${escapeHtml(meta.empty_reason || "暂无行业目录数据")}</div>`;
    return;
  }
  if (
    state.chokepointActiveSegment !== "all" &&
    !groups.some(node => node.segment === state.chokepointActiveSegment)
  ) {
    state.chokepointActiveSegment = "all";
  }

  const totals = groups[0]?.totals || {};
  const totalLeaders = Number(totals.unique_leader_count ?? groups.reduce((sum, node) => sum + Number(node.leader_count || 0), 0));
  const totalIndustries = Number(totals.industry_count ?? groups.length);
  const totalRecommended = Number(totals.recommended_count ?? groups.reduce((sum, node) => sum + Number(node.recommended_count || 0), 0));
  const totalMatched = Number(totals.matched_count ?? 0);
  const totalQuoted = Number(totals.quote_available_count ?? 0);
  const active = state.chokepointActiveSegment || "all";
  const selectedGroups = active === "all"
    ? groups
    : groups.filter(node => node.segment === active);
  const rawLeaderRows = selectedGroups
    .flatMap(node => (node.leaders || []).map(leader => ({ ...leader, chain_segment: node.segment })))
    .sort(compareChokepointLeaders);
  const leaderRows = active === "all" ? uniqueChokepointLeaders(rawLeaderRows) : rawLeaderRows;
  const matchedCount = active === "all" ? totalMatched : leaderRows.filter(row => row.matched).length;
  const quoteCount = active === "all" ? totalQuoted : leaderRows.filter(row => row.quote_available).length;
  const buyWatchCount = active === "all" ? totalRecommended : leaderRows.filter(row => ["buy", "watch"].includes(row.recommendation?.level)).length;
  const selectedTitle = active === "all" ? "全部卡脖子行业" : active;
  const note = active === "all"
    ? (meta.empty_reason || "点击行业 Tab 查看该环节龙头；龙头股只代表产业地位，不等于买入建议。")
    : "列表优先展示命中策略或可观察标的；未命中会直接给出不推荐或仅观察原因。";
  const tabs = [
    `
      <button class="industry-tab ${active === "all" ? "is-active" : ""}" type="button" data-segment="all">
        <span>全部</span>
        <strong>${totalLeaders}</strong>
        <small>${totalIndustries}行业 · ${totalRecommended}可买/观察</small>
      </button>
    `,
    ...groups.map(node => `
      <button class="industry-tab ${active === node.segment ? "is-active" : ""}" type="button" data-segment="${escapeHtml(node.segment)}">
        <span>${escapeHtml(node.segment)}</span>
        <strong>${node.leader_count || 0}</strong>
        <small>${node.recommended_count || 0}/${node.leader_count || 0}</small>
      </button>
    `),
  ].join("");
  const leaders = leaderRows.length
    ? leaderRows.map(renderChokepointLeaderRow).join("")
    : `<div class="empty">该行业暂无龙头目录</div>`;

  els.chokepointChainMap.innerHTML = `
    <div class="industry-browser">
      <div class="industry-tabs" role="tablist" aria-label="卡脖子行业切换">${tabs}</div>
      <div class="industry-panel">
        <div class="industry-panel-head">
          <div>
            <h3>${escapeHtml(selectedTitle)}</h3>
            <p>${escapeHtml(note)}</p>
          </div>
          <div class="industry-stats">
            <div><span>龙头股</span><strong>${leaderRows.length}</strong></div>
            <div><span>可买/观察</span><strong>${buyWatchCount}</strong></div>
            <div><span>命中策略</span><strong>${matchedCount}</strong></div>
            <div><span>有行情</span><strong>${quoteCount}</strong></div>
          </div>
        </div>
        <div class="leader-list">
          <div class="leader-list-head">
            <span>行业/股票</span>
            <span>实时状态</span>
            <span>系统动作</span>
            <span>原因</span>
          </div>
          ${leaders}
        </div>
      </div>
    </div>`;
  bindChokepointIndustryMap();
}

function uniqueChokepointLeaders(rows) {
  const byCode = new Map();
  (rows || []).forEach(row => {
    const key = String(row.code || "");
    const current = byCode.get(key);
    if (!current) {
      byCode.set(key, { ...row, segments: [row.chain_segment || row.segment].filter(Boolean) });
      return;
    }
    const mergedSegments = new Set([...(current.segments || []), row.chain_segment || row.segment].filter(Boolean));
    const better = compareChokepointLeaders(row, current) < 0 ? row : current;
    byCode.set(key, {
      ...current,
      ...better,
      chain_segment: [...mergedSegments].join(" / "),
      segments: [...mergedSegments],
    });
  });
  return [...byCode.values()].sort(compareChokepointLeaders);
}

function bindChokepointIndustryMap() {
  [...els.chokepointChainMap.querySelectorAll(".industry-tab")].forEach(node => {
    node.addEventListener("click", () => {
      state.chokepointActiveSegment = node.dataset.segment || "all";
      renderChainMap(state.chokepointIndustryMap, state.chokepointMeta || {});
      renderChokepointTable(state.lastRows.chokepoint, state.chokepointMeta || {});
    });
  });
  [...els.chokepointChainMap.querySelectorAll(".leader-row")].forEach(item => {
    item.addEventListener("click", event => {
      event.stopPropagation();
      showSentiment(item.dataset.code, item.dataset.name);
    });
  });
}

function renderChokepointLeaderRow(row) {
  const rec = row.recommendation || {};
  const pctClass = Number(row.pct_chg || 0) >= 0 ? "positive" : "negative";
  const matched = row.matched ? "命中策略" : row.quote_available ? "未命中策略" : "无行情";
  const status = row.quote_available
    ? `${formatNumber(row.price, 3)} / <span class="${pctClass}">${formatNumber(row.pct_chg, 2)}%</span>`
    : "无行情";
  return `
    <div class="leader-row" data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
      <div class="leader-main">
        <span class="leader-segment">${escapeHtml(row.chain_segment || row.segment || "-")}</span>
        <strong>${escapeHtml(row.name)}</strong>
        <small>${escapeHtml(row.code)}</small>
      </div>
      <div class="leader-market">
        <span>${status}</span>
        <small>${row.quote_available ? formatMoney(row.turnover) : "-"}</small>
      </div>
      <div class="leader-action">
        <span class="tag ${row.matched ? "strategy" : "stable"}">${escapeHtml(matched)}</span>
        <span class="tag recommend-${escapeHtml(rec.level || "unknown")}">${escapeHtml(rec.label || "无法判断")}</span>
      </div>
      <div class="leader-reason">${escapeHtml(rec.reason || (row.reasons || []).join("；") || "-")}</div>
    </div>`;
}

function compareChokepointLeaders(left, right) {
  const levelRank = { buy: 0, watch: 1, observe: 2, avoid: 3, unknown: 4 };
  const leftLevel = levelRank[left.recommendation?.level || "unknown"] ?? 9;
  const rightLevel = levelRank[right.recommendation?.level || "unknown"] ?? 9;
  if (leftLevel !== rightLevel) return leftLevel - rightLevel;
  if (Boolean(left.matched) !== Boolean(right.matched)) return left.matched ? -1 : 1;
  if (Boolean(left.quote_available) !== Boolean(right.quote_available)) return left.quote_available ? -1 : 1;
  return Number(right.score || 0) - Number(left.score || 0);
}

function renderChokepointTable(rows, meta = {}) {
  const activeSegment = state.chokepointActiveSegment || "all";
  const scopedRows = activeSegment === "all"
    ? rows
    : (rows || []).filter(row => row.chain_segment === activeSegment);
  const displayRows = filterAndSortRows(scopedRows);
  if (!displayRows.length) {
    const fallbackRows = chokepointLeaderFallbackRows(activeSegment);
    if (fallbackRows.length) {
      renderChokepointLeaderFallback(fallbackRows, activeSegment, meta);
      return;
    }
    const scopeText = activeSegment === "all" ? "" : `「${escapeHtml(activeSegment)}」`;
    const reason = meta.empty_reason || "该环节当前没有股票同时满足上游关键词、流动性和风险过滤条件。";
    els.chokepointBody.innerHTML = `<tr><td colspan="16" class="empty">${scopeText}暂无命中卡脖子策略的股票。${escapeHtml(reason)}</td></tr>`;
    return;
  }
  els.chokepointBody.innerHTML = displayRows.map((row, index) => standardStrategyRow(row, index, {
    liquidity: ["liquidity_score"],
    momentum: ["chokepoint_score"],
    trend: ["early_trend_score", "not_overextended_score"],
    execution: ["execution_score"],
  })).join("");
  bindSentimentRows(els.chokepointBody);
}

function chokepointLeaderFallbackRows(activeSegment) {
  const groups = state.chokepointIndustryMap || [];
  const selected = activeSegment === "all"
    ? groups
    : groups.filter(node => node.segment === activeSegment);
  return selected.flatMap(node => (node.leaders || []).map(leader => ({ ...leader, chain_segment: node.segment })));
}

function renderChokepointLeaderFallback(rows, activeSegment, meta = {}) {
  const title = activeSegment === "all" ? "行业龙头观察榜" : `「${escapeHtml(activeSegment)}」龙头观察榜`;
  const reason = meta.empty_reason || "当前没有股票命中卡脖子策略，以下为行业龙头实时状态，不等同于推荐买入。";
  els.chokepointBody.innerHTML = `
    <tr>
      <td colspan="16" class="empty">${title}：${escapeHtml(reason)}</td>
    </tr>
	    ${rows.map((row, index) => {
	      const rec = row.recommendation || {};
	      const pctClass = Number(row.pct_chg || 0) >= 0 ? "positive" : "negative";
	      const matched = row.matched ? "命中策略" : row.quote_available ? "未命中策略" : "无行情";
	      return `
	        <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
	          <td class="num">${index + 1}</td>
	          ${codeCell(row)}
	          <td>${escapeHtml(row.name)}</td>
	          <td>${escapeHtml(row.market_label || "-")}</td>
	          <td class="num">${row.quote_available ? formatNumber(row.price, 3) : "-"}</td>
	          <td class="num ${pctClass}">${row.quote_available ? `${formatNumber(row.pct_chg, 2)}%` : "-"}</td>
	          <td class="num">${row.quote_available ? formatNumber(row.volume_ratio, 2) : "-"}</td>
	          <td class="num">${row.quote_available ? `${formatNumber(row.turnover_rate, 2)}%` : "-"}</td>
	          <td class="num">${row.quote_available ? formatMoney(row.turnover) : "-"}</td>
	          ${marketCapCell(row)}
	          <td class="num">${row.quote_available ? `${formatNumber(row.sixty_day_pct, 2)}%` : "-"}</td>
	          <td class="num">-</td>
	          <td class="num">-</td>
	          <td class="num">-</td>
	          <td class="num">-</td>
	          <td><span class="tag recommend-${escapeHtml(rec.level || "unknown")}">${escapeHtml(rec.label || "无法判断")}</span></td>
	          <td class="reasons">
	            <span class="tag stable">${escapeHtml(matched)}</span>
	            <span class="tag stable">${escapeHtml(row.chain_segment || row.segment || "-")}</span>
	            <span class="tag recommend-${escapeHtml(rec.level || "unknown")}">${escapeHtml(rec.reason || "-")}</span>
	          </td>
	        </tr>`;
    }).join("")}
  `;
  bindSentimentRows(els.chokepointBody);
}

// ===== 新增盈利策略页（反转低波 / 小市值价值 / 量价突破） =====
function makeFactorLoader(stateKey, loadedKey, endpoint, topN, bodyEl, renderFn, label, colSpan = 16) {
  return async function () {
    state[loadedKey] = true;
    bodyEl().innerHTML = `<tr><td colspan="${colSpan}" class="empty">加载中...</td></tr>`;
    const params = new URLSearchParams({ top_n: String(topN), market: els.marketSelect.value });
    try {
      const res = await fetch(`${endpoint}?${params.toString()}`);
      const payload = await res.json();
      if (!payload.ok) throw new Error(payload.error || "接口返回异常");
      state.lastRows[stateKey] = payload.data || [];
      renderMetrics({ health: payload.health, meta: payload.meta, market_sentiment: {} });
      renderFn(state.lastRows[stateKey], payload.meta || {});
      setStatus(`${label}更新时间 ${payload.meta.generated_at}`);
    } catch (err) {
      bodyEl().innerHTML = `<tr><td colspan="${colSpan}" class="empty">${escapeHtml(err.message)}</td></tr>`;
      setStatus(`${label}加载失败：${err.message}`);
    }
  };
}

const loadReversalPicks = makeFactorLoader("reversal", "reversalLoaded", "/api/reversal-picks", 30, () => els.reversalBody, renderReversalTable, "反转低波", 17);
const loadSmallcapPicks = makeFactorLoader("smallcap", "smallcapLoaded", "/api/smallcap-value-picks", 30, () => els.smallcapBody, renderSmallcapTable, "小市值价值", 17);
const loadBreakoutPicks = makeFactorLoader("breakout", "breakoutLoaded", "/api/breakout-picks", 30, () => els.breakoutBody, renderBreakoutTable, "量价突破", 17);

function renderReversalTable(rows) {
  renderStandardStrategyTable(rows, els.reversalBody, "暂无符合条件的超跌低波标的", {
    liquidity: ["liquidity_score"],
    momentum: ["reversal_score", "oversold_calm_score"],
    trend: ["lowvol_score", "not_overextended_score"],
    execution: ["calm_turnover_score"],
  });
}

function renderSmallcapTable(rows, meta) {
  renderStandardStrategyTable(rows, els.smallcapBody, meta && meta.note ? meta.note : "暂无满足护栏的小市值标的", {
    liquidity: ["liquidity_score"],
    momentum: ["smallcap_score"],
    trend: ["value_score", "oversold_calm_score"],
    execution: ["not_overextended_score", "lowvol_score"],
  });
}

function renderBreakoutTable(rows, meta = {}) {
  renderStandardStrategyTable(
    rows,
    els.breakoutBody,
    meta.note || "暂无突破/多头排列标的；若未开启历史因子，系统会使用实时强势兜底。",
    {
      liquidity: ["volume_break_score", "liquidity_score"],
      momentum: ["momentum_score", "breakout_strength"],
      trend: ["trend_score", "breakout_strength"],
      execution: ["execution_score"],
    },
  );
}


function renderSwingTable(rows) {
  const displayRows = filterAndSortRows(rows);
  if (!displayRows.length) {
    els.swingBody.innerHTML = '<tr><td colspan="16" class="empty">暂无符合条件的波段股票</td></tr>';
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

function renderPositionTable(rows) {
  const displayRows = filterAndSortRows(rows);
  if (!displayRows.length) {
    els.positionBody.innerHTML = '<tr><td colspan="17" class="empty">暂无符合条件的中长期股票</td></tr>';
    return;
  }
  els.positionBody.innerHTML = displayRows.map(row => {
    const pctClass = row.pct_chg >= 0 ? "positive" : "negative";
    const sixtyClass = row.sixty_day_pct >= 0 ? "positive" : "negative";
    const ytdClass = row.ytd_pct >= 0 ? "positive" : "negative";
    const explanation = explanationTags(row);
    return `
      <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
        <td class="num">${row.rank}</td>
        ${codeCell(row)}
        <td>${escapeHtml(row.name)}</td>
        <td>${escapeHtml(row.theme || "-")}</td>
        <td>${escapeHtml(row.market_label)}</td>
        <td class="num">${formatNumber(row.price, 3)}</td>
        <td class="num ${pctClass}">${formatNumber(row.pct_chg, 2)}%</td>
        ${marketCapCell(row)}
        <td class="num ${sixtyClass}">${formatNumber(row.sixty_day_pct, 2)}%</td>
        <td class="num ${ytdClass}">${formatNumber(row.ytd_pct, 2)}%</td>
        <td class="num">${formatNumber(row.ma20_gap, 2)}%</td>
        <td class="num">${formatNumber(row.volatility_20d, 2)}%</td>
        <td class="num">${formatNumber(row.trend_score, 1)}</td>
        <td class="num">${formatNumber(row.quality_proxy_score, 1)}</td>
        <td class="num">${formatNumber(row.liquidity_score, 1)}</td>
        ${scoreCell(row)}
        <td class="reasons">${explanation}</td>
      </tr>
    `;
  }).join("");
  bindSentimentRows(els.positionBody);
}

function renderValidationMetrics(metrics) {
  const sample = Number(metrics.sample_count || 0);
  const outcome = Number(metrics.outcome_sample_count || 0);
  const real = Number(metrics.real_sample_count || 0);
  const replay = Number(metrics.replay_sample_count || 0);
  const horizon = metrics.primary_horizon_label || "主周期";
  const winRate = metrics.win_rate_primary_net == null ? null : Number(metrics.win_rate_primary_net);
  const avgReturn = metrics.avg_primary_return_net == null ? null : Number(metrics.avg_primary_return_net);
  els.validationSampleCount.textContent = outcome > sample ? `${sample}/${outcome}` : `${sample}`;
  els.validationWinRate.textContent = winRate != null ? `${formatNumber(winRate, 1)}%` : "-";
  els.validationHit3.textContent = `真 ${real} / 回 ${replay}`;
  els.validationAvgReturn.textContent = avgReturn != null ? `${horizon} ${formatNumber(avgReturn, 2)}%` : "-";
  renderNextDayCompare(metrics.next_day_compare || {}, metrics.replay_next_day_compare || {});
  renderValidationSimpleDecision({ sample, outcome, real, replay, winRate, avgReturn, horizon });
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

function renderValidationSimpleDecision({ sample, outcome, real, replay, winRate, avgReturn, horizon }) {
  if (!els.validationSimpleDecision) return;
  let level = "neutral";
  let text = "结论：数据正在更新，先关注锚点方向与锚点到现在变化。";
  if (outcome <= 0 && sample <= 0) {
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
  } else if (winRate >= 55 && avgReturn > 0) {
    level = "good";
    text = `结论：可观察。${horizon}次日方向命中率 ${formatNumber(winRate, 1)}%，次日平均涨跌 ${formatNumber(avgReturn, 2)}%。`;
  } else if (winRate >= 50 && avgReturn >= 0) {
    level = "watch";
    text = `结论：一般，继续观察。${horizon}方向不弱不强，暂不建议提高权重。`;
  } else {
    level = "bad";
    text = `结论：暂不加权。${horizon}次日表现偏弱，先不要依赖这个策略。`;
  }
  els.validationSimpleDecision.className = `validation-current-decision decision-${level}`;
  els.validationSimpleDecision.textContent = text;
}

function renderValidationDates(rows) {
  if (!rows.length) {
    els.validationDatesBody.innerHTML = '<tr><td colspan="4" class="empty">暂无保存记录</td></tr>';
    els.validationDetailBody.innerHTML = '<tr><td colspan="7" class="empty">暂无可查看明细</td></tr>';
    return;
  }
  els.validationDatesBody.innerHTML = rows.map(row => `
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
}

function renderValidationDetail(rows) {
  if (!rows.length) {
    els.validationDetailBody.innerHTML = '<tr><td colspan="7" class="empty">暂无明细</td></tr>';
    return;
  }
  els.validationDetailBody.innerHTML = rows.map(row => {
    const anchorPrice = Number(row.price_at_signal);
    const anchorChange = row.pct_chg_at_signal;
    const todayChange = row.current_pct_chg;
    const anchorToNow = row.anchor_to_now_return;
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
        <td class="num ${numberClass(todayChange)}">${pctText(todayChange)}</td>
        <td class="num ${numberClass(anchorToNow)}">${pctText(anchorToNow)}</td>
      </tr>
    `;
  }).join("");
  bindSentimentRows(els.validationDetailBody);
}

function primaryValidationLabel(row) {
  const strategy = row.strategy_name || "";
  if (strategy === "reversal_picks") return "5日";
  if (["swing_picks", "breakout_picks"].includes(strategy)) return "10日";
  if (["long_term", "position_picks", "tech_potential", "chokepoint_picks", "smallcap_value_picks"].includes(strategy)) return "20日";
  return "次日";
}

function primaryValidationReturn(row) {
  const strategy = row.strategy_name || "";
  if (strategy === "reversal_picks") {
    return row.signal_hold_5d_return ?? row.hold_5d_return ?? row.signal_hold_3d_return ?? row.hold_3d_return;
  }
  if (["swing_picks", "breakout_picks"].includes(strategy)) {
    return row.signal_hold_10d_return ?? row.hold_10d_return ?? row.signal_hold_5d_return ?? row.hold_5d_return;
  }
  if (["long_term", "position_picks", "tech_potential", "chokepoint_picks", "smallcap_value_picks"].includes(strategy)) {
    return row.signal_hold_20d_return ?? row.hold_20d_return ?? row.signal_hold_10d_return ?? row.hold_10d_return;
  }
  return row.signal_next_close_return ?? row.next_close_return;
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
  markSelectedValidationRow();
  if (state.selectedValidation.date && state.selectedValidation.strategy) {
    loadValidationDaily(state.selectedValidation.date, state.selectedValidation.strategy);
  }
}

function validationSampleTypeBadge(row) {
  const real = Number(row.real_count || 0);
  const replay = Number(row.replay_count || 0);
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
    els.validationSelectionLabel.textContent = "未选择日期";
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
  renderLongTermTable(state.lastRows.longTerm);
  if (state.tomorrowLoaded) {
    renderTomorrowTable(state.lastRows.tomorrow);
  }
  if (state.techLoaded) {
    renderTechTable(state.lastRows.tech);
  }
  if (state.chokepointLoaded) {
    renderChainMap(state.chokepointIndustryMap, state.chokepointMeta || {});
    renderChokepointTable(state.lastRows.chokepoint, state.chokepointMeta || {});
  }
  if (state.reversalLoaded) {
    renderReversalTable(state.lastRows.reversal);
  }
  if (state.smallcapLoaded) {
    renderSmallcapTable(state.lastRows.smallcap);
  }
  if (state.breakoutLoaded) {
    renderBreakoutTable(state.lastRows.breakout);
  }
  if (state.horizonLoaded) {
    renderSwingTable(state.lastRows.swing);
    renderPositionTable(state.lastRows.position);
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
    state.lastRows.longTerm,
    state.lastRows.tomorrow,
    state.lastRows.tech,
    state.lastRows.chokepoint,
    state.lastRows.reversal,
    state.lastRows.smallcap,
    state.lastRows.breakout,
    state.lastRows.swing,
    state.lastRows.position,
    ...(state.chokepointIndustryMap || []).map(node => node.leaders || []),
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
  const badge = verdictBadge(row.verdict);
  const score = scorePairValue(row.score, 1, "综合分");
  const entry = scorePairValue(entrySafetyValue, 0, executionScoreHint(entrySafetyValue), "买入安全 ");
  const separator = entry ? '<span class="score-pair-separator">/</span>' : "";
  const number = `<div class="score-pair" title="综合分 / 买入安全">${score}${separator}${entry}</div>`;
  const tier = row.verdict?.tier ? ` score-${escapeHtml(row.verdict.tier)}` : "";
  return `<td class="num score${tier}">${badge}${number}</td>`;
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
  if (value === "short_term") return "短期推荐";
  if (value === "long_term") return "长期推荐";
  if (value === "tech_potential") return "科技潜力";
  if (value === "tomorrow_picks") return "明天预测";
  if (value === "swing_picks") return "波段 5-10 日";
  if (value === "position_picks") return "中长期 1-3 月";
  if (value === "chokepoint_picks") return "卡脖子";
  if (value === "reversal_picks") return "反转低波";
  if (value === "smallcap_value_picks") return "小市值价值";
  if (value === "breakout_picks") return "量价突破";
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

els.refreshButton.addEventListener("click", connectRecommendationStream);
els.stockPredictionBtn.addEventListener("click", loadStockPrediction);
els.stockPredictionInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    loadStockPrediction();
  }
});
els.marketSelect.addEventListener("change", () => {
  state.tomorrowLoaded = false;
  state.techLoaded = false;
  state.chokepointLoaded = false;
  state.reversalLoaded = false;
  state.smallcapLoaded = false;
  state.breakoutLoaded = false;
  state.horizonLoaded = false;
  connectRecommendationStream();
  if (document.getElementById("tomorrowPanel").classList.contains("active")) {
    loadTomorrowPicks();
  }
  if (document.getElementById("techPanel").classList.contains("active")) {
    loadTechPotential();
  }
  if (document.getElementById("chokepointPanel").classList.contains("active")) {
    loadChokepointPicks();
  }
  if (document.getElementById("reversalPanel").classList.contains("active")) {
    loadReversalPicks();
  }
  if (document.getElementById("smallcapPanel").classList.contains("active")) {
    loadSmallcapPicks();
  }
  if (document.getElementById("breakoutPanel").classList.contains("active")) {
    loadBreakoutPicks();
  }
  if (document.getElementById("horizonPanel").classList.contains("active")) {
    loadHorizonPicks();
  }
});
els.actionFilterSelect.addEventListener("change", rerenderCurrentTables);
els.sortSelect.addEventListener("change", rerenderCurrentTables);
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
    if (button.dataset.tab === "tomorrowPanel" && !state.tomorrowLoaded) {
      loadTomorrowPicks();
    }
    if (button.dataset.tab === "techPanel" && !state.techLoaded) {
      loadTechPotential();
    }
    if (button.dataset.tab === "chokepointPanel" && !state.chokepointLoaded) {
      loadChokepointPicks();
    }
    if (button.dataset.tab === "reversalPanel" && !state.reversalLoaded) {
      loadReversalPicks();
    }
    if (button.dataset.tab === "smallcapPanel" && !state.smallcapLoaded) {
      loadSmallcapPicks();
    }
    if (button.dataset.tab === "breakoutPanel" && !state.breakoutLoaded) {
      loadBreakoutPicks();
    }
    if (button.dataset.tab === "horizonPanel" && !state.horizonLoaded) {
      loadHorizonPicks();
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
    if (button.dataset.tab === "portfolioPanel" && !state.portfolioLoaded) {
      loadPortfolio();
    }
  });
});
els.loadPortfolioBtn.addEventListener("click", loadPortfolio);
els.portfolioStrategySelect.addEventListener("change", loadPortfolio);
els.validationDaysSelect.addEventListener("change", loadValidation);
els.closeDetails.addEventListener("click", () => {
  els.detailsPanel.hidden = true;
});

connectRecommendationStream();
loadStrategyOverview();
