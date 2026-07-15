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
        el.parentElement?.classList.toggle("has-status", Boolean(String(text || "").trim()));
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
        if (els.deepseekApiCallCount) {
          const deepseekCalls = Number(meta.deepseek_api_call_count);
          if (Number.isFinite(deepseekCalls)) {
            els.deepseekApiCallCount.textContent = `${deepseekCalls}次`;
            els.deepseekApiCallCount.dataset.level = deepseekCalls > 0 ? "ok" : "warn";
            els.deepseekApiCallCount.title = `今日DeepSeek API实际调用${deepseekCalls}次`;
          } else {
            els.deepseekApiCallCount.textContent = "-";
            els.deepseekApiCallCount.dataset.level = "neutral";
            els.deepseekApiCallCount.title = "";
          }
        }
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
              title = "历史因子未开启，明日与2-5日的历史类因子不会参与打分。";
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

      function normalizeStatusTime(value) {
        return String(value || "")
          .replace(/^\s+|\s+$/g, "")
          .replace(/[T]/g, " ")
          .replace(/\.[0-9]+(?:Z)?$/i, "")
          .replace(/\s+/g, " ")
          .trim();
      }

      let lastStatusValue = "";

      function setStatus(text) {
        const value = typeof text === "string" ? text : String(text || "等待刷新");
        const statusValue = value.trim();
        if (!statusValue) {
          if (lastStatusValue === "等待刷新") return;
          lastStatusValue = "等待刷新";
        } else if (statusValue === lastStatusValue) {
          return;
        } else {
          lastStatusValue = statusValue;
        }
        let statusTag = "状态";
        let quoteText = "-";
        let rankText = "-";

        if (!statusValue) {
          statusTag = "等待刷新";
          quoteText = "等待刷新";
        } else if (/^\s*已加载快照\b/.test(statusValue)) {
          statusTag = "行情更新";
          const loadedMatch = statusValue.match(/^\s*已加载快照\s+([^\s，,。·•]+)/);
          if (loadedMatch) {
            quoteText = normalizeStatusTime(loadedMatch[1]);
          }
        } else if (/^\s*行情更新\b/.test(statusValue)) {
          statusTag = "行情更新";
          const fullMatch = statusValue.match(/^\s*行情更新\s+([^\s，,。·•]+)\s*[·•]\s*排名\s*([^\s，,。·•]+)/);
          if (fullMatch) {
            quoteText = normalizeStatusTime(fullMatch[1]);
            rankText = normalizeStatusTime(fullMatch[2]);
          } else {
            const quoteMatch = statusValue.match(/行情更新\s+([0-9T:\.-]+)/);
            if (quoteMatch) {
              quoteText = normalizeStatusTime(quoteMatch[1]);
            }
            const rankMatch = statusValue.match(/排名\s*([0-9T:\.-]+)/);
            if (rankMatch) {
              rankText = normalizeStatusTime(rankMatch[1]);
            }
          }
        } else {
          const genericMatch = statusValue.match(/^\s*(.+?)\s+([0-9T:\.-]+)(?:\s*[·•]\s*排名\s*([0-9T:\.-]+))?(?:\s*[·•].*)?$/);
          if (genericMatch) {
            statusTag = genericMatch[1].trim();
            quoteText = normalizeStatusTime(genericMatch[2]);
            rankText = normalizeStatusTime(genericMatch[3] || "-");
          } else {
            statusTag = statusValue;
          }
        }

        if (quoteText && rankText && normalizeStatusTime(quoteText) === normalizeStatusTime(rankText)) {
          rankText = "-";
        }

        if (els.statusTag) {
          els.statusTag.textContent = statusTag || "状态";
          els.statusTag.title = statusTag || "";
        }
        if (els.statusQuoteTime) {
          els.statusQuoteTime.textContent = quoteText;
          els.statusQuoteTime.title = quoteText || "";
        }
        if (els.statusRank) {
          els.statusRank.textContent = rankText;
          els.statusRank.title = rankText || "";
        }
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
