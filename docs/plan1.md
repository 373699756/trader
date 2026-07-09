一、现状诊断（基于真实代码）

DeepSeek 目前接了两条链路，但都有一个共同的致命缺口：它的判断从来没有被结果检验过。

现状: rerank_candidates() 用 blend_alpha=0.30 固定混合 LLM 分和本地分，并用硬阈值
gate（penalty≥30 剔除、rank<45 剔除）
问题: 0.30 是拍脑袋的全局常数；从没验证过 DeepSeek 的 priority/avoid/veto 事后是否真的提高了胜率
────────────────────────────────────────
现状: review_strategy_validation() 输出 rule_candidates（可验证规则）
问题: 只保存为"影子建议"，从不回测、不自动采纳，等于白跑
────────────────────────────────────────
现状: prompt 里有 catalyst_strength / theme_truth_score / event_type 字段
问题: 但 ENABLE_MARKET_NEWS=0、ENABLE_INLINE_SENTIMENT=0 默认关闭 → DeepSeek
在没有新闻原文的情况下判断催化剂，等于让它编题材，幻觉风险最高
────────────────────────────────────────
现状: 个股预测 stance（小仓试单/只观察…）
问题: 从不回填、不打分，无法知道对不对
────────────────────────────────────────
现状: DeepSeek 只在已过滤的池子内做排序
问题: 从不参与"今天到底该不该出手"的大盘 go/no-go 判断，而这才是 A 股短线盈亏的最大来源

一句话：DeepSeek 现在是"只说不认账"，闭环缺了"认账"这一环。

---
二、优化总原则

本地量化保底（保证降级可用）
  → DeepSeek 增强（排序 / 催化剂 / 大盘 go-no-go）
  → 每条 DeepSeek 判断都落库并事后回填打分（归因）
  → 用已有的 walk-forward OOS harness 检验 DeepSeek 是否真加了 alpha
  → 只有样本外为正才提权/采纳，否则自动降权到 0

核心思想借用你代码里 calibrate_live_weights() 已有的做法：任何改动必须样本外（OOS）优于基线 + 多数 fold 为正才允许上线。把这条纪律从"权重校准"扩展到"DeepSeek 的每一个判断"。

---
三、分阶段优化计划

P0 — DeepSeek 归因闭环（最高优先级，没有它后面全是空谈）

目标：让每一条 DeepSeek 判断都可被事后证伪。

1. 在 save_signals() 落库时，除了最终 rank，额外持久化：local_rank、deepseek_action、deepseek_veto、deepseek_penalty、deepseek_rank_score、blend_alpha。（现在这些字段生成了但没进验证库。）
2. 在 update_outcomes() 回填真实行情后，计算 DeepSeek 归因指标：
  - DeepSeek 判 avoid/veto 的票，次日/主周期净收益均值 —— 如果显著为负，说明否决有效；如果为正，说明 DeepSeek 在误杀。
  - priority 票 vs watch 票的净胜率差。
  - 反事实：local-only 排序 top-N 的净收益 vs DeepSeek-rerank 后 top-N 的净收益。这一个数字直接回答"DeepSeek 到底有没有用"。
3. 验证页新增一块 "DeepSeek 增益"，按 short_term / tomorrow_picks / swing_picks 分别显示。

产出：第一次能用数据说"DeepSeek 对明天推荐有 +X% 净胜率增益，对 2-5 天推荐是 -Y%（该关掉）"。

---
P1 — 用新闻/公告原文给 DeepSeek 的催化剂判断"接地"（对短线盈利增益最大）

现在 prompt 要 DeepSeek 判 theme_truth_score、catalyst_strength，但输入里根本没有新闻文本——这是最大的幻觉源，也是最大的机会。

1. 打开并强化 sentiment.py / ENABLE_MARKET_NEWS，为进入 rerank 池的每只票拉取当日新闻标题 + 公告类型（东财/同花顺已有接口）。
2. 在 _request_payload() 里加 recent_news（近1-2日标题列表）、announcement_flags（业绩预告/减持/解禁/问询函）字段。
3. Prompt 明确要求：theme_truth_score 必须引用具体新闻依据，无依据则判为"题材待证实"并降权。
4. 复用已有的 event_risk.py（解禁/质押/减持）作为硬扣分输入，让 DeepSeek 的 event_risk_score 有真实数据支撑，而不是猜。

