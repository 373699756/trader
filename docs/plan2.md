按普通办公 PC 配置，计划就走“轻量本地规则 + 低频 DeepSeek + 验证闭环”，不引入 Qlib 训练和机器学
  习模型。

  目标

  本地规则负责找候选
  DeepSeek 负责少量排雷
  验证库负责裁判
  OOS 不提升就降权/关闭

  核心不是追求复杂模型，而是把每一次推荐都变成可验证样本。

  P0：先固化省钱运行模式

  目的：避免 DeepSeek 乱调用、重复调用。

  已完成方向继续固化：

  1. 大盘明确 risk_on/risk_off 时用本地 market gate。
  2. 只有大盘灰区才问 DeepSeek。
  3. 三策略推荐合并成一次 batched rerank。
  4. 验证页 GET 不触发 DeepSeek。
  5. 个股预测默认本地，只有点“优化建议”才调用 DeepSeek。
  6. DeepSeek 只复核 top 10-15 候选。

  验收：

  - 打开推荐页不会三策略分别打 API。
  - 打开验证页不会打 DeepSeek API。
  - 点击个股普通预测不会打 DeepSeek API。
  - 点击个股“优化建议”才打 DeepSeek API。

  P1：强化本地轻量因子

  目的：让本地先筛出更靠谱的候选，DeepSeek 只处理少量高价值样本。

  优先强化这些因子：

  趋势强度
  短期不过热
  流动性
  量价配合
  行业强度
  历史胜率
  事件风险
  大盘状态

  重点不是增加很多复杂指标，而是把已有分数拆清楚：

  local_score
  momentum_score
  trend_score
  liquidity_score
  risk_guard_score
  industry_score
  event_risk_score
  historical_edge_score
  overheat_penalty

  验收：

  - 推荐结果里能看到主要加分/扣分原因。
  - 过热票、低流动性票、事件风险票明显降权。
  - risk_off 日推荐数量减少。

  P2：DeepSeek 只复核冲突样本

  目的：减少 token 花费，把 DeepSeek 用在最有价值的地方。

  只送 DeepSeek 这些候选：

  本地高分但风险也高
  涨幅/换手/量比过热
  新闻/公告有风险
  三策略之间分歧明显
  大盘灰区
  进入 top 10-15 的候选

  DeepSeek 不做：

  不扫全市场
  不新增股票
  不直接决定买卖
  不生成正式权重
  不承诺收益

  DeepSeek 只输出：

  priority
  watch
  avoid
  veto
  penalty
  reason
  risk_flags

  验收：

  - DeepSeek 调用候选数量受控。
  - 每条 DeepSeek 判断都落库。
  - 被 veto/avoid 的票也进入 shadow 验证。
  - 验证页能看 DeepSeek 是否真的避开亏损。

  P3：验证库每日闭环

  目的：让系统知道自己到底有没有用。

  每天保存：

  推荐日期
  策略名
  股票代码
  本地分数
  最终分数
  DeepSeek action
  DeepSeek penalty
  是否 shadow veto
  大盘 gate
  推荐理由

  次日/数日后回填：

  次日开盘收益
  次日收盘收益
  3日收益
  5日收益
  止损/止盈退出收益
  最大回撤

  验收：

  - 三策略都有真实样本。
  - DeepSeek veto/avoid 有真实收益归因。
  - 大盘 gate 有命中率。
  - 可以比较 local-only vs local+DeepSeek。

  P4：每周 OOS 调权

  目的：不靠感觉改参数，靠样本外结果改。

  每周跑一次：

  calibrate.py

  只调小范围参数：

  趋势权重
  流动性权重
  过热惩罚
  风险惩罚
  明天推荐最低分
  波段策略热度限制
  DeepSeek blend_alpha

  规则：

  样本外收益提升
  多数 fold 为正
  真实样本数达到门槛
  待回填样本不能太多

  通过后：

  进入人工确认
  不自动改正式策略

  验收：

  - 每个建议都有 OOS 记录。
  - 没通过 OOS 的建议保持 shadow。
  - DeepSeek 没有贡献的策略 alpha 降到 0。

  P5：前端看板收敛到决策信息

  目的：普通 PC 用起来要轻，不要堆复杂信息。

  推荐页重点显示：

  策略
  排名
  本地分
  DeepSeek action
  风险标签
  大盘 gate
  推荐数量变化

  验证页重点显示：

  主周期净收益
  净胜率
  DeepSeek 增益
  avoid/veto 平均收益
  大盘 gate 命中率
  待回填样本数

  个股页：

  默认本地预测
  手动 DeepSeek 优化
  显示止损/止盈/回避条件

  验收：

  - 普通预测不花 DeepSeek。
  - 深度优化明确手动触发。
  - 验证页打开不花 DeepSeek。

  P6：费用控制规则

  建议默认配置：

  ENABLE_DEEPSEEK_RUNTIME=1
  DEEPSEEK_ENABLED=1
  DEEPSEEK_CACHE_ENABLED=1
  DEEPSEEK_CACHE_TTL_SECONDS=86400
  DEEPSEEK_REVIEW_LIMIT=12
  DEEPSEEK_MAX_TOKENS=900
  DEEPSEEK_RETRY_COUNT=1
  DEEPSEEK_VALIDATION_RETRY_COUNT=0

  ENABLE_DEEPSEEK_MARKET_GATE=1
  ENABLE_DEEPSEEK_NEWS_CONTEXT=0
  ENABLE_STANCE_TRACKING=1

  更省钱配置：

  DEEPSEEK_STRATEGIES=tomorrow_picks,swing_picks

  暂不开：

  ENABLE_DEEPSEEK_NEWS_CONTEXT=1

  等 DeepSeek 归因稳定为正后，再考虑只对 top 5 开新闻上下文。

  P7：执行节奏

  第一阶段：1-2 天
  确认当前省钱调用模式全部生效，补调用次数测试。

  第二阶段：3-5 天
  梳理本地因子，把加分/扣分原因和分数字段稳定下来。

  第三阶段：1 周
  连续保存推荐快照，开始积累真实样本。

  第四阶段：第 2 周
  跑第一次 OOS 调权，只允许 shadow 或人工确认。

  第五阶段：第 3-4 周
  根据验证结果决定：

  - 哪个策略保留 DeepSeek。
  - 哪个策略 DeepSeek alpha 降到 0。
  - 哪些本地风险规则应强化。

  最终验收标准

  至少看 20 个真实前瞻样本/策略后再判断：

  risk_off 是否减少亏损
  主周期净胜率是否改善
  净收益是否扣成本后仍提升
  最大回撤是否下降

  如果不满足，就不是“继续加 DeepSeek”，而是：

  关掉无效策略的 DeepSeek
  降低 alpha
  只保留大盘 gate 或事件风险复核

  这套计划适合普通办公 PC：不训练模型，不吃 CPU/GPU，把算力和 API 钱都花在最可能产生收益的地方。

  这跟现在的策略有什么不同


