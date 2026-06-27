const state = {
  timer: null,
  countdown: window.APP_CONFIG.refreshSeconds,
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
  marketRegime: {},
  selectedValidation: {
    date: "",
    strategy: "",
  },
  charts: {},
};

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
  marketSentiment: document.getElementById("marketSentiment"),
  marketSelect: document.getElementById("marketSelect"),
  actionFilterSelect: document.getElementById("actionFilterSelect"),
  sortSelect: document.getElementById("sortSelect"),
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
  techBody: document.getElementById("techBody"),
  chokepointBody: document.getElementById("chokepointBody"),
  chokepointChainMap: document.getElementById("chokepointChainMap"),
  reversalBody: document.getElementById("reversalBody"),
  smallcapBody: document.getElementById("smallcapBody"),
  breakoutBody: document.getElementById("breakoutBody"),
  swingBody: document.getElementById("swingBody"),
  positionBody: document.getElementById("positionBody"),
  saveStrategySelect: document.getElementById("saveStrategySelect"),
  saveSnapshotBtn: document.getElementById("saveSnapshotBtn"),
  saveStatus: document.getElementById("saveStatus"),
  updateStatus: document.getElementById("updateStatus"),
  validationScoreboard: document.getElementById("validationScoreboard"),
  updateValidation: document.getElementById("updateValidation"),
  backfillValidationSamples: document.getElementById("backfillValidationSamples"),
  prefetchValidationHistory: document.getElementById("prefetchValidationHistory"),
  validationStrategySelect: document.getElementById("validationStrategySelect"),
  validationDaysSelect: document.getElementById("validationDaysSelect"),
  validationSelectionLabel: document.getElementById("validationSelectionLabel"),
  validationSampleCount: document.getElementById("validationSampleCount"),
  validationWinRate: document.getElementById("validationWinRate"),
  validationHit3: document.getElementById("validationHit3"),
  validationAvgReturn: document.getElementById("validationAvgReturn"),
  validationDatesBody: document.getElementById("validationDatesBody"),
  validationDetailBody: document.getElementById("validationDetailBody"),
  detailsPanel: document.getElementById("detailsPanel"),
  detailsTitle: document.getElementById("detailsTitle"),
  detailsSummary: document.getElementById("detailsSummary"),
  newsList: document.getElementById("newsList"),
  closeDetails: document.getElementById("closeDetails"),
};

async function loadRecommendations() {
  clearInterval(state.timer);
  state.countdown = window.APP_CONFIG.refreshSeconds;
  setStatus("刷新中...");
  const params = new URLSearchParams({
    top_n: "10",
    market: els.marketSelect.value,
  });
  try {
    const res = await fetch(`/api/recommendations?${params.toString()}`);
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "接口返回异常");
    }
    const recommendations = payload.recommendations || {};
    state.lastRows.shortTerm = recommendations.short_term || payload.data || [];
    state.lastRows.longTerm = recommendations.long_term || [];
    state.lastRows.consensus = payload.meta?.strategy_consensus?.rows || [];
    state.marketRegime = payload.meta?.market_regime || {};
    renderMetrics(payload);
    renderOverviewRegime(state.marketRegime);
    renderDecisionDesk(state.lastRows.consensus);
    rerenderCurrentTables();
    if (state.tomorrowLoaded) {
      loadTomorrowPicks();
    }
    if (state.techLoaded) {
      loadTechPotential();
    }
    if (state.horizonLoaded) {
      loadHorizonPicks();
    }
    setStatus(`更新时间 ${payload.meta.generated_at}，${window.APP_CONFIG.refreshSeconds} 秒后自动刷新`);
  } catch (err) {
    const message = `<tr><td colspan="16" class="empty">${escapeHtml(err.message)}</td></tr>`;
    els.shortTermBody.innerHTML = message;
    els.longTermBody.innerHTML = message;
    els.strategyConsensusBody.innerHTML = '<tr><td colspan="12" class="empty">加载失败</td></tr>';
    setStatus(`刷新失败：${err.message}`);
  } finally {
    startCountdown();
  }
}

async function loadStrategyOverview() {
  state.overviewLoaded = true;
  els.strategyOverviewGrid.innerHTML = '<div class="empty">加载中...</div>';
  els.strategyOverviewBody.innerHTML = '<tr><td colspan="11" class="empty">加载中...</td></tr>';
  try {
    const res = await fetch("/api/strategy-overview?days=20");
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "接口返回异常");
    }
    renderStrategyOverview(payload);
  } catch (err) {
    els.strategyOverviewGrid.innerHTML = `<div class="empty">${escapeHtml(err.message)}</div>`;
    els.strategyOverviewBody.innerHTML = `<tr><td colspan="11" class="empty">${escapeHtml(err.message)}</td></tr>`;
  }
}

