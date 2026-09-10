# 评分链实现复核（2026-09-10）

本报告逐节对照 `docs/01_评分逻辑.md` 与活动源码 `src/trader`。最初复核基线为提交 `4c7a6b6`；本次又在
`a93851dfeb7f75948d70a0c9fe64b1d16376b053`（与上游一致、工作树起始干净）复核候选资格、容量研究、留出、
Shadow、生产授权及权重来源。“已实现”表示活动代码、类型边界和自动化契约已经存在，不代表尚缺的真实历史
数据、样本外收益证据、Shadow 结果或人工生产授权已经取得。候选、公式和排名定义仍只以评分逻辑文档为权威，
本报告只记录实现状态。

## 结论

整份评分逻辑尚未全部实现。第 2–10 节对应的生产决策主链已有活动实现，本批修复四处可复现偏差后，未再发现
该主链存在已知的规则级不一致；第 3 节中依赖真实历史点时来源的资料覆盖、第 6.3 节 V3 合格工件、第 11–12 节
研究与收益证明链仍未闭合。系统对这些缺口保持失败关闭，不能把“类型和流程代码已存在”写成“收益已验证”或
“已获生产权限”。

## 用户四批计划对照

### 批次一：资格顺序与候选单一所有权

状态：生产行为已实现，当前验收未完全闭合。

| 要求 | 当前状态 | 代码与测试证据 |
| --- | --- | --- |
| 资格先于候选分和每板上限 | 已实现 | `plan_scored_candidates()` 先调用统一 `hard_filter()`，再检查策略历史、活动模型字段、核心缺失率和最低分，最后由 `limited_codes()` 取每板窗口；领域/应用定向回归通过 |
| Today/Tomorrow/D25 独立候选 | 已实现 | `build_candidate_plans()` 对三策略分别形成 `ScoredCandidatePlan`；`InputBatch.requested_codes` 和候选特征按策略保存 |
| 单一候选所有者与配置顶层双真相删除 | 已实现 | `candidate_planning.py` 只编排领域规划；活动 `input_runtime.py` 无 `_CANDIDATE_WEIGHTS`，`config/strategy.json` 已无顶层通用 `candidate_weights` |
| 全体有序储备、失效补位、物理并集去重 | 已实现 | `ScoredCandidatePlan.reserves` 保存完整合格顺序，`refresh_candidate_reserves()` 沿同板后继补位并用 `attempted` 去重；`CandidatePlanSet.physical_union()` 对三策略窗口求稳定并集 |
| 本地历史/模型资格且无同步下载 | 已实现 | 候选规划只读取 `FeatureSnapshot.history_days` 和模型输入纯检查；行情适配器不在候选或评分同步链下载历史 |
| 有类型资格与公开漏斗 | 等价实现 | `ScoredStockEvaluation`、`ScoredCandidatePlan`、`ScoredCandidateStageCounts`、`SupplyFunnel` 为不可变类型；策略由计划外壳拥有，板块由有类型 `FeatureSnapshot.quote.board` 拥有，公开字段由 `bootstrap_status.py` 白名单投影 |
| 页面解释相对 100 分与合法空仓 | 已实现 | Web 已区分最高相对信号分与成本/风险/动作门导致的空仓，不把候选恢复写成必有推荐 |
| 全部生产权重只由配置拥有 | 未实现 | 板块候选/本地、DeepSeek 维度和融合外层权重会从配置注入；但 `scoring.py` 仍硬编码候选及本地组件内部系数，`strategy_validation.py` 又复制板块、维度和融合数值，配置尚非唯一数值所有者 |
| 指定的完整回归与当前运行验收 | 未闭合 | 119 项候选/配置/状态定向测试通过，但现有用例用小上限等价覆盖补位，尚缺精确第 121 名、完整拒绝类型、deadline/最近有效整批和 1080 最坏物理路径组合回归；本次 `full` 诊断因服务未启动不能完成六检查点 |
| 当前性能门 | 未通过 | `full` 诊断和独立 `make performance-check` 连续复跑均仅报 `board_preselection:absolute_budget`；两次 P95 分别为 302.648ms、289.049ms，固定负载仍为 5500/360，不能充当 1080 最坏候选报价证明 |

本次没有发现资格先后、策略隔离、储备补位或通用 25% 粗分重新回流活动链。当前需要继续的是验收复核与
权重所有权迁移，不是再次实现一套候选规划器。

### 批次二：候选容量与排序历史验证

状态：基础骨架部分实现，指定研究未实现且被真实点时证据阻塞。

