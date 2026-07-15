# DeepSeek 全策略综合荐股 + Today 9:36 执行完整计划

## Review 结论

- 本计划合并了此前 `money.md` 中的 DeepSeek 候选发现方案、五维结构化研究、25% 综合评分、前端透明展示要求，以及后续新增的 today 9:36 执行窗口和每日 188 次全策略 DeepSeek API 预算。
- 方案没有让 DeepSeek 绕过本地硬过滤，也不允许 DeepSeek 凭空新增股票；它只能从系统点时候选池中发现候选、做五维结构化评分和风险扣分。
- 188 次是全系统每日 DeepSeek API 调用硬上限，不是 today 单独上限；常规日不需要打满，剧烈行情日允许接近硬上限。
- 该方案不能保证收益，但比纯本地更有机会减少漏选，并通过硬过滤、veto、缓存和预算隔离控制 DeepSeek 幻觉风险。

## Summary

- DeepSeek 可以发现本地漏掉的候选，但只能从系统点时候选池中选择，不能新增候选池外股票。
- `today_term` 从 09:36 才允许可执行推荐，09:30-09:35 只观察。
- DeepSeek 现在以 25% 权重参与最终综合评分：`final_score = local_score * 0.75 + deepseek_score * 0.25 - risk_penalty`。
- DeepSeek 推荐股票必须再次通过本地硬过滤和策略周期过滤。
- 前端必须展示 DeepSeek 是否参与、25% 权重、总调用数、分策略调用数、审查数量、覆盖率和失败原因。

## Runtime Flow

- 09:15-09:25：共享预热
  - 服务 today、tomorrow、swing、long_term。
  - 预审新闻、公告、政策、财务、风险事件。
  - 预算上限 15 次。
- 09:30-09:35：today 开盘观察
  - 不产生可执行推荐。
  - 审查开盘异动、追高风险、封板失败、资金背离。
  - 预算上限 15 次。
- 09:36-10:30：today 主执行窗口
  - 允许可执行推荐。
  - 本地行情高频刷新，DeepSeek 只审新增/变化/高风险候选。
  - 预算上限 42 次。
- 10:30-11:20：today 降级执行窗口
  - 可执行门槛提高。
  - DeepSeek 未覆盖且高风险候选默认降为观察。
  - 预算上限 13 次。
- 13:00-14:00：午后三策略主审
  - tomorrow：次日催化、隔夜风险、利好兑现。
  - swing：2-5 日持续性、行业政策、财务健康。
  - long_term：低估值、卡脖子、国产替代、政策扶持、龙头。
  - 预算上限 65 次。
- 14:20：最终补审
  - 只审新进入最终候选、证据变化、风险变化、未覆盖但准备进入推荐的股票。
  - 预算上限 38 次。
- 全天突发行情预留
  - 监管、减持、公告、业绩、政策突发。
  - 预算上限 5 次。
- 15:00 后
  - 盘后补缺仍使用收盘行情。
  - DeepSeek 只使用当日点时证据，不伪造盘后新事实。

## Budget Allocation

- 每日总硬上限：
  - `DEEPSEEK_DAILY_API_HARD_LIMIT = 188`
- 分策略硬上限：
  - `today_term = 70`
  - `tomorrow_picks = 45`
  - `swing_picks = 35`
  - `long_term_watch = 18`
  - `shared_preheat = 15`
  - `emergency_reserved = 5`
- 按时间窗口硬上限：
  - `09:15-09:25 shared_preheat = 15`
  - `09:30-09:35 today_open_observe = 15`
  - `09:36-10:30 today_main = 42`
  - `10:30-11:20 today_late = 13`
  - `13:00-14:00 afternoon_main = 65`
  - `14:20 final_supplement = 38`
  - `emergency_reserved = 5`
- 使用原则：
  - 常规日不必打满 188。
  - 剧烈行情日允许接近 188。
  - today 不得抢占 tomorrow、swing、long_term 的保底预算。
  - 共享候选和缓存优先复用，减少重复调用。

## Candidate Construction

- DeepSeek 候选池由本地先构建，默认 80-120 只。
- 候选来源：
  - 三策略本地高分股；
  - 追高风险股；
  - 新进入候选；
  - 新闻/公告/财务/政策证据较强股；
  - 午后资金结构明显变化股；
  - 长期低估值/国产替代/卡脖子龙头候选。
- 本地预过滤：
  - ST/退市风险；
  - 停牌；
  - 流动性不足；
  - 风险黑名单；
  - 明显异常行情；
  - 明显不可交易股票。