• 和现在相比，方向不是换一套策略，而是把现有策略收敛成更省钱、更可验证、更本地优先的运行方式。

  核心区别如下。

  1. DeepSeek 从“增强排序器”变成“少量排雷器”

  现在：

  本地三策略出候选
  DeepSeek 批量 rerank
  DeepSeek 影响最终排名

  计划后：

  本地三策略仍然负责主要排序
  DeepSeek 只复核 top 冲突/高风险候选
  DeepSeek 重点做 avoid/veto/penalty

  也就是说，DeepSeek 的角色从“帮你选更好的票”更多转成“帮你少踩坑”。

  2. 调用策略更省钱

  现在已经改了一部分：

  三策略 batched rerank
  market gate 明确态不问 DeepSeek
  验证页 GET 不问 DeepSeek
  个股预测默认不问 DeepSeek

  计划里要进一步固定成规则：

  只在大盘灰区问 DeepSeek
  只对 top 10-15 问 DeepSeek
  只在手动或日终任务问验证复盘
  新闻上下文默认不开

  差别是：现在代码支持省钱模式；计划是把它作为长期运行纪律。

  3. 本地因子会承担更多责任

  现在：

  本地规则打分 + DeepSeek rerank

  计划后：

  本地规则打分质量必须更强
  DeepSeek 不负责弥补本地策略弱点

  重点会放在：

  趋势强度
  不过热
  流动性
  行业强度
  事件风险
  历史胜率
  大盘状态

  如果本地筛出来的候选质量不行，DeepSeek 不应该兜底。

  4. DeepSeek 只处理“本地规则不好判断”的问题

  现在 DeepSeek 可能对普通 top 候选都做排序判断。

  计划后，只优先处理这些：

  本地分高但风险高
  涨幅/换手/量比过热
  三策略分歧
  大盘灰区
  公告/新闻风险

  普通、低冲突、低风险的候选不需要花钱问 DeepSeek。

  5. 验证闭环更严格

  现在已经有：

  DeepSeek 归因
  shadow veto 回填
  market gate 命中率
  OOS 调权

  计划后要把它作为上线门槛：

  DeepSeek 有正贡献 → 保留
  DeepSeek 无贡献 → alpha 降到 0
  avoid/veto 没避开亏损 → 降低 veto 权限
  market gate 不准 → 改成本地规则或关闭

  也就是说，不凭感觉信 DeepSeek。

  6. 不引入机器学习

  和之前提到的 Qlib + LGBModel 不同，这个计划明确不做：

  不训练模型
  不跑深度学习
  不引入大规模因子训练

  只用办公 PC 能承受的：

  pandas 轻量因子
  规则打分
  SQLite 验证库
  少量 DeepSeek API

  一句话区别

  本地规则 + DeepSeek 批量增强 + 验证闭环刚补上

  计划后的策略是：

  本地规则为主
  DeepSeek 只低频排雷
  验证结果决定 DeepSeek 是否继续有权重
  普通 PC 能长期稳定跑

  不是大换血，而是把当前系统从“能用 DeepSeek”调整为“少花钱、少过拟合、按数据决定是否用
  DeepSeek”。


