DeepSeek 荐股优化：P0–P4 分阶段实施计划

Context（为什么做这件事）

现有系统（stock_analyzer/）已经是一套成熟的三策略量化看板：short_term（今天）/ tomorrow_picks（明天）/ swing_picks（2-5天），本地打分 + DeepSeek 复核 + 验证库回填 + 走前向 OOS 权重校准（calibrate.py）都已存在。

但 DeepSeek 目前是"只说不认账"：
- rerank_candidates() 用固定 blend_alpha=0.30 混合、硬阈值 gate，从未验证过它的 priority/avoid/veto 事后是否真的提高净胜率。
- review_strategy_validation() 产出的 rule_candidates 只作为影子建议保存（strategy_tuning.py:_deepseek_rule_suggestions，can_apply=False），从不回测、从不采纳。
- prompt 要 DeepSeek 判 theme_truth_score/catalyst_strength，但输入里没有新闻原文（ENABLE_MARKET_NEWS=0），等于让它编题材，幻觉风险最高。
- DeepSeek 只在已过滤池内排序，从不参与"今天该不该出手"的大盘 go/no-go，而这是 A 股短线盈亏最大来源。
- 个股预测 stance（小仓试单等）从不回填打分。

目标：把 DeepSeek 从"没人验证的建议器"改造成"经过样本外验证、可归因、能被证伪的正期望增强层"。

▎ 诚实边界：没有任何做法能"确保"盈利，文档本身也写"不保证盈利"。本计划能保证的是可验证——用数据证明 DeepSeek 在每个策略上到底加了还是减了 alpha，并据此自动提权/降权。任何声称保证赚钱的做法都不可信。

全局原则（每个阶段都遵守）

1. 本地量化保底：DeepSeek 任何一环失败都降级到纯本地，绝不中断荐股主流程（复用现有 fallback 模式）。
2. 先归因，后信任：任何 DeepSeek 干预上线前，必须能事后用真实样本打分。
3. OOS 裁判：任何"采纳建议 / 改权重 / 调 alpha"必须样本外优于基线且多数 fold 为正，复用 calibrate.py 现成门槛（CALIBRATE_IMPROVE_MARGIN、positive_folds > fold_count//2）。
4. 改动可开关：每个新能力挂独立 env flag，默认保守，可一键回退。
5. 不碰红线（沿用文档 8.4）：不实盘下单、不让 DeepSeek 自动改正式权重（只经 OOS 晋级）。

---
P0 — DeepSeek 归因闭环（基础，最高优先级）

目的：第一次能用数据回答"DeepSeek 对每个策略到底有没有用"。

关键前提（已确认，降低风险）：save_signals() 已把整行 dict 存进 raw_json（strategy_validation.py:129），metrics() 已把 raw_json 解析回 _raw（:611）。所以 deepseek_action/deepseek_veto/deepseek_penalty/deepseek_rank_score/rerank_source 已经落库，无需新增列。

改动点：
1. 捕获 local_rank：在 deepseek_client.py:_merge_ranking_rows() 排序之前，把每行 rerank 前的本地名次写入 row["local_rank"]（本地按 base_score 排序的位次）。同时确保 blend_alpha、deepseek_covered(bool) 落到行上。这些随 raw_json 自动入库。
2. 新增归因指标：在 strategy_validation.py 新增 deepseek_attribution(strategy_name, days)：
  - 从 _raw 读 deepseek_action/veto，join 已回填的 _primary_return_net。
  - 计算：avoid/veto 票的平均净收益（<0 = 否决有效）、priority vs watch 净胜率差、以及反事实：按 local_rank 取 top-N 的净收益 vs 按最终 rank 取 top-N 的净收益之差（= DeepSeek 排序增益）。
  - 样本门控复用 strategy_tuning.py 口径：real_sample_count>=10 才给结论，否则标 insufficient。
3. UI：验证页（templates/ + app.py 的 /api/strategy-validation）加一块"DeepSeek 增益"，三策略分列，显示上面三个数字 + 样本量。
4. 自适应 alpha 的数据来源：把归因结果写入 strategy_status.json 旁的小档（或复用 strategy_tuning_runs.metrics_json），供 P3 的 alpha 自适应读取。