- DeepSeek 返回候选池外股票必须丢弃并记录错误。
- DeepSeek 推荐股票必须再次通过本地硬过滤和策略周期过滤。

## DeepSeek Scoring And Merge

- 统一综合公式：
  - `final_score = local_score * 0.75 + deepseek_score * 0.25 - risk_penalty`
- 字段定义：
  - `local_score`：本地策略评分。
  - `deepseek_score`：DeepSeek 五维结构化评分，0-100。
  - `risk_penalty`：DeepSeek 与本地风险综合扣分，0-30。
  - `final_score`：最终排序分。
- 强风险优先：
  - `deepseek_veto=true` 的股票不能进入可执行 TopN。
  - DeepSeek 高分不能抵消本地硬过滤。
  - DeepSeek 缺证据不能高分。
  - DeepSeek API 失败时回退本地分。
- DeepSeek 推荐但本地不通过的股票：
  - 不进入最终推荐；
  - 可进入观察池；
  - 前端显示不可执行原因。

## Five-Dimensional Research

- 三策略共享同一份点时证据缓存，但按各自周期解释。
- 五维结构：
  - 价值与质量：估值、ROE、毛利、负债、价值分、质量分。
  - 财务健康：收入利润趋势、经营现金流、自由现金流、应收/存货、商誉、债务。
  - 市场与资金：涨幅、速度、量比、换手、振幅、5/10/20 日趋势、主力资金、委比。
  - 行业与政策：行业景气、竞争位置、成长空间、政策证据。
  - 综合风险：减持、解禁、监管、诉讼、估值透支、拥挤交易、追高风险。
- 缺失约束：
  - 无有效估值/质量指标：`value_quality.assessment=unknown`。
  - 无现金流：`financial_health.cashflow_trend=unknown`。
  - 无经营主数据：`financial_health.profit_trend=unknown`。
  - 无主力资金：`market_flow.flow_health=unknown`，`price_flow_divergence=false`。
  - 无政策 evidence：`industry_policy.policy_relevance=unknown`。
  - 无证据：`abstain=true`，不得高分。

## Strategy Rules

- `today_term`
  - 09:30-09:35：观察期，`execution_allowed=false`。
  - 09:36-10:30：主执行窗口，允许可执行推荐。
  - 10:30-11:20：降级执行窗口，执行门槛提高。
  - 13:00-14:00：主要做观察、持有/降级、转入 tomorrow/swing 候选。
  - DeepSeek 重点识别追高、冲高回落、资金背离、封板失败、当天兑现。
- `tomorrow_picks`
  - DeepSeek 重点识别次日催化、隔夜风险、当天利好是否已兑现。
  - 只支持 long_term 的逻辑不能推入明日池。
  - 隔夜风险高或利好已兑现时，必须扣分或 veto。
- `swing_picks`
  - DeepSeek 重点识别催化是否能持续 2-5 日。
  - 只支持 today 的短线情绪不能推入 swing 池。
  - 财务健康、行业政策、趋势持续性权重更高。
- `long_term_watch`
  - DeepSeek 辅助识别低估值、卡脖子、国产替代、政策扶持龙头。
  - 只做观察，不产生交易动作。

## Cache And Call Dedup

- 缓存 key：
  - `code + evidence_hash + prompt_version + model + research_input_version`
- 不重复调用条件：
  - evidence hash 未变化；
  - 10 分钟内已审；
  - 本地分变化小于 5 分；
  - 风险状态未变化；
  - 同一 prompt/model/research_input_version 已缓存。
- 允许重新审查条件：
  - 新公告/新闻/政策；
  - 风险事件新增；
  - 本地分变化超过 8 分；
  - 观察池进入可执行池；
  - today 候选转入 tomorrow/swing；
  - 午后资金结构明显变化。

## Prompt Policy

- DeepSeek system prompt 必须包含：
  - 只能使用输入 candidates 和 evidence。
  - 不得新增股票。
  - 不得编造政策、订单、资金、财务、公告事实。
  - 不得输出目标价、保证收益、直接买卖指令。
  - 无证据必须 abstain。
  - evidence_ids 必须来自输入。
- DeepSeek 输出必须为 JSON：
  - `code`
  - `strategy_fit`
  - `horizon_fit`
  - `deepseek_score`
  - `confidence`
  - `veto`
  - `risk_penalty`
  - `evidence_ids`
  - `risk_flags`
  - `reason`
  - 五维结构化字段。
- DeepSeek 必须显式判断：
  - 是否追高；
  - 是否利好已兑现；
  - 是否资金背离；
  - 是否周期匹配；
  - 是否存在减持/解禁/监管/诉讼/财务恶化；
  - 是否有真实政策、订单、业绩、行业证据支撑。