`CandidateRecallLedger` 已实现 Top10/20/50、逐板 recall、首次拒绝边界、不可交易/严重亏损率和累计延迟
P50/P95；有限参数族、开发/确认隔离、bootstrap、Holm 及失败关闭对象也已存在。但活动代码没有统一预注册
每板 60/120/180/240/完整人口容量族，也没有把控制组、完整度消融、流动性消融、模型前移和多通道召回作为
这一个研究的五个固定方案；NDCG@20/50、规模/流动性分组、完整性能/内存/deadline 报告也未在同一终态对象
闭合。更关键的是，历史行业缺查询时间、11:20/14:50 分钟锚点及资格/风险事实点时证据仍不足，所以没有真实
开发/确认结果、唯一候选或“120 足够”的结论。当前空仓与候选漏失的因果关系仍未由历史 oracle 证明。

### 批次三：一次性终端留出与 Shadow

状态：可复用部件存在，批次未执行。

领域层已有一次性留出状态、至少 200 日边界和冲突拒绝，基础设施已有留出工件 codec，研究层也有若干
shadow 模型/校准对象；所有当前报告都保持 `terminal_holdout_opened=false`、`production_authority=false`。
没有通过批次二的唯一候选，没有真实终端留出结果，也没有评分文档要求的统一 `ShadowMonitor`、生产只读
对照运行或 Shadow 准入结论。代码中出现 `shadow` 名称不能视为本批已完成。

### 批次四：人工生产启用

状态：未实现、未授权。

`config/strategy.json` 的默认 Tomorrow 评分档位仍为 `v1`；没有通过终端留出和 Shadow 的候选，也没有当前
交付批次的用户明确生产授权。活动代码没有自动晋级该候选策略，V1/V2/V3、固定 68/32、DeepSeek 168、
11:20/14:50 和 first-wins 均未因这份计划改变。

## 已实现的生产链

| 评分逻辑章节 | 状态 | 活动实现证据 | 边界说明 |
| --- | --- | --- | --- |
| 第 2 节端到端链路 | 已实现 | `application/market_data/input_runtime.py`、`application/recommendation/scored_selection.py`、`scored_deepseek_fusion.py`、`scored_freezing.py`、`application/decisions/decision_core.py` | HTTP 查询只消费已发布决策，不现场抓取或评分 |
| 第 3.1–3.4 节实时输入、统一行情和降级 | 生产机制已实现 | `domain/market`、`infra/market_data/normalization`、`infra/market_data/service`、`application/market_data` | 真实历史行业、分钟锚点及资格/风险点时事实覆盖仍不足，见后文 |
| 第 4 节两级硬过滤 | 已实现 | `domain/market/eligibility.py`、`domain/recommendation/filtering/filters.py`、`application/recommendation/candidate_planning.py` | 未核验事实按观察或未就绪处理，不假定安全 |
| 第 5 节总体、评分资格和候选预选 | 已实现 | `domain/market/epochs.py`、`domain/recommendation/selection/scored_selection.py`、`application/recommendation/candidate_planning.py` | 资格检查先于每板 120 只上限，并支持同板储备补位 |
| 第 6.1、6.2、6.4 节规则评分 | 已实现 | `domain/recommendation/scoring`、`domain/recommendation/strategies/composition.py`、`domain/recommendation/risk_fusion/downside.py` | Today/D25 使用板块规则评分；D25 两类入场形态现已独立判定 |
| 第 6.3 节 Tomorrow V1/V2 | 已实现 | `infra/scoring/profiles/v1`、`infra/scoring/profiles/v2`、`application/recommendation/model_scoring_router.py` | V3 loader/trainer 有实现，但缺合格当前工件与完整运行验收 |
| 第 7 节本地风险、下行保护、DeepSeek、预算 | 已实现 | `domain/review/rules.py`、`domain/recommendation/risk_fusion`、`infra/deepseek` | 固定 168 次物理请求上限、原子预算桶和失败保留 local 均有契约 |
| 第 8 节融合、动作、排名和 TopK | 已实现 | `domain/recommendation/risk_fusion/fusion.py`、`scored_fusion.py` | 固定 68/32、风险只扣一次、稳定排序及两池集中度均由领域对象校验 |
| 第 9 节冻结与恢复 | 已实现 | `application/recommendation/scored_freezing.py`、`today_freezing.py`、`infra/persistence/decision_records.py` | Today 错过不追补；Tomorrow/D25 支持受限 `close_fallback`；正式记录 first-wins |
| 第 10 节只读展示 | 已实现 | `application/decisions/decision_queries.py`、`web/api/decision_serializers.py`、`web/static/dashboard.js`、`render.js` | GET/SSE/Web 复用决策身份，展示板块、稳定最高分及中文限制原因 |
| 第 13 节链路外边界 | 已实现 | `application/long_runtime.py`、`application/outcomes/outcome_settlement.py` | Long 不评分不冻结；线上结算不回流训练或自动调参 |

## 本批发现并修复的代码不一致

