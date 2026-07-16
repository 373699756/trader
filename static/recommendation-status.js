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
        renderDeepSeekStatus(meta);
        renderFactorCoverageStatus(health.factor_coverage || meta.factor_coverage || payload.factor_coverage);
        renderHardFilterStatus(meta.hard_filter_report);
        els.marketSentiment.textContent = marketSentiment.score ? `${marketSentiment.score}` : "-";
        renderRiskBlacklistStatus(meta.risk_blacklist || payload.risk_blacklist);
      }

      function renderDeepSeekStatus(meta) {
        const deepseek = meta.deepseek || {};
        const byStrategy = deepseek.by_strategy || {};
        const statusLabels = {
          precomputed: "已参与",
          cache_hit: "缓存参与",
          local_only: "仅本地策略",
          abstain: "无证据放弃",
          daily_call_limit: "额度用尽",
          deadline_skipped: "超时跳过",
          disabled: "已关闭",
          error: "API失败",
        };
        if (els.deepseekParticipation) {
          const applied = deepseek.production_applied === true;
          els.deepseekParticipation.textContent = applied ? "已参与综合评分" : (statusLabels[deepseek.status] || "仅本地策略");
          els.deepseekParticipation.dataset.level = applied ? "ok" : (deepseek.status === "error" ? "error" : "warn");
          els.deepseekParticipation.title = deepseek.reason || "DeepSeek不可绕过本地硬过滤";
        }
        if (els.deepseekWeight) {
          const weight = Number(deepseek.weight);
          els.deepseekWeight.textContent = Number.isFinite(weight) ? `${Math.round(weight * 100)}%` : "25%";
          els.deepseekWeight.dataset.level = deepseek.production_applied ? "ok" : "neutral";
        }
        if (els.deepseekApiCallCount) {
          const deepseekCalls = Number(deepseek.used ?? meta.deepseek_api_call_count);
          const remaining = Number(deepseek.remaining);
          const dailyLimit = Number(deepseek.daily_limit ?? 188);
          if (Number.isFinite(deepseekCalls)) {
            els.deepseekApiCallCount.textContent = `${deepseekCalls}/${Number.isFinite(remaining) ? remaining : Math.max(0, dailyLimit - deepseekCalls)}`;
            els.deepseekApiCallCount.dataset.level = deepseekCalls > 0 ? "ok" : "warn";
            els.deepseekApiCallCount.title = `今日DeepSeek API已用${deepseekCalls}次，硬上限${dailyLimit}次`;
          } else {
            els.deepseekApiCallCount.textContent = "-";
            els.deepseekApiCallCount.dataset.level = "neutral";
            els.deepseekApiCallCount.title = "";
          }
        }
        if (els.deepseekStrategyUsage) {
          const usage = deepseek.usage_by_strategy || {};
          const limits = { today_term: 70, tomorrow_picks: 45, swing_picks: 35, long_term_watch: 18 };
          const labels = { today_term: "今", tomorrow_picks: "明", swing_picks: "波", long_term_watch: "长" };
          els.deepseekStrategyUsage.textContent = Object.keys(limits)
            .map(key => `${labels[key]}${Number(usage[key] || 0)}/${limits[key]}`)
            .join(" · ");
          els.deepseekStrategyUsage.title = Object.entries(byStrategy)
            .map(([key, item]) => `${key}: ${item.reviewed || 0}/${item.requested || 0}`)
            .join("；");
        }
        if (els.deepseekCoverage) {
          const requested = Number(deepseek.requested || 0);
          const reviewed = Number(deepseek.reviewed || 0);
          const coverage = Number(deepseek.coverage_pct || 0);
          els.deepseekCoverage.textContent = `${reviewed}/${requested} · ${formatNumber(coverage, 1)}%`;
          els.deepseekCoverage.dataset.level = coverage >= 80 ? "ok" : reviewed > 0 ? "warn" : "neutral";
          els.deepseekCoverage.title = `弃权${Number(deepseek.abstain_count || 0)}，缓存命中${Number(deepseek.cache_hit_count || 0)}`;
        }
        if (els.todayExecutionPhase) {
          const phase = deepseek.today_phase || byStrategy.today_term?.today_phase || {};
          els.todayExecutionPhase.textContent = phase.label || "仅观察";
          els.todayExecutionPhase.dataset.level = phase.execution_allowed ? "ok" : "warn";
        }
        if (els.deepseekLastBatch) {
          const batch = String(deepseek.last_batch_id || "");
          const completedAt = String(deepseek.completed_at || "").replace("T", " ").slice(5, 16);
          els.deepseekLastBatch.textContent = batch ? `${batch.slice(-8)}${completedAt ? ` · ${completedAt}` : ""}` : "-";
          els.deepseekLastBatch.title = `${batch || "无批次"}${deepseek.completed_at ? ` · ${deepseek.completed_at}` : ""}`;
        }
        if (els.deepseekFailure) {
          const message = String(deepseek.error_message || "");
          const statusText = statusLabels[deepseek.status] || deepseek.status || "正常";
          const isError = deepseek.status === "error";
          const isApplied = deepseek.production_applied === true || ["precomputed", "cache_hit"].includes(deepseek.status);
          els.deepseekFailure.textContent = message || statusText;
          els.deepseekFailure.dataset.level = isError ? "error" : message ? "warn" : isApplied ? "ok" : "warn";
          els.deepseekFailure.title = `${deepseek.error_type || ""}${message ? `: ${message}` : ""}`;
        }
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
