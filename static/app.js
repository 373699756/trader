const state = {
  timer: null,
  countdown: window.APP_CONFIG.refreshSeconds,
  lastRows: {
    shortTerm: [],
    longTerm: [],
    tomorrow: [],
    tech: [],
    swing: [],
    position: [],
  },
  tomorrowLoaded: false,
  techLoaded: false,
  horizonLoaded: false,
  overviewLoaded: false,
  validationLoaded: false,
  selectedValidation: {
    date: "",
    strategy: "",
  },
};

const els = {
  statusText: document.getElementById("statusText"),
  quoteSource: document.getElementById("quoteSource"),
  sentimentSource: document.getElementById("sentimentSource"),
  candidateCount: document.getElementById("candidateCount"),
  marketSentiment: document.getElementById("marketSentiment"),
  marketSelect: document.getElementById("marketSelect"),
  refreshButton: document.getElementById("refreshButton"),
  tabButtons: document.querySelectorAll(".tab-button"),
  tabPanels: document.querySelectorAll(".tab-panel"),
  overviewBestStrategy: document.getElementById("overviewBestStrategy"),
  overviewVerifiedCount: document.getElementById("overviewVerifiedCount"),
  overviewSampleCount: document.getElementById("overviewSampleCount"),
  overviewDays: document.getElementById("overviewDays"),
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
  swingBody: document.getElementById("swingBody"),
  positionBody: document.getElementById("positionBody"),
  saveTechSnapshot: document.getElementById("saveTechSnapshot"),
  saveTomorrowSnapshot: document.getElementById("saveTomorrowSnapshot"),
  saveSwingSnapshot: document.getElementById("saveSwingSnapshot"),
  savePositionSnapshot: document.getElementById("savePositionSnapshot"),
  updateValidation: document.getElementById("updateValidation"),
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
    renderMetrics(payload);
    renderShortTermTable(state.lastRows.shortTerm);
    renderLongTermTable(state.lastRows.longTerm);
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
    setStatus("策略验证已更新");
  } catch (err) {
    els.validationDatesBody.innerHTML = `<tr><td colspan="4" class="empty">${escapeHtml(err.message)}</td></tr>`;
    setStatus(`策略验证加载失败：${err.message}`);
  }
}

async function saveStrategySnapshot(strategy) {
  setStatus("保存预测中...");
  const params = new URLSearchParams({
    strategy,
    market: els.marketSelect.value,
  });
  try {
    const res = await fetch(`/api/strategy-validation/snapshot?${params.toString()}`, { method: "POST" });
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "保存失败");
    }
    state.selectedValidation = {
      date: payload.saved.signal_date || "",
      strategy,
    };
    els.validationStrategySelect.value = strategy;
    setStatus(`已保存 ${payload.saved.saved} 条预测，替换旧样本 ${payload.saved.replaced || 0} 条`);
    loadStrategyOverview();
    loadValidation();
  } catch (err) {
    setStatus(`保存预测失败：${err.message}`);
  }
}

async function updateValidationOutcomes() {
  setStatus("更新当前验证结果中...");
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
    setStatus(`验证结果已更新 ${payload.result.updated} 条，跳过 ${payload.result.skipped} 条`);
    loadStrategyOverview();
    loadValidation();
  } catch (err) {
    setStatus(`更新验证失败：${err.message}`);
  }
}

