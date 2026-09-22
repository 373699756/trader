(function () {
  "use strict";

  const STRATEGIES = new Set(["tomorrow", "d25", "long"]);
  const STAGE_LABELS = {
    refresh: "数据刷新",
    decision: "策略构建",
    review: "模型复核",
    publish: "结果发布",
    freeze: "快照冻结",
    settlement: "收盘结算",
    snapshot: "快照质量",
    runtime: "运行状态",
  };
  const ERROR_LABELS = {
    "refresh:source_unavailable": "行情刷新暂时不可用",
    "refresh:refresh_unavailable": "行情刷新暂时不可用",
    "decision:decision_unavailable": "策略结果暂时无法构建",
    "review:review_unavailable": "模型复核暂时不可用",
    "review:review_identity_mismatch": "模型复核身份不一致",
    "freeze:freeze_unavailable": "正式快照冻结失败",
    "freeze:close_fallback_unavailable": "收盘补算冻结失败",
    "freeze:freeze_capacity_rejected": "冻结任务队列已满",
    "settlement:settlement_unavailable": "收盘结算暂时不可用",
    "settlement:settlement_capacity_rejected": "结算任务队列已满",
    runtime_status_unavailable: "运行状态暂时不可用",
  };

  function healthView(statusPayload, snapshotReasons, strategy) {
    const payload = statusPayload && typeof statusPayload === "object" ? statusPayload : {};
    const runtimeIssues = Array.isArray(payload.recent_errors)
      ? payload.recent_errors.map(normalizeRuntimeIssue).filter(Boolean)
      : [];
    const snapshotIssues = Array.isArray(snapshotReasons)
      ? snapshotReasons.map((code) => normalizeSnapshotIssue(code, strategy)).filter(Boolean)
      : [];
    const issues = deduplicateIssues([...runtimeIssues, ...snapshotIssues]).sort(compareIssues);
    const activeIssues = issues.filter((issue) => issue.recoveryStatus === "active");
    const declaredCount = finiteNonNegativeInteger(payload.health && payload.health.issue_count);
    const issueCount = Math.max(activeIssues.length, declaredCount == null ? 0 : declaredCount);
    const declaredLevel = payload.health && payload.health.level;
    const level = declaredLevel === "error" || activeIssues.some((issue) => issue.severity === "error")
      ? "error"
      : declaredLevel === "degraded" || issueCount > 0
        ? "degraded"
        : "normal";
    return {
      level,
      issueCount,
      badge: level === "normal"
        ? issues.length ? "正常 · 最近已恢复" : "正常 · 无最近错误"
        : `${level === "error" ? "错误" : "降级"} · ${issueCount}项`,
      primary: activeIssues.length ? presentIssue(activeIssues[0]) : null,
      issues,
    };
  }

  function normalizeRuntimeIssue(raw) {
    if (!raw || typeof raw !== "object") return null;
    const code = cleanString(raw.code);
    if (!code) return null;
    return {
      code,
      severity: raw.severity === "error" ? "error" : "degraded",
      strategy: cleanString(raw.strategy),
      stage: cleanString(raw.stage) || stageFromCode(code),
      occurredAt: cleanString(raw.occurred_at),
      lastOccurredAt: cleanString(raw.last_occurred_at) || cleanString(raw.occurred_at),
      resolvedAt: cleanString(raw.resolved_at),
      count: finitePositiveInteger(raw.count) || 1,
      recoveryStatus: raw.recovery_status === "recovered" ? "recovered" : "active",
    };
  }

  function normalizeSnapshotIssue(rawCode, strategy) {
    const code = cleanString(rawCode);
    if (!code) return null;
    const split = splitStrategyPrefix(code, strategy);
    return {
      code: split.code,
      severity: "degraded",
      strategy: split.strategy,
      stage: "snapshot",
      occurredAt: null,
      lastOccurredAt: null,
      resolvedAt: null,
      count: 1,
      recoveryStatus: "active",
    };
  }

  function deduplicateIssues(values) {
    const result = new Map();
    values.forEach((issue) => {
      const key = `${issue.strategy || "system"}:${issue.code}`;
      const previous = result.get(key);
      if (!previous || compareIssues(issue, previous) < 0) result.set(key, issue);
    });
    return [...result.values()];
  }

  function compareIssues(left, right) {
    const severity = severityRank(left.severity) - severityRank(right.severity);
    if (severity) return severity;
    const recovery = recoveryRank(left.recoveryStatus) - recoveryRank(right.recoveryStatus);
    if (recovery) return recovery;
    return timestamp(right.lastOccurredAt) - timestamp(left.lastOccurredAt);
  }

  function presentIssue(issue) {
    const strategy = STRATEGIES.has(issue.strategy)
      ? window.TraderSelection?.strategyLabel(issue.strategy) || issue.strategy
      : issue.strategy ? "其他策略" : "系统";
    const stage = STAGE_LABELS[issue.stage] || "运行链路";
    const time = issue.lastOccurredAt ? formatTime(issue.lastOccurredAt) : "时间待确认";
    const recovery = issue.recoveryStatus === "recovered" ? "已恢复" : "处理中";
    return {
      ...issue,
      message: issueLabel(issue.code, issue.stage),
      meta: `${strategy} · ${stage} · ${time} · ${recovery}`,
    };
  }

  function runtimeErrorRows(issues) {
    if (!Array.isArray(issues) || issues.length === 0) {
      return '<div class="error-detail-empty">暂无错误记录</div>';
    }
    return issues.map((issue) => {
      const presented = presentIssue(issue);
      const status = issue.recoveryStatus === "recovered" ? "已恢复" : "活动中";
      const statusLevel = issue.recoveryStatus === "recovered" ? "recovered" : issue.severity;
      const count = issue.count > 1 ? ` · 累计 ${issue.count} 次` : "";
      const resolved = issue.resolvedAt ? ` · 恢复于 ${formatDateTime(issue.resolvedAt)}` : "";
      return `<article class="error-detail-item" data-level="${escapeHtml(statusLevel)}">
        <header><div><span class="error-state-tag">${escapeHtml(status)}</span><strong>${escapeHtml(presented.message)}</strong></div><time>${escapeHtml(formatDateTime(issue.lastOccurredAt))}</time></header>
        <p>${escapeHtml(presented.meta)}${escapeHtml(count)}${escapeHtml(resolved)}</p>
        <div class="error-code-row"><code>${escapeHtml(issue.code)}</code><button type="button" data-copy-code="${escapeHtml(issue.code)}">复制代码</button></div>
      </article>`;
    }).join("");
  }

  function issueSummaryTitle(issues) {
    const values = Array.isArray(issues) ? issues : [];
    const active = values.filter((issue) => issue && issue.recoveryStatus !== "recovered").length;
    const recovered = values.length - active;
    if (active && recovered) return `最近错误 · 活动${active}项 / 已恢复${recovered}项`;
    if (active) return `最近错误 · 活动${active}项`;
    if (recovered) return `最近错误 · 已恢复${recovered}项`;
    return "最近错误";
  }

  function issueLabel(code, stage) {
    if (ERROR_LABELS[code]) return ERROR_LABELS[code];
    if (window.TraderRender && typeof window.TraderRender.reasonLabel === "function") {
      const label = window.TraderRender.reasonLabel(code);
      if (label !== "部分数据暂不可用") return label;
    }
    return ({
      refresh: "行情刷新暂时不可用",
      decision: "策略结果暂时无法构建",
      review: "模型复核暂时不可用",
      publish: "结果发布发生错误",
      freeze: "正式快照冻结发生错误",
      settlement: "收盘结算发生错误",
      snapshot: "快照数据质量降级",
    })[stage] || "运行链路暂时降级";
  }

  function splitStrategyPrefix(code, fallbackStrategy) {
    const separator = code.indexOf(":");
    if (separator > 0) {
      const prefix = code.slice(0, separator);
      if (STRATEGIES.has(prefix)) return { strategy: prefix, code: code.slice(separator + 1) };
    }
    return { strategy: cleanString(fallbackStrategy), code };
  }

  function stageFromCode(code) {
    const prefix = code.split(":", 1)[0];
    return STAGE_LABELS[prefix] ? prefix : "runtime";
  }

  function formatDateTime(value) {
    const parsed = new Date(value || "");
    return Number.isFinite(parsed.getTime())
      ? parsed.toLocaleString("zh-CN", { hour12: false })
      : "时间待确认";
  }

  function formatTime(value) {
    const parsed = new Date(value || "");
    return Number.isFinite(parsed.getTime())
      ? parsed.toLocaleTimeString("zh-CN", { hour12: false })
      : "时间待确认";
  }

  function cleanString(value) {
    if (typeof value !== "string") return null;
    const cleaned = value.trim();
    return cleaned ? cleaned.slice(0, 128) : null;
  }

  function finitePositiveInteger(value) {
    const parsed = Number(value);
    return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
  }

  function finiteNonNegativeInteger(value) {
    if (value == null || value === "" || typeof value === "boolean") return null;
    const parsed = Number(value);
    return Number.isInteger(parsed) && parsed >= 0 ? parsed : null;
  }

  function severityRank(value) {
    return value === "error" ? 0 : 1;
  }

  function recoveryRank(value) {
    return value === "active" ? 0 : 1;
  }

  function timestamp(value) {
    const parsed = new Date(value || "").getTime();
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  window.TraderStatusHealth = Object.freeze({
    healthView,
    issueSummaryTitle,
    presentIssue,
    runtimeErrorRows,
  });
})();