| 问题 | 原行为 | 修复后行为 | 回归证据 |
| --- | --- | --- | --- |
| D25 入场分支 | D25 入场分支把回踩和突破的专属字段合并为一组必需输入；即使其中一类已经完整确认，另一类缺字段仍返回 `null` | 缩量回踩和放量突破独立判定；任一完整确认直接为 100，仅在无法确认任一分支且结果仍依赖缺失字段时返回 `null` | `tests/unit/domain/test_downside.py` |
| 集中度补位 | 先因集中度跳过高排名股票后继续扫描池外候选，可能补满 6 只，并丢失原始池内位次 | 每个动作池先固定前 N 名和 `selection_rank`，再做集中度；跳过项保留位次和原因，禁止池外补数；入选 `rank` 仍连续 | `tests/unit/domain/test_tomorrow_fusion.py`、决策 codec/API 契约 |
| 最高分同分排序 | `top_scores` 只按最终分和代码排序，遗漏生产规则要求的本地分第二关键字 | 统一按最终分降序、本地分降序、代码升序 | `tests/unit/application/test_decision_queries.py` |
| 板块身份展示 | `DecisionItem` 未固化板块，GET/SSE 和 Web 无法可靠展示评分时使用的板块身份 | 板块写入决策身份、持久化 codec、GET/SSE 投影和页面；集中度及池外原因同步中文化 | `tests/unit/domain/test_decision_identity.py`、`tests/contract/test_web_contract.py`、`tests/js/test_dashboard_state.js` |

另发现一项文档不一致：`docs/03_工程实施.md` 曾称 V3 训练标签仍预扣 20/50/100bp。当前
`infra/scoring/profiles/v3/training.py` 已输出 `label_target=pre_cost_excess_return` 且
`training_cost_bps=0`，对应 codec/测试也已约束；本批已删除该假缺口，保留真实未完成项。

## 尚未实现或尚未闭合

| 对应章节 | 状态 | 真实缺口 | 后续入口 |
| --- | --- | --- | --- |
| 第 5–8 节 | 部分实现 | 候选资格顺序和单一所有权已实现，但当前性能绝对预算、精确 1080 最坏路径及真实服务六检查点未闭合 | `docs/03_工程实施.md` 第 3 节 |
| 第 5–8 节 | 未实现 | 候选/本地组件内部仍有硬编码权重，设置校验器保存重复固定数值，尚未实现所有生产权重由配置单一拥有 | 工程实施第 4 节 |
| 第 3.4、6.3 节 | 部分实现 | 父加增量 active archive 已可发布，但活动日历、股票池和 V3 训练输入尚未完整消费动态 `source_cutoff` | `docs/03_工程实施.md` 第 2 节 |
| 第 6.3 节 | 阻塞 | 尚无由 active archive 重建并通过当前 loader 的 V3 三件工件；训练仍全量驻留 `_Sample`，`training-input.json`、`report.json`、`model.json` 尚不能整组原子切换 | 工程实施第 5–6 节 |
| 第 3、11、12 节 | 外部证据阻塞 | 缺可证明查询/生效时间的历史行业、11:20/14:50 分钟锚点，以及完整资格、硬过滤和风险事实；因此真实点时数据集、收益结论和终端留出保持关闭 | 工程实施第 7 节 |
| 第 11–12 节 | 部分实现 | `CandidateRecallLedger` 和有限参数族已实现，但指定容量族、五方案、NDCG/分组/性能完整报告及真实确认结果不存在 | 工程实施第 8 节 |
| 第 11–12 节 | 部分实现 | `FeatureSpecCatalog`、`CanonicalOutcomeEvaluator`、风险/成本/不确定性及多类 shadow 研究对象已经实现；评分逻辑点名的统一 `ShadowMonitor` 尚未实现 | 工程实施第 9 节；不得用 fixture 代替 |
| 第 12 节 | 阻塞 | 终端留出尚未打开，Shadow 收益/尾部风险门未执行，人工生产启用也未获授权 | 工程实施第 9–10 节 |
| 第 6、12 节可选路线 | 未授权 | Today 与 D25 仍按已授权规则头运行；各自模型头没有独立点时数据、训练、确认、终端留出和授权 | 工程实施第 11 节 |
| 第 10、11 节 | 明确不建模 | V1/V2 没有逐股严重亏损概率，状态公开 `loss_probability_status=not_modeled`，页面不得用总体统计伪造 | 若未来建模，须经过第 12 节完整门禁 |

因此，后续不能笼统表述为“继续补整条评分链”。应按工程实施中的依赖顺序先完成动态归档消费，再重建 V3
工件和工程验收；真实点时来源、终端留出、Shadow 与人工授权仍是独立后置门，不能由代码测试自动替代。
