window.TraderValidationUI = {
  validationStrategyMeta(strategy, strategyLabel) {
    const label = strategyLabel(strategy);
    if (strategy === "swing_picks") {
      return { label, horizon: "2-5日退出", focus: "2-5日持有样本", outcome: "2-5日退出净收益" };
    }
    if (strategy === "short_term") {
      return { label, horizon: "次日辅助", focus: "盘中观察样本", outcome: "次日辅助表现" };
    }
    return { label, horizon: "次日", focus: "明日优先样本", outcome: "次日净收益" };
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
    const {
      strategy, sample, outcome, replay, realDayCount, winRate, avgReturn,
      horizon, pendingOutcome, validationGate,
    } = data;
    let level = "neutral";
    let text = "结论：数据正在更新，先关注锚点方向与锚点到现在变化。";
    if (strategy === "short_term") {
      level = "watch";
      text = "结论：盘中观察仅用于发现强势股，仓位固定为0，不作为可执行荐股；次日数据只做辅助归因。";
    } else if (Number(pendingOutcome || 0) > 0 && sample <= 0) {
      text = `结论：还有 ${pendingOutcome} 条信号待回填，当前先不要用胜率下结论。`;
    } else if (outcome <= 0 && sample <= 0) {
      if (replay > 0) {
        text = `结论：当前仅有回放样本 ${replay} 条，不能作为主判断；请等待快照与实时回填。`;
      } else {
        text = "结论：暂无真实回填结果。系统会自动回填最新锚点样本，稍后自动更新。";
      }
    } else if (sample <= 0 && outcome > 0) {
      text = `结论：已有 ${outcome} 条回填结果，但主周期样本未成熟，先不要据此下结论。`;
    } else if (validationGate?.blocked) {
      level = validationGate.state === "retired" ? "bad" : "watch";
      text = `结论：${validationGate.reason || "组合验证门控未通过，仅保留备选观察"}；仓位0。`;
    } else if (winRate == null || avgReturn == null) {
      text = "结论：统计字段不完整，等待自动更新结果。";
    } else if (!validationGate?.validated) {
      level = "watch";
      text = `结论：验证门控状态尚未确认，真实${realDayCount}日；当前只观察，不提高权重。`;
    } else if (winRate >= 55) {
      level = "good";
      text = `结论：通过执行门控。真实${realDayCount}日，${horizon}净胜率 ${formatNumber(winRate, 1)}%，平均净收益 ${formatNumber(avgReturn, 2)}%。`;
    } else {
      level = "watch";
      text = `结论：通过最低执行门控但不加权。真实${realDayCount}日，${horizon}净胜率 ${formatNumber(winRate, 1)}%，平均净收益 ${formatNumber(avgReturn, 2)}%。`;
    }
    target.className = `validation-current-decision decision-${level}`;
    target.textContent = text;
  },
};
