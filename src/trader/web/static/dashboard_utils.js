(function () {
  "use strict";

  function latencySummary(values) {
    if (!Array.isArray(values) || values.length === 0) {
      return { sample_count: 0, p50_ms: null, p95_ms: null, maximum_ms: null };
    }
    const ordered = values.filter((value) => Number.isFinite(value) && value >= 0).sort((left, right) => left - right);
    if (ordered.length === 0) {
      return { sample_count: 0, p50_ms: null, p95_ms: null, maximum_ms: null };
    }
    const rank = (probability) => ordered[Math.max(0, Math.ceil(ordered.length * probability) - 1)];
    return {
      sample_count: ordered.length,
      p50_ms: Number(rank(0.50).toFixed(3)),
      p95_ms: Number(rank(0.95).toFixed(3)),
      maximum_ms: Number(ordered[ordered.length - 1].toFixed(3)),
    };
  }

  function utf8Bytes(value) {
    return new TextEncoder().encode(String(value || "")).byteLength;
  }

  function quotedEtag(value) {
    return value.startsWith('"') ? value : `"${value}"`;
  }

  function phaseLabel(value) {
    return ({
      closed: "休市",
      warmup: "共享预热",
      today_observe: "今早观察",
      today_main: "今早主执行",
      today_late: "今早降级执行",
      midday: "午间暂停",
      afternoon: "午后主审",
      final_review: "最终补审",
      deepseek_cutoff: "模型截止",
      final_quote: "最终报价",
      frozen: "冻结窗口",
      close_fallback: "收盘恢复中",
      after_close: "收盘后",
    })[value] || value;
  }

  window.TraderDashboardUtils = Object.freeze({
    latencySummary,
    phaseLabel,
    quotedEtag,
    utf8Bytes,
  });
})();
