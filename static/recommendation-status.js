(function () {
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

  window.TraderStatusRefresh = {
    create(context) {
      const { state, els, helpers } = context;
      const { formatNumber } = helpers;

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

      function setOpsStatus(el, text, level) {
        if (!el) return;
        el.textContent = text;
        el.className = "ops-status" + (level ? ` ops-${level}` : "");
      }

      function renderToolResult(html) {
        if (!els.toolResultPane) return;
        els.toolResultPane.innerHTML = html;
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
            title = "历史因子未开启，明日优先和2-5日持有的历史类因子不会参与打分。";
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

      function startPushStatusCountdown() {
        clearInterval(state.timer);
        state.timer = null;
      }

      function setStatus(text) {
        els.statusText.textContent = text;
      }

      function resizeCharts() {
        Object.values(state.charts).forEach((chart) => chart && !chart.isDisposed?.() && chart.resize());
      }

      function resizeVisibleCharts() {
        Object.values(state.charts).forEach((chart) => {
          if (chart && !chart.isDisposed?.() && chart.getDom?.()?.offsetParent !== null) {
            chart.resize();
          }
        });
      }

      function registerChartResize() {
        window.addEventListener("resize", resizeCharts);
      }

      return {
        chartTheme: CHART_THEME,
        registerChartResize,
        renderChart,
        renderMetrics,
        renderToolResult,
        resizeCharts,
        resizeVisibleCharts,
        setOpsStatus,
        setStatus,
        startPushStatusCountdown,
      };
    },
  };
})();