› 你这策略比现在的更高明吗？短期内有更高收益？


• 不一定更高明，也不能保证短期收益更高。更准确地说：

  这个方案不是“更激进地提高收益”
  而是“降低 DeepSeek 误用成本，减少坏样本，提升收益/风险比”

  短期收益是否更高，要靠真实样本验证。

  和现在相比，它可能带来的收益点

  1. 减少误杀和误追
     现在 DeepSeek 参与 rerank，如果判断错，可能把好票压下去，也可能把一般票抬上来。
     新方案把 DeepSeek 更多限制在 avoid/veto/penalty，它不轻易重排所有票，风险更低。

  2. 坏日子少出手
     短线亏损很多来自大盘环境差。
     market gate 如果判断有效，收益提升不是来自“选中大牛股”，而是少做亏钱交易。

  3. 减少高位过热票
     对短线来说，涨幅、换手、量比过热很容易次日回落。
     如果 DeepSeek 和本地规则能更稳定识别这类票，净收益可能改善。

  4. 省钱后可以长期跑
     如果 DeepSeek 成本太高，策略无法稳定积累样本。省调用不是单纯省钱，而是让系统能长期验证和调
     参。

  但它也可能降低短期收益

  因为它更保守：

  risk_off 日少推荐
  高风险票被降权
  DeepSeek 不再大幅拉升某些强势票

  如果市场处在强题材、强投机阶段，保守过滤可能会错过一些短线高弹性票。

  所以它更可能改善：

  回撤
  亏损天数
  错误推荐率
  净收益稳定性

  不一定提升：

  单日最高收益
  极端行情里的爆发收益

  真正要比较的是这四组

  不能靠感觉判断“更高明”。应该让系统并行记录：

  A. local-only
  B. 当前 DeepSeek rerank
  C. 新方案：本地为主 + DeepSeek 排雷
  D. 新方案 + market gate

  然后看真实前瞻样本：

  净收益
  净胜率
  最大回撤
  DeepSeek 增益
  avoid/veto 是否有效
  risk_off 是否减少亏损

  如果 C/D 明显优于 B，才说明新方案更好。

  我的判断

  短期内，如果你追求“多抓强势票”，现在的 DeepSeek rerank 可能更激进。

  如果你追求：

  少亏
  少踩雷
  减少 DeepSeek 误判

  建议不要一次替换

  最稳做法是保留对照：

  正式展示：新方案
  shadow 记录：当前 DeepSeek rerank 反事实

  跑 20-40 个真实前瞻样本后再决定。

  如果数据显示当前 rerank 更赚钱，就保留当前方式；如果新方案回撤更小、净收益更稳，就切新方案。不
  要凭假设切。


› 1. 每日 1 次 market gate：只在本地大盘宽度处于灰区时问 DeepSeek；明确 risk_on/risk_off 的日子
  用
       本地规则。

    2. 后台 1 次 batched rerank：三策略合并，最多 top 10-15/策略，候选去重后发送。
    3. 日终/收盘后 1 次 validation review：不要在页面 GET 里触发。
    4. 个股 DeepSeek 改成手动按钮：默认不跑；已入选推荐榜或用户明确点击才跑。

