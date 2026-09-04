(function () {
  "use strict";

  const STREAM_RETRY_INITIAL_MS = 1000;
  const STREAM_RETRY_MAX_MS = 15000;
  const FALLBACK_POLL_MS = 3000;
  const PATCH_LATENCY_SAMPLE_CAPACITY = 256;

  function create(dependencies) {
    const {
      state, els, patches, formatters, diagnostics, patchToPaintSamples,
      loadRecommendations, loadStatus, applyRecommendationPatch, applyOverlayPatch,
      requestRecommendationResync, recordBrowserError,
    } = dependencies;

    function rememberEvent(event) {
      const parsed = Number(event.lastEventId);
      if (Number.isInteger(parsed) && parsed >= 0) state.lastEventId = parsed;
    }

    function eventPayload(event) {
      try {
        return JSON.parse(event.data || "{}");
      } catch (error) {
        recordBrowserError("event_json", error && error.message);
        requestRecommendationResync("event_schema_mismatch");
        return null;
      }
    }

    function eventMatchesCurrent(payload) {
      const currentDate = state.payload && (state.payload.current_trade_date || state.payload.trade_date);
      return patches.eventMatchesCurrent(payload, state.strategy, currentDate);
    }

    function connect() {
      if (state.stream) state.stream.close();
      if (state.releaseMismatch) {
        state.stream = null;
        els.streamStatus.textContent = "已阻止";
        return;
      }
      const query = state.lastEventId > 0 ? `?cursor=${state.lastEventId}` : "";
      const stream = new EventSource(`/api/events${query}`);
      state.stream = stream;
      els.streamStatus.textContent = "连接中";
      stream.onopen = () => {
        els.streamStatus.textContent = "实时";
        stopPolling();
        if (state.streamRetry) window.clearTimeout(state.streamRetry);
        state.streamRetry = null;
        state.streamRetryDelayMs = STREAM_RETRY_INITIAL_MS;
      };
      const refreshDecision = (event) => {
        const receivedAt = performance.now();
        rememberEvent(event);
        diagnostics.incrementalSseBytes += formatters.utf8Bytes(event.data || "");
        const payload = eventPayload(event);
        if (!state.date && eventMatchesCurrent(payload) && applyRecommendationPatch(payload)) {
          recordPatchPaint(receivedAt);
        }
      };
      const refreshOverlay = (event) => {
        const receivedAt = performance.now();
        rememberEvent(event);
        diagnostics.incrementalSseBytes += formatters.utf8Bytes(event.data || "");
        const payload = eventPayload(event);
        if (!state.date && eventMatchesCurrent(payload) && applyOverlayPatch(payload)) recordPatchPaint(receivedAt);
      };
      stream.addEventListener("decision", refreshDecision);
      stream.addEventListener("overlay", refreshOverlay);
      stream.addEventListener("resync_required", (event) => {
        rememberEvent(event);
        if (!state.date) requestRecommendationResync("server_resync");
      });
      stream.onerror = () => {
        stream.close();
        if (state.stream === stream) state.stream = null;
        els.streamStatus.textContent = "轮询";
        startPolling();
        if (!state.date && !state.releaseMismatch) {
          loadStatus().finally(() => {
            if (!state.date && !state.releaseMismatch) loadRecommendations("stream_disconnect");
          });
        }
        if (state.streamRetry) window.clearTimeout(state.streamRetry);
        const retryDelay = state.streamRetryDelayMs;
        state.streamRetryDelayMs = Math.min(STREAM_RETRY_MAX_MS, retryDelay * 2);
        state.streamRetry = window.setTimeout(() => {
          state.streamRetry = null;
          connect();
        }, retryDelay);
      };
    }

    function recordPatchPaint(receivedAt) {
      window.requestAnimationFrame(() => {
        const elapsed = Math.max(0, performance.now() - receivedAt);
        if (patchToPaintSamples.length >= PATCH_LATENCY_SAMPLE_CAPACITY) {
          patchToPaintSamples.shift();
          diagnostics.patchToPaintDroppedSamples += 1;
        }
        patchToPaintSamples.push(elapsed);
      });
    }

    function startPolling() {
      if (state.pollTimer) return;
      state.pollTimer = window.setInterval(() => {
        loadStatus().then((status) => {
          if (status && !state.date && !state.releaseMismatch) loadRecommendations("poll");
        });
      }, FALLBACK_POLL_MS);
    }

    function stopPolling() {
      if (!state.pollTimer) return;
      window.clearInterval(state.pollTimer);
      state.pollTimer = null;
    }

    return Object.freeze({ connect });
  }

  window.TraderDashboardStream = Object.freeze({ create });
})();