代表文件：deepseek_client.py、strategy_validation.py、app.py、templates/*validation*.html。

验收：验证页显示三策略各自的"DeepSeek 净胜率增益"和反事实排序增益，样本≥20 时给明确结论。

---
P1 — 新闻/公告给 DeepSeek 催化剂判断"接地"（对短线增益最直接）

目的：消除 theme_truth_score/catalyst_strength 的幻觉，让 DeepSeek 基于真消息规避"看着强、次日闷杀"的票。

已确认可行：providers.py:141 get_stock_news() / :169 get_market_news() 已从东财免费接口拉新闻；sentiment.py:102 score_stock_sentiment() 已能打分。P1 是接线不是新爬虫。

改动点：
1. 新增新闻缓存层：仿 event_risk.py 的缓存模式，新增 news_cache（复用 sentiment provider），对进入 rerank 池的票批量拉近 1-2 日标题 + 公告类型，带 TTL（新增 NEWS_CACHE_HOURS）。限流：只对进入 DEEPSEEK_REVIEW_LIMIT(20) 的池子拉，控制请求数。
2. payload 注入：deepseek_client.py:_request_payload() 每候选加 recent_news（标题列表，截断）、announcement_flags（业绩预告/减持/解禁/问询/质押，来自 event_risk.py 已有数据）、news_sentiment（score_news_items 分）。
3. prompt 强化：_build_messages() 明确要求 theme_truth_score 必须引用具体新闻依据，无依据 → 判"题材待证实"并降权；event_risk_score 以传入的真实 flags 为准。
4. 开关：新增 ENABLE_DEEPSEEK_NEWS_CONTEXT（默认 0，联调后开）；失败静默降级为无新闻（现状行为）。

代表文件：sentiment.py/providers.py（拉取+缓存）、deepseek_client.py（payload+prompt）、config.py（flag）。

验收：payload 含真实新闻；开启后，event_risk 命中（减持/解禁/问询）的票被 DeepSeek 降权/剔除比例上升，且 P0 归因显示这些票次日净收益确为负（否决有效）。

---
P2 — 大盘 Go/No-Go 层（决定"今天出不出手"）

目的：短线最大的钱是"坏日子不出手"省下来的。让 DeepSeek 每日一次判大盘，坏日子自动收缩推荐。

改动点：
1. 每日大盘判断：新增 deepseek_client.py:review_market_regime(context)，输入现有 market_regime + 涨跌家数/涨停晋级率/两市成交额/北向（能取到的用现有 provider，取不到则跳过该字段），输出 regime ∈ {risk_on, balanced, risk_off} + size_factor(0-1) + 理由。每日缓存一次（复用 rerank 的日期缓存机制）。
2. 接入荐股数量：recommendation_runtime_support.py:build_recommendation_horizons() 在 finalize 时按 size_factor 缩放三策略 display_count，并在 risk_off 日抬高 TOMORROW_PRIMARY_MIN_SCORE。缩放系数复用 portfolio.py 已有的 PORTFOLIO_GROSS_RISK_OFF=0.4 等常量语义。
3. 落库 + 归因：把每日 regime 判断存入验证库（复用 strategy_tuning_runs 或新增轻量表），事后用当日市场真实表现回填，统计判断准确率。
4. 开关：ENABLE_DEEPSEEK_MARKET_GATE（默认 0）；关闭时完全不改变现有数量逻辑。

代表文件：deepseek_client.py、recommendation_runtime_support.py、app_runtime_support.py、config.py。

验收：risk_off 日推荐数自动收缩；regime 判断准确率可在验证页查看。

---
P3 — DeepSeek 影子规则 → OOS 自动晋级 + alpha 自适应

目的：把 rule_candidates 从"只保存"变成"经样本外验证才采纳"；把固定 blend_alpha 变成每策略自学。

已确认可复用：calibrate.py:_walk_forward_evaluate()/_evaluate_live_samples()/_walk_forward_splits() + 晋级门槛（calibrate_live_weights 里 oos_best > oos_baseline + margin 且 positive_folds > fold_count//2 才 _write_weights_override）。

改动点：
1. 规则 OOS 评估：扩展 calibrate.py:_evaluate_live_samples()（或包一层）使其能接受一条 DeepSeek 惩罚规则 {field, operator, threshold, penalty}，在 test fold 上比较"加规则 vs 不加规则"的 _objective 差。
2. 晋级流程：新增 calibrate.py:evaluate_deepseek_rule(strategy, rule, samples)，跑 _walk_forward_evaluate 同款 fold 逻辑；仅当 OOS 为正且多数 fold 改善才把规则写入 weights.json（或规则覆盖档），否则停在影子态。strategy_tuning.py:_deepseek_rule_suggestions() 的产物改为候选，由此函数裁决 can_apply。
3. alpha 自适应：新增 calibrate.py:calibrate_blend_alpha(strategy, samples)，用 P0 归因数据在 α∈{0,0.15,0.3,0.45} 上做 OOS 网格，选 OOS 最优；DeepSeek 无增益的策略 α→0（等于自动关掉无效 LLM 干预）。结果写入 weights.json，_merge_ranking_rows 读取按策略 α。
4. 人工确认闸：晋级候选默认进 strategy_tuning_runs（can_apply=True, shadow_mode=False 待确认），保留文档 8.4"不自动改正式权重"的人工确认环节——OOS 通过只是"允许采纳"，落地仍需现有确认动作。

代表文件：calibrate.py、strategy_tuning.py、deepseek_client.py（读按策略 α）、config.py。

验收：每条采纳规则都有 OOS fold 记录；blend_alpha 按策略自适应；无增益策略 α 自动降到 0。

---
P4 — 个股 stance 回填 + 退出纪律联动

目的：让个股建议也能被验证；用含止损止盈的真实口径衡量净收益。

改动点：
1. stance 快照：/api/stock-prediction 返回的 optimization.stance（stock_optimization.py）落一份轻量快照（新增小表或复用 signals 表加 tag），次日/N 日回填，统计"小仓试单"事后净胜率。
2. 退出联动：让 DeepSeek entry_plan/risk_controls 产出可执行止损/止盈价，喂进 paper_trading.py，用现有 risk_rules.simulate_exit（stop 5%/take 8%/trail 4%，EXIT_*_PCT）跑含退出的真实净收益，替代"次日收盘"理想口径。
3. 开关：ENABLE_STANCE_TRACKING（默认 0）。

代表文件：stock_optimization.py、app.py、strategy_validation.py、paper_trading.py。

验收：个股建议有事后净胜率；净收益口径含止损止盈。

---
落地顺序

严格按 P0 → P1 → P2 → P3 → P4，每阶段独立可验证、独立可回退。P0 是其余阶段的度量基础，必须先稳。每阶段完成后用下方 Verification 验证再进下一阶段。

Verification（每阶段通用）

1. 单测：pytest（现有 tests/，pytest.ini 已配）；为每个新函数（deepseek_attribution、evaluate_deepseek_rule、calibrate_blend_alpha、review_market_regime）加针对性单测，用构造样本断言归因/OOS 逻辑正确。
2. 端到端：./run.sh 起本地看板，命中相关接口：
  - P0：GET /api/strategy-validation?strategy=tomorrow_picks 看"DeepSeek 增益"块。
  - P1：GET /api/recommendations?top_n=18 后查日志/meta 确认 payload 含新闻、event flags 生效。
  - P2：模拟 risk_off 看推荐数收缩。
  - P3：POST /api/strategy-validation/tuning?strategy=tomorrow_picks 看 OOS fold 记录与 can_apply 裁决。
3. 降级回归：临时 ENABLE_DEEPSEEK_RUNTIME=0 及各新 flag=0，确认三策略仍正常产出（保底不破）。
4. 数据安全：改动前用 daily_job --list-validation-backups 确认有备份；任何 schema 变更走 _init_db 的 ALTER 幂等模式（现有代码已用此模式加列），不破坏历史样本。

不做（保持红线）

不实盘下单、不做全市场分钟级训练、不优先 RL、不让 DeepSeek 直接改正式权重（只经 OOS + 人工确认晋级）。
</content>
</invoke>


使用deepseek利益最优化，另外这个文档里面哪些开源库里面哪些策略可 以和deepseek结合产生好收益
