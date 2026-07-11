(function () {
  window.TraderRecommendationApp = {
    create(context) {
      const { state, els, helpers, config, status } = context;
      const { DEFAULT_ACTION_FILTER, DEFAULT_MARKET, DEFAULT_SORT_MODE } = config;
      const { escapeHtml, formatMoney, formatNumber, hasRows, rememberFingerprint } = helpers;
      const { renderMetrics, setStatus, startPushStatusCountdown } = status;
      const RecommendationUtils = window.TraderRecommendationUtils;
      const RecommendationRenderers = window.TraderRecommendationRenderers;
      const RecommendationTables = window.TraderRecommendationTables;

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
        if (state.timer) {
          clearInterval(state.timer);
          state.timer = null;
        }
      }

      function connectRecommendationStream() {
        stopRecommendationStream();
        state.countdown = window.APP_CONFIG.refreshSeconds;
        setStatus("已启用定时刷新，等待数据...");
        startPushStatusCountdown();
        state.timer = setInterval(() => {
          void loadRecommendations();
        }, Math.max(5, window.APP_CONFIG.refreshSeconds || 30) * 1000);
      }

      async function startRecommendationStreamWithSnapshot() {
        const loadedSnapshot = await loadLatestRecommendationSnapshot();
        if (!loadedSnapshot) {
          await loadRecommendations();
        }
        prefetchRecommendationPools();
        connectRecommendationStream();
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
            setStatus("明日优先已显示，后台用 DeepSeek 刷新中...");
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
              setStatus(`明日优先更新时间 ${payload.meta.generated_at || "最近快照"}`);
            }
          } catch (err) {
            state.tomorrowLoaded = false;
            if (!background || !hasRows(state.lastRows.tomorrow)) {
              els.tomorrowBody.innerHTML = `<tr><td colspan="17" class="empty">${escapeHtml(err.message)}</td></tr>`;
            }
            if (!background) {
              setStatus(`明日优先加载失败：${err.message}`);
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
              setStatus(`2-5日持有更新时间 ${swingPayload.meta.generated_at}`);
            }
          } catch (err) {
            state.horizonLoaded = false;
            if (!background || !hasRows(state.lastRows.swing)) {
              els.swingBody.innerHTML = `<tr><td colspan="17" class="empty">${escapeHtml(err.message)}</td></tr>`;
            }
            if (!background) {
              setStatus(`2-5日持有加载失败：${err.message}`);
            }
          } finally {
            state.horizonLoading = null;
          }
        })();
        return state.horizonLoading;
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
          els.swingBody.innerHTML = '<tr><td colspan="17" class="empty">暂无符合条件的2-5日持有股票</td></tr>';
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

      function selectRecommendationPool(filter) {
        applyRecommendationPoolFilter(filter);
        ensureRecommendationPoolData();
      }

      return {
        applyRecommendationPoolFilter,
        ensureRecommendationPoolData,
        loadRecommendations,
        prefetchRecommendationPools,
        selectRecommendationPool,
        startRecommendationStreamWithSnapshot,
        stopRecommendationStream,
      };
    },
  };
})();