为什么这条对"短线赚钱"最直接：A 股短线兑现高度依赖催化剂真伪和消息面风险（减持/解禁/问询往往是次日杀跌主因）。让 DeepSeek 读到真消息，才能真正规避"看着强、次日闷杀"的票。

---
P2 — 大盘 Go/No-Go 层（决定"今天出不出手"）

DeepSeek 现在只在池内排序，从不否决整个交易日。但短线最大的钱是"坏日子不出手"省下来的。

1. 新增一个每日一次的 DeepSeek 大盘判断（复用 market_regime + 涨跌家数/涨停晋级率/北向/两市成交额）：输出 risk_on / balanced / risk_off 与建议的推荐数量上限系数。
2. risk_off 日：自动把三类推荐上限从 18 压到（如）5，并抬高 TOMORROW_PRIMARY_MIN_SCORE。这套 gross 敞口缩放逻辑你在 portfolio.py 里（PORTFOLIO_GROSS_RISK_OFF=0.4）已经有了，只是没接到荐股数量上。
3. 同样落库 + 事后归因：risk_off 日实际市场表现如何，验证这个判断的准确率。

---
P3 — DeepSeek 影子规则 → 走 OOS 自动晋级（把建议变成可验证的策略改动）

现在 rule_candidates 只保存不用。把它接进你已经写好的 walk-forward harness。

1. review_strategy_validation() 产出的每条 {field, operator, threshold, penalty} 规则，喂给 calibrate_live_weights() 同款的 _walk_forward_evaluate()。
2. 复用现成晋级门槛：oos_best > oos_baseline + margin 且 positive_folds > fold_count//2 才写入 weights.json；否则永远停在影子态。
3. blend_alpha 本身也纳入校准——每个策略学一个自己的 alpha，DeepSeek 无 OOS 增益的策略 alpha 自动趋近 0（等于自动关掉无效的 LLM 干预）。

这是把 DeepSeek 建议转成"可验证盈利"的唯一正路：LLM 提假设，OOS 回测做裁判，人只在晋级前确认。

---
P4 — 个股预测 stance 回填 + 退出纪律联动

1. 把 /api/stock-prediction 的 stance（buy_trial 等）也落一份轻量快照，次日/N 日回填，统计"小仓试单"事后胜率——否则这个建议永远无法优化。
2. 让 DeepSeek 的 entry_plan / risk_controls 直接产出可执行的止损/止盈价，喂进 paper_trading.py 模拟持仓，用 EXIT_STOP_LOSS_PCT / EXIT_TAKE_PROFIT_PCT / EXIT_TRAILING_STOP_PCT 跑出含退出的真实净收益，而不只是"次日收盘"这种理想化口径。

---
四、落地顺序与验收

┌──────┬────────────────────────┬──────────────────────────────────────────────────────────┐
│ 阶段 │          内容          │                    验收标准（可量化）                    │
├──────┼────────────────────────┼──────────────────────────────────────────────────────────┤
│ P0   │ DeepSeek 归因落库 +    │ 验证页能显示三策略各自的"DeepSeek 净胜率增益"，样本≥20   │
│      │ 反事实对比             │                                                          │
├──────┼────────────────────────┼──────────────────────────────────────────────────────────┤
│ P1   │ 新闻/公告接地          │ theme_truth_score                                        │
│      │                        │ 必带新闻依据；接地后减持/解禁票的次日误荐率下降          │
├──────┼────────────────────────┼──────────────────────────────────────────────────────────┤
│ P2   │ 大盘 go/no-go          │ risk_off 日推荐数自动收缩；该判断事后准确率可查          │
├──────┼────────────────────────┼──────────────────────────────────────────────────────────┤
│ P3   │ 影子规则走 OOS 晋级    │ 每条采纳规则都有 OOS fold 记录；alpha 按策略自适应       │
├──────┼────────────────────────┼──────────────────────────────────────────────────────────┤
│ P4   │ stance 回填 + 退出模拟 │ 个股建议有事后胜率；净收益口径含止损止盈                 │
└──────┴────────────────────────┴──────────────────────────────────────────────────────────┘

判断整套优化是否成功的唯一标准（沿用文档第 7 节口径，加严）：
- 真实前瞻样本（非 replay）≥ 20/策略；
- 主周期净胜率（扣 VALIDATION_TRADE_COST_PCT=0.25% 成本 + 滑点）持续 > 50%；
- 开 DeepSeek 相比 local-only，OOS 净收益为正——否则就按数据把对应策略的 DeepSeek 关掉，这本身也是优化。

---
