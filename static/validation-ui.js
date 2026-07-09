window.TraderValidationUI = {
  validationStrategyMeta(strategy, strategyLabel) {
    const label = strategyLabel(strategy);
    if (strategy === "swing_picks") {
      return { label, horizon: "5日", focus: "2-5天样本", outcome: "5日净收益" };
    }
    return { label, horizon: "次日", focus: "次日样本", outcome: "次日净收益" };
  },

  validationBatchSummaryFromRows(rows, helpers) {
    const { primaryValidationNetReturn, validationSkipReason } = helpers;
    const validRows = (rows || [])
      .map(row => ({ row, netReturn: primaryValidationNetReturn(row) }))
      .filter(item =>
        Number.isFinite(item.netReturn) &&
        item.row.outcome_updated_at &&
        !validationSkipReason(item.row.skip_reason)
      );
    const sample = validRows.length;
    const up = validRows.filter(item => item.netReturn > 0).length;
    const down = validRows.filter(item => item.netReturn < 0).length;
    const flat = sample - up - down;
    return {
      sample_count: sample,
      up_count: up,
      down_count: down,
      flat_count: flat,
      win_rate: sample > 0 ? (up / sample) * 100 : null,
      avg_return: sample > 0 ? validRows.reduce((sum, item) => sum + item.netReturn, 0) / sample : null,
    };
  },

  renderValidationSimpleDecision(target, data, helpers) {
    if (!target) return;
    const { formatNumber } = helpers;
    const { sample, outcome, real, replay, winRate, avgReturn, horizon, executionSkipped, pendingOutcome } = data;
    let level = "neutral";
    let text = "结论：数据正在更新，先关注锚点方向与锚点到现在变化。";
    if (Number(pendingOutcome || 0) > 0 && sample <= 0) {
      text = `结论：还有 ${pendingOutcome} 条信号待回填，当前先不要用胜率下结论。`;
    } else if (outcome <= 0 && sample <= 0) {
      if (replay > 0) {
        text = `结论：当前仅有回放样本 ${replay} 条，不能作为主判断；请等待快照与实时回填。`;
      } else {
        text = "结论：暂无真实回填结果。系统会自动回填最新锚点样本，稍后自动更新。";
      }
    } else if (sample <= 0 && outcome > 0) {
      text = `结论：已有 ${outcome} 条回填结果，但主周期样本未成熟，先不要据此下结论。`;
    } else if (sample < 30) {
      text = `结论：先别信胜率。当前有效样本 ${sample} 条，少于 30 条，只能观察。`;
    } else if (real < 10) {
      level = "watch";
      text = `结论：谨慎看。有效样本 ${sample} 条，但真实前瞻只有 ${real} 条，回放 ${replay} 条只能粗筛。`;
    } else if (winRate == null || avgReturn == null) {
      text = "结论：统计字段不完整，等待自动更新结果。";
    } else if (Number(executionSkipped || 0) > 0 && sample < 30) {
      text = `结论：先观察。当前有效样本 ${sample} 条，另有 ${executionSkipped} 条不可执行/被剔除样本，先不要据此加权。`;
    } else if (winRate >= 55 && avgReturn > 0) {
      level = "good";
      text = `结论：可观察。${horizon}净胜率 ${formatNumber(winRate, 1)}%，${horizon}平均净收益 ${formatNumber(avgReturn, 2)}%。`;
    } else if (winRate >= 50 && avgReturn >= 0) {
      level = "watch";
      text = `结论：一般，继续观察。${horizon}表现不弱不强，暂不建议提高权重。`;
    } else {
      level = "bad";
      text = `结论：暂不加权。${horizon}表现偏弱，先不要依赖这个策略。`;
    }
    target.className = `validation-current-decision decision-${level}`;
    target.textContent = text;
  },
};