async function loadValidation() {
  state.validationLoaded = true;
  els.validationDatesBody.innerHTML = '<tr><td colspan="4" class="empty">加载中...</td></tr>';
  const params = new URLSearchParams({
    strategy: els.validationStrategySelect.value,
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
    setStatus("策略验证已更新");
  } catch (err) {
    els.validationDatesBody.innerHTML = `<tr><td colspan="4" class="empty">${escapeHtml(err.message)}</td></tr>`;
    setStatus(`策略验证加载失败：${err.message}`);
  }
}

// C2-4：各策略主周期净胜率走势折线 + 顶部一眼结论记分牌。
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

// 一眼结论记分牌：每策略主周期扣成本后的净胜率徽章 + 一句话结论。
function renderValidationScoreboard(series) {
  if (!els.validationScoreboard) return;
  if (!series.length) {
    els.validationScoreboard.innerHTML = '<div class="empty">暂无验证数据，先在左下保存预测并回填结果</div>';
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
        verdict = "样本不足 · 待观察";
      } else if (realSamples < 10) {
        level = "watch";
        verdict = "真实样本少 · 降权";
      } else if (win == null) {
        level = "neutral";
        verdict = "无净胜率";
      } else if (win >= 55 && Number(avg || 0) > 0) {
        level = "good";
        verdict = "净表现可观察";
      } else if (win >= 50) {
        level = "watch";
        verdict = "净胜率中性";
      } else {
        level = "bad";
        verdict = "净胜率偏低";
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
          <div class="score-meta">${escapeHtml(horizon)}净胜率 · 净收益 ${avgText} · 真 ${realSamples} / 回 ${replaySamples}</div>
        </div>`;
    })
    .join("");
}

function renderValidationLine(series) {
  const active = (series || []).filter((s) => (s.daily || []).length);
  if (!active.length) {
    renderChart("validationLine", {
      title: { text: "暂无验证数据（先保存并更新预测结果）", left: "center", top: "middle", textStyle: { color: CHART_THEME.text, fontSize: 13 } },
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
    yAxis: { type: "value", name: "净胜率%", min: 0, max: 100 },
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

async function saveStrategySnapshot(strategy) {
  const btn = els.saveSnapshotBtn;
  btn.disabled = true;
  setOpsStatus(els.saveStatus, "保存中…", "pending");
  const params = new URLSearchParams({ strategy, market: els.marketSelect.value });
  try {
    const res = await fetch(`/api/strategy-validation/snapshot?${params.toString()}`, { method: "POST" });
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "保存失败");
    }
    state.selectedValidation = { date: payload.saved.signal_date || "", strategy };
    els.validationStrategySelect.value = strategy;
    setOpsStatus(
      els.saveStatus,
      `✓ 已保存 ${payload.saved.saved} 条（${payload.saved.signal_date || ""}）` +
        (payload.saved.replaced ? `，替换旧样本 ${payload.saved.replaced} 条` : ""),
      "ok"
    );
    loadStrategyOverview();
    loadValidation();
  } catch (err) {
    setOpsStatus(els.saveStatus, `✗ 保存失败：${err.message}`, "bad");
  } finally {
    btn.disabled = false;
  }
}

async function updateValidationOutcomes() {
  const btn = els.updateValidation;
  btn.disabled = true;
  setOpsStatus(els.updateStatus, "更新中…", "pending");
  const params = new URLSearchParams();
  const strategy = state.selectedValidation.strategy || els.validationStrategySelect.value;
  if (strategy) {
    params.set("strategy", strategy);
  }
  if (state.selectedValidation.date) {
    params.set("date", state.selectedValidation.date);
  }
  try {
    const url = `/api/strategy-validation/update${params.toString() ? `?${params.toString()}` : ""}`;
    const res = await fetch(url, { method: "POST" });
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "更新失败");
    }
    setOpsStatus(els.updateStatus, `✓ 更新 ${payload.result.updated} 条 / 跳过 ${payload.result.skipped} 条`, "ok");
    loadStrategyOverview();
    loadValidation();
  } catch (err) {
    setOpsStatus(els.updateStatus, `✗ 更新失败：${err.message}`, "bad");
  } finally {
    btn.disabled = false;
  }
}

async function prefetchHistoryAndUpdateValidation() {
  const btn = els.prefetchValidationHistory;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "处理中…";
  setOpsStatus(els.updateStatus, "下载历史并验证中…（可能需要几十秒）", "pending");
  const params = new URLSearchParams({ days: "180", limit: "500", update: "1" });
  const strategy = state.selectedValidation.strategy || els.validationStrategySelect.value;
  if (strategy) {
    params.set("strategy", strategy);
  }
  if (state.selectedValidation.date) {
    params.set("date", state.selectedValidation.date);
  }
  try {
    const res = await fetch(`/api/strategy-validation/prefetch-history?${params.toString()}`, { method: "POST" });
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "历史下载失败");
    }
    const prefetch = payload.prefetch || {};
    const outcome = payload.outcome || {};
    setOpsStatus(
      els.updateStatus,
      `✓ 下载 ${prefetch.downloaded || 0} / 缓存 ${prefetch.cached || 0} / 失败 ${prefetch.failed || 0}；更新 ${outcome.updated || 0} / 跳过 ${outcome.skipped || 0}`,
      "ok"
    );
    loadStrategyOverview();
    loadValidation();
  } catch (err) {
    setOpsStatus(els.updateStatus, `✗ 下载/验证失败：${err.message}`, "bad");
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

async function backfillValidationSamples() {
  const btn = els.backfillValidationSamples;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "回放中…";
  setOpsStatus(els.updateStatus, "正在下载K线并回放历史信号…（首次可能需要较久）", "pending");
  const strategy = state.selectedValidation.strategy || els.validationStrategySelect.value || "tomorrow_picks";
  const params = new URLSearchParams({
    strategy,
    days: "260",
    replay_days: "20",
    top_n: "30",
    holding_days: "3",
    limit: "120",
  });
  try {
    const res = await fetch(`/api/strategy-validation/backfill-samples?${params.toString()}`, { method: "POST" });
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || payload.replay?.error || "历史回放失败");
    }
    const prefetch = payload.prefetch || {};
    const replay = payload.replay || {};
    const metrics = payload.metrics || {};
    setOpsStatus(
      els.updateStatus,
      `✓ 回放 ${replay.date_count || 0} 日 / 新增 ${replay.saved || 0} 条；结果更新 ${replay.outcome?.updated || 0} 条；当前样本 ${metrics.sample_count || 0} 条`,
      "ok"
    );
    if ((prefetch.failed || 0) > 0) {
      setOpsStatus(
        els.updateStatus,
        `✓ 回放 ${replay.saved || 0} 条；${prefetch.failed || 0} 只历史下载失败，已跳过；当前样本 ${metrics.sample_count || 0} 条`,
        "ok"
      );
    }
    state.selectedValidation = { date: "", strategy };
    els.validationStrategySelect.value = strategy;
    loadStrategyOverview();
    loadValidation();
  } catch (err) {
    setOpsStatus(els.updateStatus, `✗ 回放失败：${err.message}`, "bad");
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

async function loadValidationDaily(date, strategy) {
  state.selectedValidation = { date, strategy };
  renderValidationSelection();
  markSelectedValidationRow();
  els.validationDetailBody.innerHTML = '<tr><td colspan="13" class="empty">加载中...</td></tr>';
  const params = new URLSearchParams({ date, strategy });
  try {
    const res = await fetch(`/api/strategy-validation/daily?${params.toString()}`);
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "接口返回异常");
    }
    renderValidationDetail(payload.data || []);
  } catch (err) {
    els.validationDetailBody.innerHTML = `<tr><td colspan="12" class="empty">${escapeHtml(err.message)}</td></tr>`;
  }
}

async function loadTechPotential() {
  state.techLoaded = true;
  els.techBody.innerHTML = '<tr><td colspan="16" class="empty">加载中...</td></tr>';
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
    state.lastRows.tech = payload.data || [];
    renderMetrics({ health: payload.health, meta: payload.meta, market_sentiment: {} });
    renderTechTable(state.lastRows.tech);
    setStatus(`科技潜力榜更新时间 ${payload.meta.generated_at}`);
  } catch (err) {
    els.techBody.innerHTML = `<tr><td colspan="16" class="empty">${escapeHtml(err.message)}</td></tr>`;
    setStatus(`科技潜力榜加载失败：${err.message}`);
  }
}

async function loadTomorrowPicks() {
  state.tomorrowLoaded = true;
  els.tomorrowBody.innerHTML = '<tr><td colspan="16" class="empty">加载中...</td></tr>';
  const params = new URLSearchParams({
    top_n: "50",
    market: els.marketSelect.value,
  });
  try {
    const res = await fetch(`/api/tomorrow-picks?${params.toString()}`);
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "接口返回异常");
    }
    state.lastRows.tomorrow = payload.data || [];
    renderMetrics({ health: payload.health, meta: payload.meta, market_sentiment: {} });
    renderTomorrowPredictionStrip(payload);
    renderTomorrowTable(state.lastRows.tomorrow);
    loadTomorrowValidationMetrics();
    setStatus(`明天预测更新时间 ${payload.meta.generated_at || "最近快照"}`);
  } catch (err) {
    els.tomorrowBody.innerHTML = `<tr><td colspan="16" class="empty">${escapeHtml(err.message)}</td></tr>`;
    resetTomorrowPredictionStrip(err.message);
    setStatus(`明天预测加载失败：${err.message}`);
  }
}

async function loadHorizonPicks() {
  state.horizonLoaded = true;
  els.swingBody.innerHTML = '<tr><td colspan="16" class="empty">加载中...</td></tr>';
  els.positionBody.innerHTML = '<tr><td colspan="16" class="empty">加载中...</td></tr>';
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
    state.lastRows.swing = swingPayload.data || [];
    state.lastRows.position = positionPayload.data || [];
    renderMetrics({ health: swingPayload.health, meta: swingPayload.meta, market_sentiment: {} });
    renderSwingTable(state.lastRows.swing);
    renderPositionTable(state.lastRows.position);
    setStatus(`波段/中长期更新时间 ${swingPayload.meta.generated_at}`);
  } catch (err) {
    const message = `<tr><td colspan="16" class="empty">${escapeHtml(err.message)}</td></tr>`;
    els.swingBody.innerHTML = message;
    els.positionBody.innerHTML = message;
    setStatus(`波段/中长期加载失败：${err.message}`);
  }
}

function renderMetrics(payload) {
  const health = payload.health || {};
  const meta = payload.meta || {};
  const marketSentiment = payload.market_sentiment || {};
  els.quoteSource.textContent = health.quotes_source || "-";
  els.sentimentSource.textContent = health.sentiment_source || "-";
  els.candidateCount.textContent = meta.candidate_count ?? "-";
  els.marketSentiment.textContent = marketSentiment.score ? `${marketSentiment.score}` : "-";
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
  els.tomorrowStrategyVersion.textContent = meta.strategy_version || "tomorrow_picks_v2";
  els.tomorrowDataStatus.textContent = dataStatus;
  els.tomorrowCandidateCount.textContent = meta.candidate_count ?? "-";
  els.tomorrowBuyableFilter.textContent = `主板≤${formatNumber(policy.main_max_gain, 1)}%，创/科≤${formatNumber(policy.growth_max_gain, 1)}%，成交额≥${minTurnover}`;
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
  if (row.serenity_profile) {
    const profile = row.serenity_profile;
    tags.push(
      `<span class="tag ${actionTagClass(profile.action_label)}">质量:${formatNumber(profile.quality_score, 1)} ${escapeHtml(profile.action_label || "-")}</span>`
    );
    (profile.evidence || []).slice(0, 2).forEach(item => {
      tags.push(`<span class="tag stable">证据:${escapeHtml(item.label || "-")}</span>`);
    });
  }

  (row.reasons || []).slice(0, 5).forEach(text => {
    tags.push(`<span class="tag">推荐:${escapeHtml(text)}</span>`);
  });
  tags.push(riskTag("追高", row.chase_risk));
  tags.push(riskTag("透支", row.overextension));

  (row.failure_reasons || []).slice(0, 4).forEach(text => {
    tags.push(`<span class="tag risk">失败:${escapeHtml(text)}</span>`);
  });
  (row.risk_words || []).slice(0, 3).forEach(text => {
    tags.push(`<span class="tag risk">舆情:${escapeHtml(text)}</span>`);
  });
  tags.push(similarSignalStatsTag(row.similar_signal_stats));
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

function renderShortTermTable(rows) {
  const displayRows = filterAndSortRows(rows);
  if (!displayRows.length) {
    els.shortTermBody.innerHTML = '<tr><td colspan="16" class="empty">暂无符合条件的股票</td></tr>';
    return;
  }
  els.shortTermBody.innerHTML = displayRows.map(row => {
    const pctClass = row.pct_chg >= 0 ? "positive" : "negative";
    const explanation = explanationTags(row);
    return `
      <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
        <td class="num">${row.rank}</td>
        <td class="num">${escapeHtml(row.code)}</td>
        <td>${escapeHtml(row.name)}</td>
        <td>${escapeHtml(row.market_label)}</td>
        <td class="num">${formatNumber(row.price, 3)}</td>
        <td class="num ${pctClass}">${formatNumber(row.pct_chg, 2)}%</td>
        <td class="num">${formatNumber(row.speed || row.five_min_pct, 2)}%</td>
        <td class="num">${formatNumber(row.volume_ratio, 2)}</td>
        <td class="num">${formatNumber(row.turnover_rate, 2)}%</td>
        <td class="num">${formatMoney(row.turnover)}</td>
        <td>${escapeHtml(row.industry || "-")}</td>
        <td class="num">${formatNumber(row.momentum_score, 1)}</td>
        <td class="num">${formatNumber(row.sentiment_score, 1)}</td>
        ${scoreCell(row)}
        <td>${stabilityTag(row)}</td>
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
    const explanation = explanationTags(row);
    return `
      <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
        <td class="num">${row.rank}</td>
        <td class="num">${escapeHtml(row.code)}</td>
        <td>${escapeHtml(row.name)}</td>
        <td>${escapeHtml(row.market_label)}</td>
        <td class="num">${formatNumber(row.price, 3)}</td>
        <td class="num ${pctClass}">${formatNumber(row.pct_chg, 2)}%</td>
        <td class="num ${sixtyClass}">${formatNumber(row.sixty_day_pct, 2)}%</td>
        <td class="num ${ytdClass}">${formatNumber(row.ytd_pct, 2)}%</td>
        <td class="num">${formatNumber(row.turnover_rate, 2)}%</td>
        <td class="num">${formatMoney(row.turnover)}</td>
        <td>${escapeHtml(row.industry || "-")}</td>
        <td class="num">${formatNumber(row.trend_score, 1)}</td>
        <td class="num">${formatNumber(row.sentiment_score, 1)}</td>
        ${scoreCell(row)}
        <td>${stabilityTag(row)}</td>
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
    const explanation = explanationTags(row);
    return `
      <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
        <td class="num">${row.rank}</td>
        <td class="num">${escapeHtml(row.code)}</td>
        <td>${escapeHtml(row.name)}</td>
        <td>${escapeHtml(row.market_label)}</td>
        <td class="num">${formatNumber(row.price, 3)}</td>
        <td class="num ${pctClass}">${formatNumber(row.pct_chg, 2)}%</td>
        <td class="num">${formatNumber(row.volume_ratio, 2)}</td>
        <td class="num">${formatNumber(row.turnover_rate, 2)}%</td>
        <td class="num">${formatMoney(row.turnover)}</td>
        <td class="num ${sixtyClass}">${formatNumber(row.sixty_day_pct, 2)}%</td>
        <td class="num">${formatNumber(row.liquidity_score, 1)}</td>
        <td class="num">${formatNumber(row.momentum_score, 1)}</td>
        <td class="num">${formatNumber(row.trend_score, 1)}</td>
        <td class="num">${formatNumber(row.execution_score, 1)}</td>
        ${scoreCell(row)}
        <td class="reasons">${explanation}</td>
      </tr>
    `;
  }).join("");

  bindSentimentRows(els.tomorrowBody);
}

function renderTechTable(rows) {
  const displayRows = filterAndSortRows(rows);
  if (!displayRows.length) {
    els.techBody.innerHTML = '<tr><td colspan="16" class="empty">暂无符合条件的科技潜力股票</td></tr>';
    return;
  }
  els.techBody.innerHTML = displayRows.map(row => {
    const pctClass = row.pct_chg >= 0 ? "positive" : "negative";
    const sixtyClass = row.sixty_day_pct >= 0 ? "positive" : "negative";
    const ytdClass = row.ytd_pct >= 0 ? "positive" : "negative";
    const explanation = explanationTags(row);
    return `
      <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
        <td class="num">${row.rank}</td>
        <td class="num">${escapeHtml(row.code)}</td>
        <td>${escapeHtml(row.name)}</td>
        <td>${escapeHtml(row.theme || "-")}</td>
        <td>${escapeHtml(row.market_label)}</td>
        <td class="num">${formatNumber(row.price, 3)}</td>
        <td class="num ${pctClass}">${formatNumber(row.pct_chg, 2)}%</td>
        <td class="num ${sixtyClass}">${formatNumber(row.sixty_day_pct, 2)}%</td>
        <td class="num ${ytdClass}">${formatNumber(row.ytd_pct, 2)}%</td>
        <td class="num">${formatMoney(row.turnover)}</td>
        <td class="num">${formatNumber(row.theme_score, 1)}</td>
        <td class="num">${formatNumber(row.early_trend_score, 1)}</td>
        <td class="num">${formatNumber(row.not_overextended_score, 1)}</td>
        <td class="num">${formatNumber(row.execution_score, 1)}</td>
        ${scoreCell(row)}
        <td class="reasons">${explanation}</td>
      </tr>
    `;
  }).join("");

  bindSentimentRows(els.techBody);
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
    renderMetrics({ health: payload.health, meta: payload.meta, market_sentiment: {} });
    renderChainMap(payload.meta?.chain || []);
    renderChokepointTable(state.lastRows.chokepoint);
    setStatus(`卡脖子榜更新时间 ${payload.meta.generated_at}`);
  } catch (err) {
    els.chokepointBody.innerHTML = `<tr><td colspan="16" class="empty">${escapeHtml(err.message)}</td></tr>`;
    els.chokepointChainMap.innerHTML = `<div class="empty">${escapeHtml(err.message)}</div>`;
    setStatus(`卡脖子榜加载失败：${err.message}`);
  }
}

// 产业链全景图：按环节分组的代表票卡片。
function renderChainMap(chain) {
  const active = (chain || []).filter((node) => (node.picks || []).length);
  if (!active.length) {
    els.chokepointChainMap.innerHTML = '<div class="empty">当日无命中卡脖子环节的标的</div>';
    return;
  }
  els.chokepointChainMap.innerHTML = active
    .map((node) => {
      const picks = node.picks
        .map((p) => {
          const v = p.verdict || {};
          const badge = v.tier ? `<span class="verdict verdict-${escapeHtml(v.tier)}">${escapeHtml(v.label || v.tier)}</span>` : "";
          const pctClass = Number(p.pct_chg || 0) >= 0 ? "positive" : "negative";
          return `
            <div class="chain-pick" data-code="${escapeHtml(p.code)}" data-name="${escapeHtml(p.name)}">
              <div class="chain-pick-name">${escapeHtml(p.name)} <span class="chain-pick-code">${escapeHtml(p.code)}</span></div>
              <div class="chain-pick-meta">
                ${badge}
                <span class="num">${formatNumber(p.score, 1)}</span>
                <span class="num ${pctClass}">${formatNumber(p.pct_chg, 2)}%</span>
              </div>
            </div>`;
        })
        .join("");
      return `
        <div class="chain-node">
          <div class="chain-node-head">
            <span class="chain-segment">${escapeHtml(node.segment)}</span>
            <span class="chain-count">${node.count}</span>
          </div>
          <div class="chain-picks">${picks}</div>
        </div>`;
    })
    .join("");
  bindSentimentRows(els.chokepointChainMap);
}

function renderChokepointTable(rows) {
  const displayRows = filterAndSortRows(rows);
  if (!displayRows.length) {
    els.chokepointBody.innerHTML = '<tr><td colspan="16" class="empty">暂无命中卡脖子环节的股票</td></tr>';
    return;
  }
  els.chokepointBody.innerHTML = displayRows.map(row => {
    const pctClass = row.pct_chg >= 0 ? "positive" : "negative";
    const sixtyClass = row.sixty_day_pct >= 0 ? "positive" : "negative";
    const ytdClass = row.ytd_pct >= 0 ? "positive" : "negative";
    const explanation = explanationTags(row);
    return `
      <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
        <td class="num">${row.rank}</td>
        <td class="num">${escapeHtml(row.code)}</td>
        <td>${escapeHtml(row.name)}</td>
        <td>${escapeHtml(row.chain_segment || "-")}</td>
        <td>${escapeHtml(row.market_label)}</td>
        <td class="num">${formatNumber(row.price, 3)}</td>
        <td class="num ${pctClass}">${formatNumber(row.pct_chg, 2)}%</td>
        <td class="num ${sixtyClass}">${formatNumber(row.sixty_day_pct, 2)}%</td>
        <td class="num ${ytdClass}">${formatNumber(row.ytd_pct, 2)}%</td>
        <td class="num">${formatMoney(row.turnover)}</td>
        <td class="num">${formatNumber(row.chokepoint_score, 1)}</td>
        <td class="num">${formatNumber(row.early_trend_score, 1)}</td>
        <td class="num">${formatNumber(row.not_overextended_score, 1)}</td>
        <td class="num">${formatNumber(row.execution_score, 1)}</td>
        ${scoreCell(row)}
        <td class="reasons">${explanation}</td>
      </tr>
    `;
  }).join("");

  bindSentimentRows(els.chokepointBody);
}

// ===== 新增盈利策略页（反转低波 / 小市值价值 / 量价突破） =====
function makeFactorLoader(stateKey, loadedKey, endpoint, topN, bodyEl, renderFn, label) {
  return async function () {
    state[loadedKey] = true;
    bodyEl().innerHTML = '<tr><td colspan="16" class="empty">加载中...</td></tr>';
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
      bodyEl().innerHTML = `<tr><td colspan="16" class="empty">${escapeHtml(err.message)}</td></tr>`;
      setStatus(`${label}加载失败：${err.message}`);
    }
  };
}

const loadReversalPicks = makeFactorLoader("reversal", "reversalLoaded", "/api/reversal-picks", 30, () => els.reversalBody, renderReversalTable, "反转低波");
const loadSmallcapPicks = makeFactorLoader("smallcap", "smallcapLoaded", "/api/smallcap-value-picks", 30, () => els.smallcapBody, renderSmallcapTable, "小市值价值");
const loadBreakoutPicks = makeFactorLoader("breakout", "breakoutLoaded", "/api/breakout-picks", 30, () => els.breakoutBody, renderBreakoutTable, "量价突破");

function renderReversalTable(rows) {
  const r = filterAndSortRows(rows);
  if (!r.length) { els.reversalBody.innerHTML = '<tr><td colspan="16" class="empty">暂无符合条件的超跌低波标的</td></tr>'; return; }
  els.reversalBody.innerHTML = r.map(row => {
    const pc = row.pct_chg >= 0 ? "positive" : "negative";
    const s60 = row.sixty_day_pct >= 0 ? "positive" : "negative";
    const r20 = row.ret_20d >= 0 ? "positive" : "negative";
    return `
      <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
        <td class="num">${row.rank}</td>
        <td class="num">${escapeHtml(row.code)}</td>
        <td>${escapeHtml(row.name)}</td>
        <td>${escapeHtml(row.market_label)}</td>
        <td class="num">${formatNumber(row.price, 3)}</td>
        <td class="num ${pc}">${formatNumber(row.pct_chg, 2)}%</td>
        <td class="num ${s60}">${formatNumber(row.sixty_day_pct, 2)}%</td>
        <td class="num ${r20}">${formatNumber(row.ret_20d, 2)}%</td>
        <td class="num">${formatNumber(row.volatility_20d, 2)}</td>
        <td class="num">${formatNumber(row.turnover_rate, 2)}%</td>
        <td class="num">${formatNumber(row.reversal_score, 1)}</td>
        <td class="num">${formatNumber(row.lowvol_score, 1)}</td>
        <td class="num">${formatNumber(row.calm_turnover_score, 1)}</td>
        ${scoreCell(row)}
        <td class="reasons">${explanationTags(row)}</td>
      </tr>`;
  }).join("");
  bindSentimentRows(els.reversalBody);
}

function renderSmallcapTable(rows, meta) {
  const r = filterAndSortRows(rows);
  if (!r.length) {
    const note = meta && meta.note ? escapeHtml(meta.note) : "暂无满足护栏的小市值标的";
    els.smallcapBody.innerHTML = `<tr><td colspan="14" class="empty">${note}</td></tr>`;
    return;
  }
  els.smallcapBody.innerHTML = r.map(row => {
    const pc = row.pct_chg >= 0 ? "positive" : "negative";
    return `
      <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
        <td class="num">${row.rank}</td>
        <td class="num">${escapeHtml(row.code)}</td>
        <td>${escapeHtml(row.name)}</td>
        <td>${escapeHtml(row.market_label)}</td>
        <td class="num">${formatNumber(row.price, 3)}</td>
        <td class="num ${pc}">${formatNumber(row.pct_chg, 2)}%</td>
        <td class="num">${formatNumber((row.float_market_cap || 0) / 1e8, 1)}</td>
        <td class="num">${formatNumber(row.pe_dynamic, 1)}</td>
        <td class="num">${formatNumber(row.pb, 2)}</td>
        <td class="num">${formatNumber(row.turnover_rate, 2)}%</td>
        <td class="num">${formatNumber(row.smallcap_score, 1)}</td>
        <td class="num">${formatNumber(row.value_score, 1)}</td>
        ${scoreCell(row)}
        <td class="reasons">${explanationTags(row)}</td>
      </tr>`;
  }).join("");
  bindSentimentRows(els.smallcapBody);
}

function renderBreakoutTable(rows) {
  const r = filterAndSortRows(rows);
  if (!r.length) { els.breakoutBody.innerHTML = '<tr><td colspan="14" class="empty">暂无突破/多头排列标的（需开启历史因子）</td></tr>'; return; }
  els.breakoutBody.innerHTML = r.map(row => {
    const pc = row.pct_chg >= 0 ? "positive" : "negative";
    const s60 = row.sixty_day_pct >= 0 ? "positive" : "negative";
    return `
      <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
        <td class="num">${row.rank}</td>
        <td class="num">${escapeHtml(row.code)}</td>
        <td>${escapeHtml(row.name)}</td>
        <td>${escapeHtml(row.market_label)}</td>
        <td class="num">${formatNumber(row.price, 3)}</td>
        <td class="num ${pc}">${formatNumber(row.pct_chg, 2)}%</td>
        <td class="num ${s60}">${formatNumber(row.sixty_day_pct, 2)}%</td>
        <td>${row.ma_bull_aligned ? '<span class="tag strategy">多头</span>' : "-"}</td>
        <td>${row.breakout_20d ? '<span class="tag strategy">新高</span>' : "-"}</td>
        <td class="num">${formatNumber(row.vol_ma5_ratio, 2)}</td>
        <td class="num">${formatNumber(row.breakout_strength, 1)}</td>
        <td class="num">${formatNumber(row.volume_break_score, 1)}</td>
        ${scoreCell(row)}
        <td class="reasons">${explanationTags(row)}</td>
      </tr>`;
  }).join("");
  bindSentimentRows(els.breakoutBody);
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
        <td class="num">${escapeHtml(row.code)}</td>
        <td>${escapeHtml(row.name)}</td>
        <td>${escapeHtml(row.market_label)}</td>
        <td class="num">${formatNumber(row.price, 3)}</td>
        <td class="num ${pctClass}">${formatNumber(row.pct_chg, 2)}%</td>
        <td class="num ${ret5Class}">${formatNumber(row.ret_5d, 2)}%</td>
        <td class="num ${ret10Class}">${formatNumber(row.ret_10d, 2)}%</td>
        <td class="num ${ret20Class}">${formatNumber(row.ret_20d, 2)}%</td>
        <td class="num">${formatNumber(row.ma20_gap, 2)}%</td>
        <td class="num">${formatMoney(row.turnover)}</td>
        <td class="num">${formatNumber(row.momentum_score, 1)}</td>
        <td class="num">${formatNumber(row.trend_score, 1)}</td>
        <td class="num">${formatNumber(row.liquidity_score, 1)}</td>
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
    els.positionBody.innerHTML = '<tr><td colspan="16" class="empty">暂无符合条件的中长期股票</td></tr>';
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
        <td class="num">${escapeHtml(row.code)}</td>
        <td>${escapeHtml(row.name)}</td>
        <td>${escapeHtml(row.theme || "-")}</td>
        <td>${escapeHtml(row.market_label)}</td>
        <td class="num">${formatNumber(row.price, 3)}</td>
        <td class="num ${pctClass}">${formatNumber(row.pct_chg, 2)}%</td>
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
  els.validationSampleCount.textContent = outcome > sample ? `${sample}/${outcome}` : `${sample}`;
  els.validationWinRate.textContent = metrics.win_rate_primary_net != null ? `${formatNumber(metrics.win_rate_primary_net, 1)}%` : "-";
  els.validationHit3.textContent = `${real}/${replay}`;
  els.validationAvgReturn.textContent = metrics.avg_primary_return_net != null ? `${horizon} ${formatNumber(metrics.avg_primary_return_net, 2)}%` : "-";
}

function renderValidationDates(rows) {
  if (!rows.length) {
    els.validationDatesBody.innerHTML = '<tr><td colspan="4" class="empty">暂无保存记录</td></tr>';
    els.validationDetailBody.innerHTML = '<tr><td colspan="13" class="empty">暂无可查看明细</td></tr>';
    return;
  }
  els.validationDatesBody.innerHTML = rows.map(row => `
    <tr data-date="${escapeHtml(row.signal_date)}" data-strategy="${escapeHtml(row.strategy_name)}">
      <td>${escapeHtml(row.signal_date)}</td>
      <td>${escapeHtml(strategyLabel(row.strategy_name))}</td>
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
    els.validationDetailBody.innerHTML = '<tr><td colspan="13" class="empty">暂无明细</td></tr>';
    return;
  }
  els.validationDetailBody.innerHTML = rows.map(row => {
    const reasons = (row.reasons || []).map(text => `<span class="tag">${escapeHtml(text)}</span>`).join("");
    const signalClose = row.signal_next_close_return ?? row.next_close_return;
    const signalHigh = row.signal_intraday_high_return ?? row.intraday_high_return;
    const signalHold = primaryValidationReturn(row);
    const holdLabel = primaryValidationLabel(row);
    const closeClass = Number(signalClose || 0) >= 0 ? "positive" : "negative";
    const openCloseClass = Number(row.next_close_return || 0) >= 0 ? "positive" : "negative";
    const holdClass = Number(signalHold || 0) >= 0 ? "positive" : "negative";
    return `
      <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
        <td class="num">${row.rank}</td>
        <td class="num">${escapeHtml(row.code)}</td>
        <td>${escapeHtml(row.name)}</td>
        <td>${escapeHtml(strategyLabel(row.strategy_name))}</td>
        <td class="num">${formatNumber(row.price_at_signal, 3)}</td>
        <td class="num ${closeClass}">${signalClose == null ? "-" : `${formatNumber(signalClose, 2)}%`}</td>
        <td class="num">${signalHigh == null ? "-" : `${formatNumber(signalHigh, 2)}%`}</td>
        <td class="num ${openCloseClass}">${row.next_close_return == null ? "-" : `${formatNumber(row.next_close_return, 2)}%`}</td>
        <td class="num ${holdClass}">${signalHold == null ? "-" : `${escapeHtml(holdLabel)} ${formatNumber(signalHold, 2)}%`}</td>
        <td class="num">${row.future_days ?? "-"}</td>
        <td>${Number(row.signal_hit_3pct ?? row.hit_3pct ?? 0) ? "是" : "否"}</td>
        <td>${Number(row.signal_hit_5pct ?? row.hit_5pct ?? 0) ? "是" : "否"}</td>
        <td class="reasons">${reasons}</td>
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
    const first = rows[0];
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
    state.lastRows.swing,
    state.lastRows.position,
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

// C3：表格综合分单元格 = verdict 徽章 + 数值。
function scoreCell(row) {
  const badge = verdictBadge(row.verdict);
  const number = `<div>${formatNumber(row.score, 1)}</div>`;
  return `<td class="num score">${badge}${number}</td>`;
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
    ["可执行", row.execution_score],
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

function startCountdown() {
  state.timer = setInterval(() => {
    state.countdown -= 1;
    if (state.countdown <= 0) {
      loadRecommendations();
      return;
    }
    setStatus(`下次刷新 ${state.countdown} 秒`);
  }, 1000);
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

els.refreshButton.addEventListener("click", loadRecommendations);
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
  loadRecommendations();
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
    if (button.dataset.tab === "overviewPanel" && !state.overviewLoaded) {
      loadStrategyOverview();
    }
  });
});
els.saveSnapshotBtn.addEventListener("click", () => saveStrategySnapshot(els.saveStrategySelect.value));
els.backfillValidationSamples.addEventListener("click", backfillValidationSamples);
els.prefetchValidationHistory.addEventListener("click", prefetchHistoryAndUpdateValidation);
els.updateValidation.addEventListener("click", updateValidationOutcomes);
els.validationStrategySelect.addEventListener("change", () => {
  state.selectedValidation = { date: "", strategy: els.validationStrategySelect.value };
  loadValidation();
});
els.validationDaysSelect.addEventListener("change", loadValidation);
els.closeDetails.addEventListener("click", () => {
  els.detailsPanel.hidden = true;
});

loadRecommendations();
loadStrategyOverview();
