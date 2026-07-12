# 生产冻结与试验登记

P0 的机器事实源是 `config/production_baseline.json`。启动时默认启用
`PRODUCTION_FREEZE_ENABLED=1`，策略版本、明日策略 Top-K、候选过滤阈值、退出规则和
生产权重均按该清单锁定。七个实验性开关被强制关闭；DeepSeek 可以继续调用和记录，
但只形成 shadow 对照，不改变生产排序、过滤或仓位。

每个推荐响应和验证信号都携带 `generation`：其中包含基线 ID、策略版本、排序字段、
完整有效开关、权重/输入/输出指纹和重放上下文。`baseline_status=drift_detected` 表示代码
或运行配置与冻结清单不一致，不能把结果混入同一实验基线。

试验登记保存在 `experiments/registry.jsonl`，每行一个 JSON 对象。登记必须包含假设、
唯一变更、训练窗口、测试窗口、主指标、风险约束、试验族、结果和决定。可用以下命令
查看或追加经过校验的记录：

```bash
python -m stock_analyzer.experiment_registry list
python -m stock_analyzer.experiment_registry register --record experiment.json
```

`tomorrow_picks` 是首个研究策略，生产 K 固定为 5。所有研究报告同时给出 K=3/5/10，
其中 K=3/10 仅作敏感性诊断，不得因表现更好而替换生产 K。
