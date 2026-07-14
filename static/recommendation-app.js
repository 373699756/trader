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

      function payloadMarketTimestamp(payload) {
        const values = [
          payload.meta?.quote_timestamp,
          payload.meta?.data_source_timestamp,
          payload.health?.last_quote_refresh,
          payload.meta?.generated_at,
          payload.snapshot?.saved_at,
        ];
        for (const value of values) {
          const timestamp = Date.parse(String(value || ""));
          if (Number.isFinite(timestamp)) return timestamp;
        }
        return 0;
      }

      function applyRecommendationsPayload(payload) {
        if (!payload.ok) {
          throw new Error(payload.error || "接口返回异常");
        }
        const marketTimestamp = payloadMarketTimestamp(payload);
        if (marketTimestamp && state.recommendationDataTimestamp && marketTimestamp < state.recommendationDataTimestamp) {
          return false;
        }
        const recommendations = payload.recommendations || {};
        const shortTerm = recommendations.short_term || payload.data || [];
        const hasTomorrow = Object.prototype.hasOwnProperty.call(recommendations, "tomorrow_picks");
        const hasSwing = Object.prototype.hasOwnProperty.call(recommendations, "swing_picks");
        const tomorrow = hasTomorrow ? (recommendations.tomorrow_picks || []) : state.lastRows.tomorrow;
        const swing = hasSwing ? (recommendations.swing_picks || []) : state.lastRows.swing;
        const marketRegime = payload.meta?.market_regime || {};
        const shouldRenderTables = rememberFingerprint("recommendations", {
          shortTerm,
          tomorrow,
          swing,
          marketRegime,
        });
        state.lastRows.shortTerm = shortTerm;
        state.lastRows.tomorrow = tomorrow;
        state.lastRows.swing = swing;
        state.tomorrowLoaded = state.tomorrowLoaded || hasTomorrow;
        state.horizonLoaded = state.horizonLoaded || hasSwing;
        state.recommendationHasPayload = true;
        if (marketTimestamp) state.recommendationDataTimestamp = marketTimestamp;
        state.marketRegime = marketRegime;
        renderMetrics(payload);
        if (shouldRenderTables) {
          rerenderCurrentTables();
        }
        if (!hasTomorrow || !hasSwing) {
          prefetchRecommendationPools();
        }
        if (shouldRenderTables) {
          const quoteAt = payload.meta?.quote_timestamp || payload.health?.last_quote_refresh;
          const generatedAt = payload.meta?.generated_at || "最近快照";
          setStatus(quoteAt ? `行情更新 ${quoteAt} · 排名 ${generatedAt}` : `推荐更新 ${generatedAt}`);
        }
        return true;
      }

      async function loadRecommendations(options = {}) {
        const requestSeq = options.requestSeq || ++state.recommendationRequestSeq;
        const background = Boolean(options.background);
        if (!background) setStatus("刷新中...");
        const params = new URLSearchParams({
          top_n: String(window.APP_CONFIG.defaultTopN || 18),
          market: DEFAULT_MARKET,
          _: String(Date.now()),
        });
        try {
          const res = await fetch(`/api/recommendations?${params.toString()}`, { cache: "no-store" });
          const payload = await res.json();
          if (requestSeq !== state.recommendationRequestSeq) return false;
          return applyRecommendationsPayload(payload);
        } catch (err) {
          if (requestSeq !== state.recommendationRequestSeq) return false;
          if (!state.recommendationHasPayload) {
            const message = `<tr><td colspan="17" class="empty">${escapeHtml(err.message)}</td></tr>`;
            els.shortTermBody.innerHTML = message;
          }
          if (!background) setStatus(`刷新失败：${err.message}`);
          return false;
        }
      }

      async function loadLatestRecommendationSnapshot(requestSeq) {
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
          if (requestSeq !== state.recommendationRequestSeq) return false;
          if (!applyRecommendationsPayload(payload)) return false;
          const savedAt = payload.snapshot?.saved_at || payload.meta?.generated_at || "最近快照";
          setStatus(`已加载快照 ${savedAt}，正在拉取最新行情...`);
          return true;
        } catch (err) {
          return false;
        }
      }

      function stopRecommendationStream(options = {}) {
        if (options.invalidate !== false) state.recommendationRequestSeq += 1;
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
        stopRecommendationStream({ invalidate: false });
        state.countdown = window.APP_CONFIG.refreshSeconds;
        setStatus("正在连接实时推荐流...");
        startPushStatusCountdown();
        state.timer = setInterval(() => {
          void loadRecommendations({ background: true });
        }, Math.max(60, window.APP_CONFIG.refreshSeconds || 60) * 1000);
        if (!("EventSource" in window)) {
          setStatus("浏览器不支持实时推送，已使用60秒轮询");
          return;
        }
        const params = new URLSearchParams({
          top_n: String(window.APP_CONFIG.defaultTopN || 18),
          market: DEFAULT_MARKET,
        });
        const eventSource = new EventSource(`/api/recommendations/stream?${params.toString()}`);
        state.eventSource = eventSource;
        eventSource.onopen = () => {
          if (state.eventSource !== eventSource) return;
          if (els.streamStatus) {
            els.streamStatus.textContent = "实时";
            els.streamStatus.dataset.state = "live";
          }
        };
        eventSource.addEventListener("recommendations", event => {
          if (state.eventSource !== eventSource) return;
          try {
            const payload = JSON.parse(event.data);
            if (!applyRecommendationsPayload(payload)) return;
            const quoteAt = payload.meta?.quote_timestamp || payload.health?.last_quote_refresh || "";
            if (els.streamQuoteTime) els.streamQuoteTime.textContent = String(quoteAt).replace("T", " ");
            if (els.streamStatus) {
              const now = new Date();
              const minutes = now.getHours() * 60 + now.getMinutes();
              const marketOpen = now.getDay() >= 1 && now.getDay() <= 5
                && ((minutes >= 555 && minutes <= 695) || (minutes >= 780 && minutes <= 910));
              const quoteAge = Number(payload.meta?.quote_p95_age_seconds ?? payload.meta?.quote_age_seconds);
              const staleCount = Number(payload.meta?.quote_stale_count || 0);
              const missingCount = Number(payload.meta?.quote_missing_count || 0);
              const mismatchCount = Number(payload.meta?.quote_source_mismatch_count || 0);
              const poolLive = payload.meta?.quote_scope === "recommendation_pool";
              if (!marketOpen && poolLive) {
                els.streamStatus.textContent = "休市最新";
                els.streamStatus.dataset.state = "live";
              } else if (poolLive && staleCount === 0 && missingCount === 0 && mismatchCount === 0
                && Number.isFinite(quoteAge) && quoteAge <= 30) {
                els.streamStatus.textContent = "实时";
                els.streamStatus.dataset.state = "live";
              } else {
                const issueCount = staleCount + missingCount + mismatchCount;
                els.streamStatus.textContent = issueCount > 0
                  ? `${issueCount}只异常`
                  : (Number.isFinite(quoteAge) ? `延迟 ${Math.round(quoteAge)}s` : "回退");
                els.streamStatus.dataset.state = "delayed";
              }
            }
          } catch (err) {
            setStatus(`实时数据异常: ${err.message}`);
          }
        });
        eventSource.onerror = () => {
          if (state.eventSource !== eventSource || !els.streamStatus) return;
          els.streamStatus.textContent = "重连中";
          els.streamStatus.dataset.state = "connecting";
        };
      }

      async function startRecommendationStreamWithSnapshot() {
        stopRecommendationStream();
        const requestSeq = ++state.recommendationRequestSeq;
        if (!state.recommendationHasPayload) {
          await loadLatestRecommendationSnapshot(requestSeq);
        }
        if (requestSeq !== state.recommendationRequestSeq) return;
        await loadRecommendations({ requestSeq, background: state.recommendationHasPayload });
        if (requestSeq !== state.recommendationRequestSeq) return;
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
            setStatus("明日优先已显示；证据特征由盘中后台任务预计算");
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
            const marketTimestamp = payloadMarketTimestamp(payload);
            if (marketTimestamp && state.recommendationDataTimestamp && marketTimestamp < state.recommendationDataTimestamp) {
              return;
            }
            const rows = payload.data || [];
            const shouldRender = rememberFingerprint("tomorrow", { rows, meta: payload.meta || {} });
            state.lastRows.tomorrow = rows;
            if (marketTimestamp) state.recommendationDataTimestamp = marketTimestamp;
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
            const marketTimestamp = payloadMarketTimestamp(swingPayload);
            if (marketTimestamp && state.recommendationDataTimestamp && marketTimestamp < state.recommendationDataTimestamp) {
              return;
            }
            const swingRows = swingPayload.data || [];
            const shouldRenderSwing = rememberFingerprint("swing", swingRows);
            state.lastRows.swing = swingRows;
            if (marketTimestamp) state.recommendationDataTimestamp = marketTimestamp;
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