## API And Data Contract

- 推荐 API `meta.deepseek` 增加：
  - `enabled`
  - `production_applied=true`
  - `weight=0.25`
  - `daily_limit=188`
  - `used`
  - `remaining`
  - `usage_by_strategy`
  - `status`
  - `requested`
  - `reviewed`
  - `coverage_pct`
  - `abstain_count`
  - `cache_hit_count`
  - `last_batch_id`
  - `completed_at`
  - `error_type`
  - `error_message`
- 每行股票增加：
  - `local_score`
  - `deepseek_score`
  - `risk_penalty`
  - `final_score`
  - `deepseek_selected`
  - `deepseek_feature_status`
  - `deepseek_veto`
  - `deepseek_reason`
  - 五维结构化字段。
- DeepSeek 状态枚举：
  - `precomputed`
  - `cache_hit`
  - `local_only`
  - `abstain`
  - `daily_call_limit`
  - `deadline_skipped`
  - `disabled`
  - `error`

## Frontend Display

- 推荐池顶部显示：
  - DeepSeek 是否参与；
  - 当前权重 `25%`；
  - 今日 API 已用次数；
  - 今日 API 剩余额度；
  - 分策略调用数；
  - 审查数量 `reviewed/requested`；
  - 覆盖率；
  - 最近批次；
  - 完成时间；
  - 失败原因。
- Today 页面额外显示当前阶段：
  - 开盘观察期；
  - 主执行窗口；
  - 降级执行窗口；
  - 午后观察；
  - 仅观察。
- 每只股票显示：
  - 本地分；
  - DeepSeek 分；
  - 风险扣分；
  - 最终分；
  - 是否 DeepSeek 推荐；
  - 是否 veto；
  - 五维摘要；
  - 不可执行原因。
- DeepSeek 未参与时必须明确显示：
  - 仅本地策略；
  - API 失败；
  - 额度用尽；
  - 无证据放弃；
  - 缓存缺失；
  - 超时跳过。

## Risk Controls

- DeepSeek 不能绕过本地硬过滤。
- DeepSeek 高分不能抵消强风险 veto。
- DeepSeek 缺证据不能高分。
- DeepSeek API 失败时推荐接口必须正常返回本地结果。
- DeepSeek 推荐候选必须保存来源、证据、prompt 版本、model、request hash。
- today 不能抢占 tomorrow、swing、long_term 的保底预算。

## Test Plan

- 预算：
  - 每日 DeepSeek API 总调用数不超过 188。
  - 分策略预算耗尽后，该策略不再调用 DeepSeek。
  - today 不能抢占 tomorrow、swing、long_term 的保底预算。
- Today：
  - 09:30-09:35 不产生可执行推荐。
  - 09:36 后才允许 `execution_allowed=true`。
  - 10:30 后执行门槛提高，高风险未覆盖候选降为观察。
- 候选：
  - DeepSeek 返回候选池外股票时必须丢弃。
  - DeepSeek 推荐股票必须再次通过本地硬过滤。
  - DeepSeek 高分不能绕过本地硬过滤。
- 缓存：
  - 同一 evidence hash、prompt、model、research_input_version 不重复调用。
  - 证据变化或风险变化时允许重新审查。
- 综合评分：
  - `final_score` 必须按 `75% local + 25% deepseek - penalty` 计算。
  - DeepSeek veto 股票不能进入可执行 TopN。
- 降级：
  - DeepSeek API 失败、超时、额度用尽时，推荐接口返回本地推荐。
  - 前端必须显示失败原因。
- 前端：
  - 显示总调用数、剩余额度、分策略调用数、覆盖率、失败原因。
  - 显示 DeepSeek 是否参与和 25% 权重。
  - Today 显示当前阶段和不可执行原因。
- 统计：
  - 保存纯本地推荐、DeepSeek 推荐、综合 `final_score` 推荐三组结果。
  - 后续按策略、日期、snapshot_phase、baseline 分组评估收益和回撤。

## Assumptions

- 188 是全系统每日 DeepSeek API 调用硬上限，不是 today 单独上限。
- 常规日不需要打满 188；剧烈行情日允许接近硬上限。
- DeepSeek 当前直接以 25% 权重参与综合评分。
- 本地策略仍负责可交易性、硬过滤、执行窗口和最终风险边界。
- 该方案不能保证收益，但比纯本地更有机会减少漏选，并通过硬过滤、veto、缓存和预算隔离控制 DeepSeek 风险。