async function loadValidationDaily(date, strategy) {
  state.selectedValidation = { date, strategy };
  renderValidationSelection();
  markSelectedValidationRow();
  els.validationDetailBody.innerHTML = '<tr><td colspan="12" class="empty">加载中...</td></tr>';
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
  const verifiedCount = strategies.filter(row => Number(row.metrics?.sample_count || 0) > 0).length;
  const sampleCount = strategies.reduce((sum, row) => sum + Number(row.metrics?.sample_count || 0), 0);
  els.overviewBestStrategy.textContent = best ? best.label : "暂无";
  els.overviewVerifiedCount.textContent = `${verifiedCount}/${strategies.length}`;
  els.overviewSampleCount.textContent = sampleCount;
  els.overviewDays.textContent = `近${payload.days || 20}个保存日`;

  if (!strategies.length) {
    els.strategyOverviewGrid.innerHTML = '<div class="empty">暂无策略</div>';
    els.strategyOverviewBody.innerHTML = '<tr><td colspan="11" class="empty">暂无策略</td></tr>';
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
          <div><span>次日</span><strong>${formatPercent(metrics.avg_next_close_return)}</strong></div>
          <div><span>3%命中</span><strong>${formatPercent(metrics.hit_3pct_rate)}</strong></div>
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
        <td class="num ${numberClass(metrics.avg_next_close_return)}">${formatPercent(metrics.avg_next_close_return)}</td>
        <td class="num">${formatPercent(metrics.hit_3pct_rate)}</td>
        <td class="num ${numberClass(metrics.avg_hold_3d_return)}">${formatPercent(metrics.avg_hold_3d_return)}</td>
        <td class="num ${numberClass(metrics.avg_max_drawdown_3d)}">${formatPercent(metrics.avg_max_drawdown_3d)}</td>
        <td>${escapeHtml(latest.signal_date || "-")}</td>
        <td><span class="tag ${status.level === "bad" ? "risk" : ""}">${escapeHtml(status.label || "待验证")}</span></td>
        <td>${escapeHtml(status.advice || "-")}</td>
      </tr>
    `;
  }).join("");
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
    els.tomorrowValidationHit3.textContent = metrics.hit_3pct_rate != null ? `${formatNumber(metrics.hit_3pct_rate, 1)}%` : "-";
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

function riskTag(prefix, risk) {
  if (!risk) {
    return `<span class="tag stable">${prefix}:未知</span>`;
  }
  const level = risk.level || "low";
  const cls = level === "high" ? "risk" : level === "medium" ? "warning" : "stable";
  const label = risk.label || (level === "high" ? "高" : level === "medium" ? "中" : "低");
  return `<span class="tag ${cls}">${escapeHtml(prefix)}:${escapeHtml(label)}</span>`;
}

function similarSignalStatsTag(stats) {
  if (!stats || !Number(stats.sample_count || 0)) {
    return '<span class="tag stable">同类胜率:暂无样本</span>';
  }
  const sample = Number(stats.sample_count || 0);
  const hit3 = stats.hit_3pct_rate == null ? "-" : `${formatNumber(stats.hit_3pct_rate, 1)}%`;
  const win = stats.win_rate_next_close == null ? "-" : `${formatNumber(stats.win_rate_next_close, 1)}%`;
  const avg = stats.avg_next_close_return == null ? "-" : `${formatNumber(stats.avg_next_close_return, 2)}%`;
  return `<span class="tag validation">同类${sample}样本 3%:${hit3} 胜:${win} 均:${avg}</span>`;
}

function renderShortTermTable(rows) {
  if (!rows.length) {
    els.shortTermBody.innerHTML = '<tr><td colspan="16" class="empty">暂无符合条件的股票</td></tr>';
    return;
  }
  els.shortTermBody.innerHTML = rows.map(row => {
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
        <td class="num score">${formatNumber(row.score, 1)}</td>
        <td>${stabilityTag(row)}</td>
        <td class="reasons">${explanation}</td>
      </tr>
    `;
  }).join("");

  bindSentimentRows(els.shortTermBody);
}

function renderLongTermTable(rows) {
  if (!rows.length) {
    els.longTermBody.innerHTML = '<tr><td colspan="16" class="empty">暂无符合条件的股票</td></tr>';
    return;
  }
  els.longTermBody.innerHTML = rows.map(row => {
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
        <td class="num score">${formatNumber(row.score, 1)}</td>
        <td>${stabilityTag(row)}</td>
        <td class="reasons">${explanation}</td>
      </tr>
    `;
  }).join("");

  bindSentimentRows(els.longTermBody);
}

function renderTomorrowTable(rows) {
  if (!rows.length) {
    els.tomorrowBody.innerHTML = '<tr><td colspan="16" class="empty">暂无符合条件的股票</td></tr>';
    return;
  }
  els.tomorrowBody.innerHTML = rows.map(row => {
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
        <td class="num score">${formatNumber(row.score, 1)}</td>
        <td class="reasons">${explanation}</td>
      </tr>
    `;
  }).join("");

  bindSentimentRows(els.tomorrowBody);
}

function renderTechTable(rows) {
  if (!rows.length) {
    els.techBody.innerHTML = '<tr><td colspan="16" class="empty">暂无符合条件的科技潜力股票</td></tr>';
    return;
  }
  els.techBody.innerHTML = rows.map(row => {
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
        <td class="num score">${formatNumber(row.score, 1)}</td>
        <td class="reasons">${explanation}</td>
      </tr>
    `;
  }).join("");

  bindSentimentRows(els.techBody);
}

function renderSwingTable(rows) {
  if (!rows.length) {
    els.swingBody.innerHTML = '<tr><td colspan="16" class="empty">暂无符合条件的波段股票</td></tr>';
    return;
  }
  els.swingBody.innerHTML = rows.map(row => {
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
        <td class="num score">${formatNumber(row.score, 1)}</td>
        <td class="reasons">${explanation}</td>
      </tr>
    `;
  }).join("");
  bindSentimentRows(els.swingBody);
}

