(function () {
  "use strict";

  const STRATEGIES = new Set(["tomorrow", "d25", "long"]);
  const {
    healthView,
    issueBelongsToStage,
    issueSummaryTitle,
    presentIssue,
    runtimeErrorRows,
  } = window.TraderStatusHealth;

  function createDashboardStateRenderer(els, state, selection, render) {
    const renderTableState = (message, columns, body) => {
      (body || els.tableBody).innerHTML = `<tr><td class="table-state" colspan="${columns || 10}">${render.escapeHtml(message)}</td></tr>`;
    };
    const setLongLayout = (enabled) => {
      if (els.resultLayout) els.resultLayout.classList.toggle("is-long", Boolean(enabled));
    };
    const setLongControls = (enabled) => {
      if (els.longScopeTabs) els.longScopeTabs.hidden = !enabled;
    };
    const setNotice = (message, level) => {
      // Recommendation conclusions are rendered with the list and funnel, not as runtime errors.
    };
    const renderLoadingState = () => {
      if (els.outcomeSummary) els.outcomeSummary.hidden = true;
      els.sixSeatStatus.textContent = "待评分";
      els.sixSeatMeta.textContent = "推荐 — · 关注 — · 回避 —";
      els.inputQualityStatus.textContent = "-";
      els.inputQualityMeta.textContent = "正在读取评分输入质量";
      els.inputQualityBlockers.textContent = "本轮阻断：待计算";
      els.inputQualityDegradations.textContent = "仅降级，不代表股票存在风险：待计算";
      els.inputQualityStages.textContent = "正在读取评分输入链路";
      els.funnelStatus.textContent = "等待评分输入";
      els.funnelStages.textContent = "输入、过滤、评分与发布按顺序执行";
      els.funnelMeta.textContent = emptyFunnelSummary();
      renderObservationStages(els, null, null, state.strategy === "long", []);
      els.quoteTime.textContent = "-";
      els.quoteAge.textContent = "-";
      els.quoteSource.textContent = "行情待确认";
      els.inputQualityStrategy.textContent = selection.strategyLabel(state.strategy);
      els.inputQualityScoreTime.textContent = "等待本轮评分完成";
      els.publicationStatus.textContent = "未就绪";
      els.publicationMeta.textContent = "等待当前策略快照";
      els.budgetStatus.textContent = "—";
      els.budgetMeta.textContent = "等待当前策略预算状态";
      els.recommendationTable.classList.remove("is-history", "is-anchor-table", "is-long-table");
      setLongControls(state.strategy === "long");
      setLongLayout(false);
      const definition = render.currentTable();
      els.tableColumns.innerHTML = definition.columns;
      els.tableHead.innerHTML = definition.head;
      if (els.longSidebar) els.longSidebar.hidden = true;
      if (els.longStockHeader) els.longStockHeader.hidden = true;
      renderTableState("正在读取推荐快照");
      setNotice("正在读取推荐快照", "idle");
    };
    const renderMissingHistoricalDate = (strategy, selectedDate) => {
      if (els.outcomeSummary) els.outcomeSummary.hidden = true;
      els.sixSeatStatus.textContent = "无历史榜单";
      els.sixSeatMeta.textContent = "推荐 0 · 关注 0 · 回避 0";
      state.payload = null;
      state.projectionVersion = "";
      els.inputQualityStatus.textContent = "—";
      els.inputQualityMeta.textContent = "所选历史快照不重算评分输入";
      els.inputQualityBlockers.textContent = "本轮阻断：历史快照不适用";
      els.inputQualityDegradations.textContent = "仅降级，不代表股票存在风险：历史快照不适用";
      els.inputQualityStages.textContent = "历史快照不重算评分输入链路";
      els.funnelStatus.textContent = "历史只读";
      els.funnelStages.textContent = "历史快照不重算逐层推荐漏斗";
      els.funnelMeta.textContent = funnelSummary(null, null, null, 0, "不保存");
      renderObservationStages(els, null, null, false, []);
      els.quoteTime.textContent = "-";
      els.quoteAge.textContent = "-";
      els.quoteSource.textContent = "来源不可用";
      els.inputQualityStrategy.textContent = selection.strategyLabel(strategy);
      els.inputQualityScoreTime.textContent = "所选历史日期不重算评分";
      els.publicationStatus.textContent = "历史只读";
      els.publicationMeta.textContent = "所选日期无正式快照";
      els.budgetStatus.textContent = "—";
      els.budgetMeta.textContent = "历史日期无可用预算状态";
      els.recommendationTable.classList.add("is-history");
      els.recommendationTable.classList.remove("is-anchor-table", "is-long-table");
      setLongControls(false);
      setLongLayout(false);
      if (els.longSidebar) els.longSidebar.hidden = true;
      if (els.longStockHeader) els.longStockHeader.hidden = true;
      const definition = render.historyTable();
      els.tableColumns.innerHTML = definition.columns;
      els.tableHead.innerHTML = definition.head;
      renderTableState(`${selection.strategyLabel(strategy)}策略在 ${selectedDate} 没有荐股数据`, 7);
      setNotice("已保留所选历史日期", "idle");
    };
    return { renderLoadingState, renderMissingHistoricalDate, renderTableState, setLongControls, setLongLayout, setNotice };
  }

  function renderSummary(els, payload, items, observationState, firstVisible, selection, render, statusPayload) {
    const coverage = payload && payload.coverage || {};
    const strategyQuality = payload && payload.status === "not_ready"
      ? strategyInputQuality(payload, statusPayload) : null;
    const inputQuality = strategyQuality;
    const marketWarmup = payload && payload.status === "not_ready" && !inputQuality
      ? marketWarmupStatus(statusPayload)
      : null;
    const decisionPipeline = payload && payload.pipeline;
    const pipeline = decisionPipeline && Array.isArray(decisionPipeline.stages)
      && decisionPipeline.stages.some((stage) => stage.key === "local_data") ? decisionPipeline : null;
    const stageSnapshots = pipeline && pipeline.stage_snapshots;
    const runtimeSummary = inputQuality && inputQuality.summary
      || marketWarmup && marketWarmup.summary
      || {};
    const strategySummary = strategyQuality && strategyQuality.summary
      || marketWarmup && marketWarmup.summary
      || {};
    const evidence = pipelineStage(pipeline, "local_score");
    const action = pipelineStage(pipeline, "action_publish");
    const concentration = pipelineStage(pipeline, "fixed_selection");
    const evaluated = pipeline
      ? finiteNonNegativeInteger(evidence && evidence.output_count)
      : finiteNonNegativeInteger(coverage.evaluated_count);
    const executableCount = finiteNonNegativeInteger(
      action && pipelineFacet(action, "action_executable")?.count,
    ) ?? items.filter((item) => item.action === "executable").length;
    const observedCount = finiteNonNegativeInteger(
      action && pipelineFacet(action, "action_observe")?.count,
    ) ?? items.filter((item) => item.action === "observe").length;
    const observed = observationSummary(payload, observationState, observedCount);
    const scoreSummary = selection.recommendationSummary(payload, items);
    const highestRuntimeScore = finiteNumber(runtimeSummary.highest_final_score);
    const topScore = highestRuntimeScore == null
      ? visibleScore(scoreSummary.topScore)
      : highestRuntimeScore.toFixed(2);
    renderInputQuality(els, payload, items, strategyQuality, marketWarmup, pipeline, topScore, evaluated);
    if (payload.strategy === "long") {
      els.funnelStatus.textContent = "不适用";
      els.funnelStages.textContent = "长期固定观察池不经过短线过滤、评分与正式推荐链路";
      els.funnelMeta.textContent = "长期固定观察池不评分、不产生推荐";
      renderObservationStages(els, null, null, true, []);
    } else if (pipeline) {
      const actionEligible = finiteNonNegativeInteger(action && action.output_count);
      const selected = finiteNonNegativeInteger(concentration && concentration.output_count);
      const hasPipelineProgress = pipelineHasProgress(pipeline);
      const hasFunnelProgress = hasPipelineProgress
        && evaluated != null
        && actionEligible != null
        && selected != null;
      if (!hasFunnelProgress) {
        els.funnelStatus.textContent = "等待评分输入";
        els.funnelStages.textContent = "评分链路尚未开始";
      } else {
        els.funnelStatus.textContent = payload.stage === "hybrid" ? "已发布复核结果"
          : render.scorePresentation(payload).learnedRank ? "已发布模型排序" : "已发布本地结果";
        els.funnelStages.textContent = `轮次 ${payload.decision_version || "未知"} · 本轮至决策 ${formatStageDuration(pipeline.elapsed_ms)} · ${decisionPipelineDetails(pipeline)}`;
      }
      els.funnelMeta.textContent = pipelineFunnelSummary(
        stageSnapshots,
        executableCount,
        observed,
        hasFunnelProgress,
      ) || pendingFunnelSummary(executableCount, observed, hasFunnelProgress);
      const nextQuality = payload.frozen || payload.view === "history"
        ? null : strategyInputQuality(payload, statusPayload);
      const nextStages = nextQuality && nextQuality.stage_snapshots;
      if (Array.isArray(nextStages) && nextStages.length && Array.isArray(stageSnapshots)
        && nextStages[0].output_batch_id !== stageSnapshots[0]?.output_batch_id) {
        els.funnelStages.textContent += " · 另有新输入轮正在处理";
      }
      renderObservationStages(
        els,
        stageSnapshots,
        pipeline,
        false,
        [],
        publicationIo(statusPayload, payload),
      );
    } else if (payload && payload.status === "not_ready") {
      els.funnelStatus.textContent = "尚无已发布轮";
      els.funnelStages.textContent = "输入准备中，十一层保留展示当前已取得的进度";
      els.funnelMeta.textContent = "等待本轮发布";
      renderObservationStages(
        els, strategyQuality && strategyQuality.stage_snapshots,
        strategyQuality && strategyQuality.pipeline, false, [],
      );
    } else {
      const legacy = legacyDecisionSummary(payload, evaluated, executableCount, observed, topScore);
      els.funnelStatus.textContent = "阶段观测不可用";
      els.funnelStages.textContent = "旧快照未保存逐阶段运行观测；不以聚合计数拼接漏斗";
      els.funnelMeta.textContent = `旧快照 · ${legacy.counts} · 正式 ${executableCount} · 观察 ${observed}`;
      renderObservationStages(els, null, null, false, [], publicationIo(statusPayload, payload));
    }
    const marketFreshness = currentMarketFreshness(payload, statusPayload, strategySummary, firstVisible);
    const runtimeSource = marketFreshness.source;
    const modelBlocked = modelBlockedPreview(payload, statusPayload);
    const availableSource = [runtimeSource, firstVisible && visibleText(firstVisible.source),
      visibleText(strategySummary.latest_quote_source)].find((source) => source && source !== "unavailable");
    els.quoteSource.textContent = modelBlocked
      ? availableSource ? render.sourceLabel(availableSource) : "行情尚未准备"
      : runtimeSource
      ? render.sourceLabel(runtimeSource)
      : firstVisible && firstVisible.source
        ? render.sourceLabel(firstVisible.source)
        : visibleText(strategySummary.latest_quote_source)
          ? render.sourceLabel(strategySummary.latest_quote_source)
          : "来源不可用";
    renderBudgetSummary(els, statusPayload && statusPayload.deepseek_budget, payload,
      statusPayload && statusPayload.deepseek);
    els.inputQualityStrategy.textContent = selection.strategyLabel(payload.strategy);
    els.inputQualityScoreTime.textContent = payload.strategy === "long"
      ? "长期策略不评分"
      : modelBlocked ? "模型未就绪，预览已阻断"
      : payload.generated_at || payload.observed_at
        ? `评分于 ${payload.run_mode === "preview" ? render.formatDateTime(payload.generated_at) : render.formatTime(payload.observed_at)} 完成`
        : payload.status === "not_ready" ? "等待本轮评分完成" : "评分时间不可用";
    renderPublicationStatus(els, payload, statusPayload);
    if (payload && payload.historical !== true && statusPayload) {
      const maintenance = statusPayload.scoring_profile && statusPayload.scoring_profile.maintenance;
      const modelChanged = Array.isArray(maintenance)
        && maintenance.some((head) => head && head.restart_required === true);
      if (modelChanged || statusPayload.market_data && statusPayload.market_data.restart_required === true) {
        els.publicationMeta.textContent += " · 本地资料或模型已有更新，重启后采用";
      }
    }
  }

  function modelBlockedPreview(payload, statusPayload) {
    if (!payload || payload.status !== "not_ready" || !STRATEGIES.has(payload.strategy)
      || payload.strategy === "long" || payload.historical === true || payload.view === "history"
      || !statusPayload || statusPayload.run_mode !== "preview" || statusPayload.preview_state !== "failed") {
      return false;
    }
    return [
      "production_model_missing", "production_model_corrupt", "production_model_integrity_conflict",
      "production_model_contract_conflict", "production_model_publication_busy", "production_model_dependency_missing",
      "model_scoring_unavailable",
    ].includes(statusPayload.preview_error);
  }

  function pipelineHasProgress(pipeline) {
    const stages = pipeline && Array.isArray(pipeline.stages) ? pipeline.stages : [];
    return stages.some((stage) => (
      (Number.isFinite(stage && stage.input_count) && stage.input_count > 0)
      || (Number.isFinite(stage && stage.output_count) && stage.output_count > 0)
      || (Array.isArray(stage && stage.facets) && stage.facets.length > 0)
      || (Array.isArray(stage && stage.reason_counts) && stage.reason_counts.length > 0)
    ));
  }

  function funnelSummary(evaluated, actionEligible, selected, executable, observed) {
    return `完整评分 ${displayCount(evaluated)} · 动作合格 ${displayCount(actionEligible)} · 最终入池 ${displayCount(selected)} · 正式 ${displayCount(executable)} · 观察 ${observed == null ? "—" : observed}`;
  }

  function pipelineFunnelSummary(stageSnapshots, executable, observed, hasProgress = false) {
    const keys = new Set(Array.isArray(stageSnapshots) ? stageSnapshots.map((stage) => stage && stage.stage) : []);
    if (!OBSERVATION_STAGE_DEFINITIONS.every(([stageKeys]) => keys.has(stageKeys[0]))) return null;
    const displayStages = displayObservationStages(stageSnapshots);
    const counts = displayStages.map((stage) => stageSnapshotCount(stage));
    if (counts.every((count) => count === "—")) {
      return compactPendingFunnelSummary(executable, observed, hasProgress);
    }
    return `荐股链路 ${counts.join("→")} · 正式 ${displayCount(executable)} · 观察 ${observed == null ? "—" : observed}`;
  }

  function pendingFunnelSummary(executable, observed, hasProgress = false) {
    return compactPendingFunnelSummary(executable, observed, hasProgress);
  }

  function compactPendingFunnelSummary(executable, observed, hasProgress) {
    const status = hasProgress ? "计数待就绪" : "待启动";
    return `荐股链路${status} · 正式 ${displayCount(executable)} · 观察 ${observed == null ? "—" : observed}`;
  }

  function stageSnapshotCount(stage) {
    return displayCount(stage && stage.output_count);
  }

  function emptyFunnelSummary() {
    return funnelSummary(null, null, null, null, null);
  }

  function stageErrorMarkup(issues) {
    return issues.slice(0, 3).map((issue) => {
      const presented = presentIssue(issue);
      const level = issue.severity === "error" ? "error" : "degraded";
      return `<div class="observation-stage-error" data-level="${escapeHtml(level)}"><strong>${escapeHtml(presented.message)}</strong><span>${escapeHtml(presented.meta)}${issue.count > 1 ? ` · ${issue.count} 次` : ""}</span></div>`;
    }).join("");
  }

  const OBSERVATION_STAGE_DEFINITIONS = Object.freeze([
    [["local_data"], "本地资料加载", false],
    [["static_filter"], "静态资格过滤", true],
    [["dynamic_market"], "实时行情", false],
    [["dynamic_filter"], "动态过滤", true],
    [["market_features"], "市场总体与特征", false],
    [["model_filter"], "模型过滤", true],
    [["local_score"], "本地评分", false],
    [["local_risk"], "风险扣分", false],
    [["fixed_selection"], "固定评分前六", false],
    [["risk_review"], "可选 DeepSeek 复核", false],
    [["action_publish"], "动作与发布", false],
  ]);

  function displayObservationStages(stageSnapshots) {
    const snapshots = Array.isArray(stageSnapshots) ? stageSnapshots : [];
    const byKey = new Map(snapshots.map((stage) => [stage && stage.stage, stage]));
    return OBSERVATION_STAGE_DEFINITIONS.map(([keys]) => byKey.get(keys[0]) || {
      stage: keys[0], state: "not_ready", input_count: null, output_count: null,
      rejected_count: null, pending_count: null, failed_count: null, not_selected_count: null,
      latency_ms: null, reasons: [], operation_durations: [], source_health: {},
      observation_missing: true,
    });
  }

  function publicationIo(statusPayload, payload) {
    if (!payload || payload.view === "history") return [];
    const receipts = statusPayload && statusPayload.scheduler && statusPayload.scheduler.publication_io;
    return Array.isArray(receipts)
      ? receipts.filter((item) => item.strategy === payload.strategy && item.trade_date === payload.trade_date
        && item.decision_version === payload.decision_version
        && item.content_hash && item.content_hash === payload.content_hash)
      : [];
  }

  const PUBLICATION_OPERATIONS = Object.freeze({
    current_publish: "当前快照发布", decision_event: "决策事件交接", observer_enqueue: "研究观察队列交接",
    checkpoint_write: "检查点写入", formal_lookup: "正式记录读取", checkpoint_lookup: "检查点读取",
    freeze_seal: "冻结封口", formal_write: "正式记录写入", formal_publish: "正式快照发布",
    checkpoint_consume: "检查点清理", formal_restore: "正式快照恢复",
  });

  function publicationIoMarkup(receipts) {
    if (!receipts.length) return '<small>冻结与发布操作：本次运行尚无操作记录</small>';
    return '<small>冻结与发布操作（按操作次数统计，不是股票数量；加入队列后仍需等待处理）</small>'
      + receipts.map((item) => {
        const reason = Array.isArray(item.reasons) ? item.reasons.map((r) => `${window.TraderRender.pipelineReasonLabel(r.code)}（${displayCount(r.count)}次）`).join(" · ") : "";
        const health = item.source_health || {};
        const duration = Number.isFinite(item.latency_ms) ? `${item.latency_ms}毫秒` : "进行中";
        const identity = item.decision_version || "尚未取得评分结果版本";
        const sourceState = ({ ready: "正常", degraded: "已降级", unavailable: "不可用", unknown: "未知" })[health.state] || "未知";
        const text = `${PUBLICATION_OPERATIONS[item.operation] || "其他发布操作"} · ${stageStateLabel(item.state)} · 输入 ${item.input_count} / 输出 ${item.output_count} / 业务拒绝 ${item.rejected_count} / 待补充 ${item.pending_count} / 失败 ${item.failed_count} · 耗时 ${duration} · 来源 ${sourceState} · 距最近成功 ${Number.isFinite(health.age_seconds) ? `${health.age_seconds.toFixed(1)}秒` : "未知"}${reason ? ` · ${reason}` : ""}`;
        return `<small data-publication-operation="${escapeHtml(item.operation)}" data-state="${escapeHtml(item.state)}">${escapeHtml(text)}</small><details class="publication-version-detail"><summary>版本核对（排查结果是否一致）</summary><small>操作所用评分版本：${escapeHtml(identity)}<br>操作产出版本：${escapeHtml(item.output_version || "尚无产出版本")}</small></details>`;
      }).join("");
  }

  function renderObservationStages(els, stageSnapshots, pipeline, long, issues, publicationReceipts = []) {
    if (!els.observationStageList) return;
    if (long) {
      els.observationStageList.innerHTML = '<div class="observation-stage-empty">长期固定观察池不经过荐股评分链路</div>';
      return;
    }
    const list = els.observationStageList;
    const selection = typeof window.getSelection === "function" ? window.getSelection() : null;
    if (selection && !selection.isCollapsed && typeof list.contains === "function"
      && list.contains(selection.anchorNode) && list.contains(selection.focusNode)) return;
    const expanded = new Set(typeof list.querySelectorAll === "function"
      ? Array.from(list.querySelectorAll(".observation-stage.is-expanded"), (stage) => stage.dataset.stageKeys)
      : []);
    const stages = displayObservationStages(stageSnapshots);
    const visibleIssues = Array.isArray(issues) ? issues : [];
    const markup = stages.map((stage, index) => {
      const [stageKeys, label, isFilter] = OBSERVATION_STAGE_DEFINITIONS[index];
      const stageKey = stageKeys.join("+");
      const isExpanded = expanded.has(stageKey);
      const input = finiteNonNegativeInteger(stage && stage.input_count);
      const output = finiteNonNegativeInteger(stage && stage.output_count);
      const rejected = finiteNonNegativeInteger(stage && stage.rejected_count);
      const pending = finiteNonNegativeInteger(stage && stage.pending_count);
      const failed = finiteNonNegativeInteger(stage && stage.failed_count);
      const ioFailed = stageKeys.includes("action_publish") && publicationReceipts.some((item) => item.state === "failed");
      const state = ioFailed && (!stage || stage.state !== "failed") ? "degraded" : stage && stage.state || "not_ready";
      const reasons = Array.isArray(stage && stage.reasons)
        ? stage.reasons
        : Array.isArray(stage && stage.reason_counts) ? stage.reason_counts : [];
      const reasonUnit = stageKeys.includes("local_data") ? "项" : "只";
      const reasonText = reasons.map((reason) => `${window.TraderRender.pipelineReasonLabel(reason.reason || reason.code)}（${displayCount(reason.count)}${reasonUnit}）`).join(" · ");
      const diagnostics = pipelineStage(pipeline, stageKeys[0]);
      const facets = Array.isArray(diagnostics && diagnostics.facets) ? diagnostics.facets : [];
      const facetText = facets.map((facet) => `${window.TraderRender.pipelineFacetLabel(facet.key)} ${displayCount(facet.count)}只`).join(" · ");
      const duration = Number.isFinite(stage && stage.latency_ms)
        ? stage.latency_ms
        : Number.isFinite(stage && stage.duration_ms) ? stage.duration_ms : null;
      const stageIssues = visibleIssues.filter((issue) => stageKeys.some((key) => issueBelongsToStage(issue, key)));
      const disposition = `淘汰 ${displayCount(rejected)} · 待补 ${displayCount(pending)} · 失败 ${displayCount(failed)} · 未入选 ${displayCount(stage && stage.not_selected_count)}`;
      const outputLabel = stageKeys.some((key) => key === "market_features" || key === "model_filter") && pending > 0 && output === 0
        ? "待补充" : output == null ? "—" : displayCount(output);
      const readiness = stageKeys.includes("model_filter") && pending === input && input > 0 && output === 0 && rejected === 0 && failed === 0
        ? "模型输入未就绪" : stageStateLabel(state);
      const detail = `${readiness} · ${input == null ? "—" : displayCount(input)} → ${outputLabel} · ${disposition} · 耗时 ${formatStageDuration(duration)}`;
      const operationLabels = { processing: "处理与校验", review_wait: "复核等待", startup_reuse: "沿用启动结果", cache_reuse: "本轮缓存复用", local_read: "本地资料读取", acquisition: "行情采集", feature_assembly: "特征准备", feature_compute: "总体特征计算", quote_refresh: "行情更新", validation: "输入校验" };
      const reusedStartup = (stage.operation_durations || []).some((item) => item.operation === "startup_reuse");
      const operationText = (stage.operation_durations || []).map((item) => item.operation === "startup_reuse"
        ? `${operationLabels[item.operation]} · 启动于 ${window.TraderRender.formatTime(stage.as_of)}`
        : `${operationLabels[item.operation] || item.operation} ${formatStageDuration(item.duration_ms)}`).join(" · ");
      const health = stage.source_health || {};
      const sourceState = ({ ready: "正常", degraded: "已降级", unavailable: "不可用" })[health.state] || "未知";
      const sourceText = `来源 ${sourceState} · 覆盖 ${displayCount(health.healthy_source_count)} / ${displayCount(health.source_count)} · 来源年龄 ${Number.isFinite(health.age_seconds) ? health.age_seconds.toFixed(1) + " s" : "未知"}`;
      const missingText = stage.observation_missing
        ? '<small>本层尚无可用观测；缺失字段不代表零，历史记录按原协议只读</small>' : "";
      const detailMarkup = `${missingText}<small>${escapeHtml(sourceText)}</small>${operationText ? `<small>${escapeHtml(operationText)}${reusedStartup ? " · 本轮未重新执行" : " · 排队耗时未单独测量"}</small>` : ""}${facetText ? `<small class="observation-stage-facets">处理结果：${escapeHtml(facetText)}</small>` : ""}${reasonText ? `<small>${isFilter ? "过滤与待补充原因" : "说明与原因"}：${escapeHtml(reasonText)}</small>` : ""}${stageIssues.length ? `<div class="observation-stage-errors">${stageErrorMarkup(stageIssues)}</div>` : ""}${stageKeys.includes("action_publish") ? publicationIoMarkup(publicationReceipts) : ""}`;
      return `<article class="observation-stage${isExpanded ? " is-expanded" : ""}" data-state="${escapeHtml(state)}" data-stage-keys="${escapeHtml(stageKey)}"><div class="observation-stage-index">${String(index + 1).padStart(2, "0")}</div><div class="observation-stage-main"><button class="observation-stage-toggle" type="button" data-stage-toggle="true" aria-expanded="${isExpanded}" aria-controls="observation-stage-detail-${index}"><strong>${escapeHtml(label)}<i class="observation-stage-chevron" aria-hidden="true"></i></strong><span>${escapeHtml(detail)}</span></button><div id="observation-stage-detail-${index}" class="observation-stage-detail">${detailMarkup}</div></div></article>`;
    }).join("");
    if (list.innerHTML !== markup) list.innerHTML = markup;
  }

  function formatStageDuration(milliseconds) {
    if (!Number.isFinite(milliseconds)) return "未测量";
    if (milliseconds < 1000) return `${milliseconds} ms`;
    if (milliseconds < 60000) return `${(milliseconds / 1000).toFixed(2)} s`;
    return `${(milliseconds / 60000).toFixed(2)} min`;
  }

  function stageStateLabel(state) {
    return ({ ready: "已完成", completed: "已完成", running: "采集中", degraded: "已降级", failed: "失败", not_ready: "未就绪", not_applicable: "不适用" })[state] || "待计算";
  }

  function renderInputQuality(els, payload, items, inputQuality, marketWarmup, pipeline, topScore, evaluated) {
    if (payload && payload.strategy === "long") {
      els.inputQualityStatus.textContent = "不适用";
      els.inputQualityStages.textContent = "长期固定观察池不经过短线评分输入链路";
      els.inputQualityMeta.textContent = "长期固定观察池不评分";
      els.inputQualityBlockers.textContent = "本轮阻断：不适用";
      els.inputQualityDegradations.textContent = "仅降级，不代表股票存在风险：不适用";
      return;
    }
    const quoteAvailability = quoteAvailabilitySummary(items);
    const runtimeSummary = inputQuality && inputQuality.summary || marketWarmup && marketWarmup.summary || {};
    const runtimeTotal = finiteNonNegativeInteger(runtimeSummary.quote_total_count);
    const runtimeAvailable = finiteNonNegativeInteger(runtimeSummary.quote_covered_count);
    if (pipeline) {
      const prepared = pipelineStage(pipeline, "market_features");
      const scored = pipelineStage(pipeline, "local_score");
      const missing = prepared && prepared.pending_count;
      els.inputQualityStages.textContent = inputPipelineDetails(pipeline);
      els.inputQualityStatus.textContent = `已发布轮 · 评分 ${displayCount(scored && scored.output_count)}`;
      els.inputQualityMeta.textContent = `完整输入 ${displayCount(prepared && prepared.input_count)} · 就绪 ${displayCount(prepared && prepared.output_count)} · 待补 ${displayCount(missing)}`;
      const blocked = pipeline.stages.find((stage) => stage.pending_count || stage.failed_count);
      els.inputQualityBlockers.textContent = blocked ? `首个断点：${pipelineReasonSuffix(blocked) || blocked.key}` : "本轮无输入断点";
      els.inputQualityDegradations.textContent = pipeline.stages.some((stage) => stage.state === "degraded")
        ? "已发布轮存在降级，展开对应层查看来源及原因" : "已发布轮未记录降级";
      return;
    }
    if (payload && payload.status === "not_ready") {
      els.inputQualityStatus.textContent = "尚无已发布轮";
      els.inputQualityStages.textContent = "运行中输入不与已发布名单拼接";
      els.inputQualityMeta.textContent = "输入准备中";
      els.inputQualityBlockers.textContent = "等待本轮完成";
      els.inputQualityDegradations.textContent = "来源状态见运行诊断";
      return;
    }
    const executable = items.filter((item) => item.action === "executable").length;
    const observed = items.filter((item) => item.action === "observe").length;
    const legacy = legacyDecisionSummary(payload, evaluated, executable, observed, topScore);
    els.inputQualityStatus.textContent = `${legacy.scored} / 总体 ${legacy.population}`;
    els.inputQualityStages.textContent = `旧快照未保存评分输入阶段观测；已保存聚合 ${legacy.counts}${legacy.reasons}`;
    els.inputQualityMeta.textContent = `最高 ${topScore} · 阶段范围与门槛未保存`;
    els.inputQualityBlockers.textContent = `本轮阻断：${legacy.blocker}`;
    els.inputQualityDegradations.textContent = quoteAvailability.total
      ? `仅降级，不代表股票存在风险：当前名单行情 ${quoteAvailability.available} / ${quoteAvailability.total}`
      : "仅降级，不代表股票存在风险：可核验聚合已展示，阶段归属不可恢复";
  }

  function legacyDecisionSummary(payload, evaluated, executable, observed, topScore) {
    const coverage = payload && payload.coverage || {};
    const population = finiteNonNegativeInteger(coverage.candidate_count);
    const rejected = finiteNonNegativeInteger(coverage.rejected_count);
    const scored = displayCount(evaluated);
    const counts = `总体 ${displayCount(population)} · 已评分 ${scored} · 拒绝 ${displayCount(rejected)}`;
    const reasons = Object.entries(payload && payload.filter_reason_counts || {})
      .filter(([reason, count]) => reason && finitePositiveInteger(count) != null)
      .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
      .slice(0, 3)
      .map(([reason, count]) => `${window.TraderRender.reasonLabel(reason)}${count}`);
    const emptyReason = payload && payload.selection_diagnostics && payload.selection_diagnostics.empty_reason;
    const blocker = ({
      no_scored_candidates: "没有形成完整评分候选",
      score_below_observation_floor: window.TraderRender.scorePresentation(payload).learnedRank
        ? "本地评分未达到动作观察线；模型排序分不与动作线比较"
        : `最高 ${topScore}，未达到观察线`,
      no_positive_net_utility: "成本后净效用门未通过",
      risk_or_execution_blocked: "达到分数线的候选受风险或执行门限制",
      selection_limits: "最终池容量或集中度限制",
    })[emptyReason] || "旧记录未保存阶段归属";
    return {
      population: displayCount(population),
      scored: `已评分 ${scored}`,
      rejected: displayCount(rejected),
      counts,
      reasons: reasons.length ? `；主要过滤 ${reasons.join(" · ")}` : "；过滤原因未保存",
      blocker,
      executable,
      observed,
    };
  }

  function percent(value, total) {
    return total > 0 ? `${(value / total * 100).toFixed(1)}%` : "—";
  }

  function quoteAvailabilitySummary(items) {
    const values = Array.isArray(items) ? items : [];
    let available = 0;
    values.forEach((item) => {
      if (quoteAvailable(item)) available += 1;
    });
    return {
      total: values.length,
      available,
      quoteMissing: values.length - available,
    };
  }

  function quoteAvailable(item) {
    if (!item || ["missing", "decision_anchor"].includes(cleanString(item.quote_status))) return false;
    const price = Number(item.price);
    const change = Number(item.pct_change);
    const sourceTime = new Date(item.source_time || "").getTime();
    return Number.isFinite(price)
      && price > 0
      && item.pct_change !== null
      && item.pct_change !== ""
      && Number.isFinite(change)
      && Boolean(visibleText(item.source))
      && Number.isFinite(sourceTime);
  }

  function visibleText(value) {
    const valueText = cleanString(value);
    return valueText && !["-", "—"].includes(valueText) ? valueText : null;
  }

  function cleanDate(value) {
    return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : "—";
  }

  function renderHealth(els, statusPayload, snapshotReasons, strategy, rememberDiagnostic) {
    const health = healthView(statusPayload, snapshotReasons, strategy);
    const activeIssues = health.issues.filter((issue) => issue.recoveryStatus !== "recovered");
    const alwaysVisible = Boolean(els.errorDetailsButton.classList && els.errorDetailsButton.classList.contains("observation-trigger"));
    const observationAvailable = setObservationTriggerAvailability(els, strategy);
    els.errorDetailsButton.hidden = !alwaysVisible && activeIssues.length === 0;
    els.errorDetailsButton.dataset.level = health.level;
    els.healthBadge.textContent = !observationAvailable
      ? "观察不适用"
      : alwaysVisible
      ? (activeIssues.length ? `观察 · ${activeIssues.length}项异常` : "观察")
      : `${health.level === "error" ? "错误" : "异常"} ${activeIssues.length}`;
    if (els.observationErrorCount) els.observationErrorCount.textContent = String(activeIssues.length);
    if (typeof rememberDiagnostic === "function") {
      health.issues.forEach((issue) => rememberDiagnostic(issue.code));
    }
    return { ...health, visibleIssues: activeIssues };
  }

  function setObservationTriggerAvailability(els, strategy) {
    const available = strategy !== "long";
    els.errorDetailsButton.disabled = !available;
    els.errorDetailsButton.title = available
      ? "查看评分链路观察面板"
      : "长期策略不展示评分链路观察面板";
    if (!available) els.errorDetailsButton.setAttribute?.("aria-expanded", "false");
    return available;
  }

  function renderBudgetSummary(els, budget, payload, deepseek) {
    const budgetHidden = !deepseek || deepseek.enabled !== true;
    if (els.budgetPanel) els.budgetPanel.hidden = budgetHidden;
    if (els.summaryBand) els.summaryBand.classList.toggle("is-three-column", budgetHidden);
    const available = budget && budget.available !== false;
    const used = available ? displayCount(budget.used) : "—";
    const remaining = available ? displayCount(budget.remaining) : "—";
    const explicitLimit = available ? finiteNonNegativeInteger(budget.limit) : null;
    const usedValue = available ? finiteNonNegativeInteger(budget.used) : null;
    const remainingValue = available ? finiteNonNegativeInteger(budget.remaining) : null;
    const limit = explicitLimit == null && usedValue != null && remainingValue != null
      ? String(usedValue + remainingValue)
      : explicitLimit == null ? "—" : String(explicitLimit);
    els.budgetStatus.textContent = available ? `${used} / ${remaining}` : "不可用";
    const items = payload && Array.isArray(payload.items) ? payload.items : [];
    const executable = items.filter((item) => item.action === "executable").length;
    const reviewed = items.filter((item) => item.action === "executable" && item.review_outcome === "applied").length;
    els.budgetMeta.textContent = payload && payload.score_status === "not_applicable"
      ? "长期策略不使用模型预算"
      : `已用 / 剩余 · 上限 ${limit} · 复核 ${reviewed}/${executable}`;
  }

  function renderPublicationStatus(els, payload, statusPayload) {
    const preview = payload && payload.run_mode === "preview" ? payload
      : payload && payload.historical !== true && payload.strategy !== "long"
        && statusPayload && statusPayload.run_mode === "preview" ? statusPayload : null;
    if (preview) {
      const states = { waiting_inputs: "等待输入", running: "计算中", ready: "已完成", failed: "已阻塞" };
      const runStatus = statusPayload && statusPayload.run_mode === "preview"
        && (preview === statusPayload || payload.input_versions?.preview_run_id === statusPayload.preview_run_id)
        ? statusPayload : null;
      const state = states[runStatus && runStatus.preview_state]
        || (payload && payload.run_mode === "preview" && payload.status === "ready" ? "已完成" : "等待输入");
      els.publicationStatus.textContent = `测试预览 · ${state}`;
      const generated = preview.generated_at || preview.preview_started_at;
      const shadow = window.TraderRender.scorePresentation(payload).shadow ? " · Shadow 观察" : "";
      const error = runStatus && runStatus.preview_error;
      els.publicationMeta.textContent = `数据 ${preview.market_trade_date} 收盘 · ${preview.generated_at ? "计算" : "开始"} ${window.TraderRender.formatDateTime(generated)}${shadow} · 不冻结、不计入业绩${error ? ` · 阻塞：${error}` : ""}`;
      return;
    }
    if (window.TraderRender.scorePresentation(payload).shadow) {
      els.publicationStatus.textContent = "Shadow 观察";
      els.publicationMeta.textContent = "尚无生产授权，不计入正式推荐业绩";
      return;
    }
    if (!payload || payload.status === "not_ready") {
      const collecting = strategyLaneCollecting(payload, statusPayload);
      els.publicationStatus.textContent = collecting ? "采集中" : "未就绪";
      els.publicationMeta.textContent = collecting ? "等待本轮正式结果" : "等待当前策略快照";
      return;
    }
    if (payload.view === "history" || payload.historical === true) {
      els.publicationStatus.textContent = "历史只读";
      els.publicationMeta.textContent = "不会改变正式记录";
      return;
    }
    if (payload.strategy === "long") {
      els.publicationStatus.textContent = "不适用";
      els.publicationMeta.textContent = "长期固定观察池，不评分、不冻结";
      return;
    }
    const cutoff = "15:00";
    const strategy = STRATEGIES.has(payload.strategy) ? strategyLabel(payload.strategy) : "当前策略";
    if (payload.frozen) {
      els.publicationStatus.textContent = "已冻结";
      els.publicationMeta.textContent = `${strategy} ${cutoff} 已固化`;
      return;
    }
    els.publicationStatus.textContent = "实时滚动";
    els.publicationMeta.textContent = `${strategy} ${cutoff} 固化`;
  }

  function updateQuoteAge(els, payload, render, statusPayload) {
    const freshness = currentMarketFreshness(payload, statusPayload, null, null);
    const sourceTime = freshness.sourceTime;
    const timestamp = new Date(sourceTime || "").getTime();
    if (!Number.isFinite(timestamp)) {
      els.quoteAge.textContent = "-";
      els.quoteTime.textContent = "-";
      if (els.quoteFreshness) els.quoteFreshness.dataset.ageState = "unknown";
      return;
    }
    const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
    els.quoteAge.textContent = formatDurationHms(seconds);
    els.quoteTime.textContent = render.formatTime(sourceTime);
    if (els.quoteFreshness) els.quoteFreshness.dataset.ageState = quoteAgeState(seconds);
  }

  function currentMarketFreshness(payload, statusPayload, strategySummary, firstVisible) {
    const market = statusPayload && statusPayload.market_data;
    const age = market && market.candidate_quote_age;
    if (payload && payload.view !== "history" && age && age.latest_source_time) {
      return {
        source: visibleText(market.candidate_quote_latest_source) || visibleText(market.active_source),
        sourceTime: age.latest_source_time,
      };
    }
    const quality = strategySummary || strategyInputQuality(payload, statusPayload) && strategyInputQuality(payload, statusPayload).summary;
    return {
      source: quality && visibleText(quality.latest_quote_source)
        || firstVisible && visibleText(firstVisible.source)
        || null,
      sourceTime: quality && quality.latest_quote_source_time
        || firstVisible && firstVisible.source_time
        || payload && payload.published_at
        || null,
    };
  }

  function quoteAgeState(seconds) {
    if (seconds <= 60) return "fresh";
    if (seconds <= 180) return "aging";
    return "stale";
  }

  function formatDurationHms(totalSeconds) {
    const numeric = Number(totalSeconds);
    const duration = Number.isFinite(numeric) ? Math.max(0, Math.floor(numeric)) : 0;
    const hours = Math.floor(duration / 3600);
    const minutes = Math.floor((duration % 3600) / 60);
    const seconds = duration % 60;
    if (hours > 0) return `${hours}时 ${minutes}分 ${seconds}秒`;
    if (minutes > 0) return `${minutes}分 ${seconds}秒`;
    return `${seconds}秒`;
  }

  function observationSummary(payload, observationState, count) {
    if (payload && payload.strategy === "long") return String(count);
    if (observationState === "open" || observationState === "empty") return String(count);
    if (observationState === "hidden_history") return "不保存";
    if (observationState === "unknown") return "状态未知";
    return "已关闭";
  }

  function displayCount(value) {
    const parsed = finiteNonNegativeInteger(value);
    return parsed == null ? "—" : String(parsed);
  }

  function visibleScore(value) {
    const parsed = finiteNumber(value);
    return parsed == null ? "—" : parsed.toFixed(2);
  }

  function inputPipelineDetails(pipeline) {
    return OBSERVATION_STAGE_DEFINITIONS.slice(0, 6).map(([keys, label]) => {
      const stage = pipelineStage(pipeline, keys[0]);
      return `${stageTransition(label, stage)}${pipelineReasonSuffix(stage)}`;
    }).join(" ｜ ");
  }

  function decisionPipelineDetails(pipeline) {
    return OBSERVATION_STAGE_DEFINITIONS.slice(6).map(([keys, label]) => {
      const stage = pipelineStage(pipeline, keys[0]);
      return `${stageTransition(label, stage)}${pipelineReasonSuffix(stage)}`;
    }).join(" ｜ ");
  }

  function pipelineStage(pipeline, key) {
    const stages = pipeline && Array.isArray(pipeline.stages) ? pipeline.stages : [];
    return stages.find((stage) => stage && stage.key === key) || null;
  }

  function pipelineFacet(stage, key) {
    const facets = stage && Array.isArray(stage.facets) ? stage.facets : [];
    return facets.find((facet) => facet && facet.key === key) || null;
  }

  function stageTransition(label, stage) {
    if (stage && stage.state === "not_applicable") return `${label} 不适用`;
    if (!stage || (!Number.isFinite(stage.input_count) && !Number.isFinite(stage.output_count))) {
      return `${label} 待开始`;
    }
    const output = stage && stage.state === "running" && stage.output_count == null
      ? "采集中"
      : displayCount(stage && stage.output_count);
    return `${label} ${displayCount(stage && stage.input_count)}→${output}`;
  }

  function pipelineReasonSuffix(stage) {
    const reasons = stage && Array.isArray(stage.reason_counts) ? stage.reason_counts : [];
    const visible = reasons
      .filter((item) => item && finitePositiveInteger(item.count) != null)
      .sort((left, right) => right.count - left.count || String(left.reason).localeCompare(String(right.reason)))
      .slice(0, 2)
      .map((item) => {
        const reason = cleanString(item.reason);
        const actionLabel = window.TraderRender && typeof window.TraderRender.actionReason === "function"
          ? window.TraderRender.actionReason(reason)
          : null;
        const label = actionLabel && actionLabel !== "推荐条件暂未满足"
          ? actionLabel
          : window.TraderRender && typeof window.TraderRender.reasonLabel === "function"
            ? window.TraderRender.reasonLabel(reason)
            : reason;
        return `${label}${item.count}`;
      });
    return visible.length ? `〔主要原因 ${visible.join(" · ")}〕` : "";
  }

  function finiteNumber(value) {
    if (value == null || value === "" || typeof value === "boolean") return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function strategyInputQuality(payload, statusPayload) {
    if (!payload || payload.historical === true) return null;
    const inputQuality = statusPayload
      && statusPayload.scheduler
      && statusPayload.scheduler.input_quality;
    const value = inputQuality && inputQuality[payload.strategy];
    if (!value || typeof value !== "object") return null;
    const summary = value.summary;
    return summary && summary.trade_date === payload.trade_date ? value : null;
  }

  function recommendationReadinessStatus(payload, statusPayload) {
    const market = statusPayload && statusPayload.market_data;
    if (payload && payload.status === "not_ready" && payload.historical !== true
        && payload.strategy !== "long" && market
        && market.published_history_state === "unavailable"
        && market.history_maintenance_state === "loading"
        && market.history_maintenance_stage === "reading_active_snapshot") {
      return { primary_blocker: "history_projection_loading" };
    }
    const inputQuality = strategyInputQuality(payload, statusPayload);
    if (inputQuality) return inputQuality;
    return payload && payload.status === "not_ready" ? marketWarmupStatus(statusPayload) : null;
  }

  function marketWarmupStatus(statusPayload) {
    const market = statusPayload && statusPayload.market_data;
    if (!market || typeof market !== "object") return null;
    const total = finiteNonNegativeInteger(market.candidate_quote_cache_entries);
    const covered = finiteNonNegativeInteger(
      market.candidate_quote_age && market.candidate_quote_age.sample_count,
    );
    if (total == null && covered == null) return null;
    const normalizedTotal = total == null ? covered : total;
    const normalizedCovered = covered == null ? 0 : Math.min(covered, normalizedTotal);
    return {
      primary_blocker: "candidate_quotes_pending",
      summary: {
        quote_total_count: normalizedTotal,
        quote_covered_count: normalizedCovered,
        quote_missing_count: Math.max(0, normalizedTotal - normalizedCovered),
        security_identity_missing_count: null,
        security_identity_pending: true,
        latest_quote_source: visibleText(market.candidate_quote_latest_source)
          || visibleText(market.active_source),
        latest_quote_source_time: market.candidate_quote_age
          && market.candidate_quote_age.latest_source_time,
      },
    };
  }

  function strategyLaneCollecting(payload, statusPayload) {
    const lanes = statusPayload && statusPayload.scheduler && statusPayload.scheduler.lanes;
    if (!payload || !Array.isArray(lanes)) return false;
    const lane = lanes.find((candidate) => candidate && candidate.strategy === payload.strategy);
    return Boolean(lane && (lane.running === true || lane.pending === true));
  }

  function cleanString(value) {
    if (typeof value !== "string") return null;
    const cleaned = value.trim();
    return cleaned ? cleaned.slice(0, 128) : null;
  }

  function strategyLabel(strategy) {
    return window.TraderSelection?.strategyLabel(strategy) || strategy;
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

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  window.TraderStatusView = Object.freeze({
    createDashboardStateRenderer,
    formatDurationHms,
    decisionPipelineDetails,
    healthView,
    inputPipelineDetails,
    pendingFunnelSummary,
    pipelineFunnelSummary,
    issueSummaryTitle,
    quoteAvailabilitySummary,
    recommendationReadinessStatus,
    renderBudgetSummary,
    renderHealth,
    renderInputQuality,
    renderPublicationStatus,
    renderSummary,
    renderObservationStages,
    setObservationTriggerAvailability,
    runtimeErrorRows,
    updateQuoteAge,
  });
})();
