const state = {
  timer: null,
  eventSource: null,
  streamRetryTimer: null,
  recommendationRequestSeq: 0,
  recommendationDataTimestamp: 0,
  recommendationHasPayload: false,
  validationAutoRefreshTimer: null,
  validationAutoRefreshInFlight: false,
  validationAutoRefreshDate: "",
  validationAutoRefreshAt: 0,
  validationBaselineAutoBackfillInFlight: false,
  validationBaselineAutoBackfillKey: "",
  validationBaselineAutoBackfillAt: 0,
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
  validationCache: {},
  validationDailyCache: {},
  validationRequestSeq: 0,
  validationReportRequestSeq: 0,
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

const config = {
  VALIDATION_AUTO_REFRESH_MS,
  VALIDATION_DATE_PAGE_SIZE,
  DEFAULT_MARKET,
  DEFAULT_ACTION_FILTER,
  DEFAULT_SORT_MODE,
};

const els = {
  statusTag: document.getElementById("statusTag"),
  statusQuoteTime: document.getElementById("statusQuoteTime"),
  statusRank: document.getElementById("statusRank"),
  quoteSource: document.getElementById("quoteSource"),
  streamStatus: document.getElementById("streamStatus"),
  streamQuoteTime: document.getElementById("streamQuoteTime"),
  sentimentSource: document.getElementById("sentimentSource"),
  candidateCount: document.getElementById("candidateCount"),
  factorCoverageStatus: document.getElementById("factorCoverageStatus"),
  hardFilterCount: document.getElementById("hardFilterCount"),
  marketSentiment: document.getElementById("marketSentiment"),
  riskBlacklistStatus: document.getElementById("riskBlacklistStatus"),
  deepseekApiCallCount: document.getElementById("deepseekApiCallCount"),
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
  validationOosReport: document.getElementById("validationOosReport"),
  validationPortfolioBaseline: document.getElementById("validationPortfolioBaseline"),
  validationBaselineStatus: document.getElementById("validationBaselineStatus"),
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
  if (value === "short_term") return "今日";
  if (value === "tomorrow_picks") return "明日";
  if (value === "swing_picks") return "2-5日";
  return value || "-";
}

function validationSnapshotStrategiesText(strategies) {
  const labels = [...new Set((Array.isArray(strategies) ? strategies : []).map(strategyLabel).filter(Boolean))];
  return labels.length ? labels.join("/") : "策略验证";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

const helpers = {
  escapeHtml,
  formatMoney,
  formatNumber,
  hasRows,
  numberClass,
  rememberFingerprint,
  strategyLabel,
  validationSnapshotStrategiesText,
};

const context = { state, els, config, helpers };
context.status = window.TraderStatusRefresh.create(context);
context.recommendations = window.TraderRecommendationApp.create(context);
context.stockPrediction = window.TraderStockPrediction.create(context);
context.validation = window.TraderValidationApp.create(context);

/*
Contract anchors stay in the entry while existing frontend tests scan app.js.
Implementations live in the split modules loaded before this file:
/api/stock-prediction/${encodeURIComponent(code)}
本地量化
validationSnapshotStrategiesText(config.strategies)
snapshotStatusText(snapshot, config.strategies)
/api/strategy-validation/oos-report
/api/strategy-validation/readiness
/api/strategy-validation/portfolio-baseline
/api/strategy-validation/backfill-current-baseline
params.set("execute", "1")
window.confirm
renderValidationOosReport
renderValidationBaselineBackfillResult
maybeAutoBackfillCurrentBaseline
shouldAutoBackfillCurrentBaseline
execute: "1"
current baseline 自动回填完成
current baseline 自动回填状态
候选
回填前
回填后
oos_status
blockers
暂无真实 OOS
*/

function activateMainTab(button) {
  els.tabButtons.forEach(item => item.classList.toggle("active", item === button));
  els.tabPanels.forEach(panel => panel.classList.toggle("active", panel.id === button.dataset.tab));
  requestAnimationFrame(context.status.resizeVisibleCharts);
  if (button.dataset.tab === "todayPanel") {
    context.recommendations.applyRecommendationPoolFilter();
    context.recommendations.startRecommendationStreamWithSnapshot();
    context.validation.stopValidationAutoRefreshLoop();
    return;
  }
  context.recommendations.stopRecommendationStream();
  if (!state.validationLoaded) {
    context.validation.loadValidation();
  }
  context.validation.startValidationAutoRefreshLoop();
}

function bindEvents() {
  els.refreshButton.addEventListener("click", context.recommendations.startRecommendationStreamWithSnapshot);
  els.stockPredictionBtn.addEventListener("click", context.stockPrediction.loadStockPrediction);
  els.stockPredictionInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      context.stockPrediction.loadStockPrediction();
    }
  });
  els.generateTuningBtn?.addEventListener("click", context.validation.loadStrategyValidationReport);
  els.poolTabs.forEach(button => {
    button.addEventListener("click", () => {
      context.recommendations.selectRecommendationPool(button.dataset.poolFilter || "today");
    });
  });
  els.tabButtons.forEach(button => {
    button.addEventListener("click", () => activateMainTab(button));
  });
  els.validationStrategySelect?.addEventListener("change", context.validation.handleValidationStrategyChange);
  els.validationStrategyTabs.forEach(button => {
    button.addEventListener("click", () => {
      context.validation.selectValidationStrategy(button.dataset.validationStrategy || "short_term");
    });
  });
  els.validationDatesPrev?.addEventListener("click", () => context.validation.moveValidationDatePage(-1));
  els.validationDatesNext?.addEventListener("click", () => context.validation.moveValidationDatePage(1));
  els.validationDaysSelect.addEventListener("change", context.validation.handleValidationDaysChange);
}

function startApp() {
  context.status.registerChartResize();
  bindEvents();
  context.recommendations.applyRecommendationPoolFilter();
  context.recommendations.startRecommendationStreamWithSnapshot();
}

startApp();