function renderPositionTable(rows) {
  if (!rows.length) {
    els.positionBody.innerHTML = '<tr><td colspan="16" class="empty">暂无符合条件的中长期股票</td></tr>';
    return;
  }
  els.positionBody.innerHTML = rows.map(row => {
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
        <td class="num score">${formatNumber(row.score, 1)}</td>
        <td class="reasons">${explanation}</td>
      </tr>
    `;
  }).join("");
  bindSentimentRows(els.positionBody);
}

function renderValidationMetrics(metrics) {
  els.validationSampleCount.textContent = metrics.sample_count ?? "-";
  els.validationWinRate.textContent = metrics.win_rate_next_close != null ? `${formatNumber(metrics.win_rate_next_close, 1)}%` : "-";
  els.validationHit3.textContent = metrics.hit_3pct_rate != null ? `${formatNumber(metrics.hit_3pct_rate, 1)}%` : "-";
  els.validationAvgReturn.textContent = metrics.avg_next_close_return != null ? `${formatNumber(metrics.avg_next_close_return, 2)}%` : "-";
}

function renderValidationDates(rows) {
  if (!rows.length) {
    els.validationDatesBody.innerHTML = '<tr><td colspan="4" class="empty">暂无保存记录</td></tr>';
    els.validationDetailBody.innerHTML = '<tr><td colspan="12" class="empty">暂无可查看明细</td></tr>';
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
    els.validationDetailBody.innerHTML = '<tr><td colspan="12" class="empty">暂无明细</td></tr>';
    return;
  }
  els.validationDetailBody.innerHTML = rows.map(row => {
    const reasons = (row.reasons || []).map(text => `<span class="tag">${escapeHtml(text)}</span>`).join("");
    const signalClose = row.signal_next_close_return ?? row.next_close_return;
    const signalHigh = row.signal_intraday_high_return ?? row.intraday_high_return;
    const signalHold = row.signal_hold_3d_return ?? row.hold_3d_return;
    const closeClass = Number(signalClose || 0) >= 0 ? "positive" : "negative";
    const openCloseClass = Number(row.next_close_return || 0) >= 0 ? "positive" : "negative";
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
        <td class="num">${signalHold == null ? "-" : `${formatNumber(signalHold, 2)}%`}</td>
        <td>${Number(row.signal_hit_3pct ?? row.hit_3pct ?? 0) ? "是" : "否"}</td>
        <td>${Number(row.signal_hit_5pct ?? row.hit_5pct ?? 0) ? "是" : "否"}</td>
        <td class="reasons">${reasons}</td>
      </tr>
    `;
  }).join("");
  bindSentimentRows(els.validationDetailBody);
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
  els.detailsPanel.hidden = false;
  els.detailsTitle.textContent = `${code} ${name} 舆情详情`;
  els.detailsSummary.textContent = "加载中...";
  els.newsList.innerHTML = "";
  try {
    const params = new URLSearchParams({ name });
    const res = await fetch(`/api/sentiment/${encodeURIComponent(code)}?${params.toString()}`);
    const payload = await res.json();
    if (!payload.ok) {
      throw new Error(payload.error || "舆情接口异常");
    }
    const sentiment = payload.sentiment || {};
    const triggers = (sentiment.trigger_words || []).join("、") || "无";
    els.detailsSummary.textContent = `舆情分 ${sentiment.score}；${sentiment.summary}；关键词：${triggers}`;
    const items = sentiment.items || [];
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
    els.detailsSummary.textContent = `舆情加载失败：${err.message}`;
  }
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

function stabilityTag(row) {
  const status = row.stability_status === "new" ? "新进" : "留存";
  const cls = row.stability_status === "new" ? "tag new" : "tag stable";
  const streak = Number(row.streak || 1);
  return `<span class="${cls}">${status} ${streak}</span>`;
}

function strategyLabel(value) {
  if (value === "tech_potential") return "科技潜力";
  if (value === "tomorrow_picks") return "明天预测";
  if (value === "swing_picks") return "波段 5-10 日";
  if (value === "position_picks") return "中长期 1-3 月";
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
els.marketSelect.addEventListener("change", () => {
  state.tomorrowLoaded = false;
  state.techLoaded = false;
  state.horizonLoaded = false;
  loadRecommendations();
  if (document.getElementById("tomorrowPanel").classList.contains("active")) {
    loadTomorrowPicks();
  }
  if (document.getElementById("techPanel").classList.contains("active")) {
    loadTechPotential();
  }
  if (document.getElementById("horizonPanel").classList.contains("active")) {
    loadHorizonPicks();
  }
});
els.tabButtons.forEach(button => {
  button.addEventListener("click", () => {
    els.tabButtons.forEach(item => item.classList.toggle("active", item === button));
    els.tabPanels.forEach(panel => panel.classList.toggle("active", panel.id === button.dataset.tab));
    if (button.dataset.tab === "tomorrowPanel" && !state.tomorrowLoaded) {
      loadTomorrowPicks();
    }
    if (button.dataset.tab === "techPanel" && !state.techLoaded) {
      loadTechPotential();
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
els.saveTechSnapshot.addEventListener("click", () => saveStrategySnapshot("tech_potential"));
els.saveTomorrowSnapshot.addEventListener("click", () => saveStrategySnapshot("tomorrow_picks"));
els.saveSwingSnapshot.addEventListener("click", () => saveStrategySnapshot("swing_picks"));
els.savePositionSnapshot.addEventListener("click", () => saveStrategySnapshot("position_picks"));
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
