# Changelog

All notable changes to this project are documented here.

## Unreleased

### Fixed

- 针对用户反馈“15:00 后当天没有荐股、明日/2-5 日仍显示板块可靠度降级”补齐收盘恢复边界：
  收盘补算使用收盘时刻重新校验报价年龄，三板评分改用全市场样本后再过滤候选；组件可靠度按已知输入比例计算，
  盘后冷启动不把天然缺失的尾盘分钟字段作为永久阻断，入场质量不再因可选行业宽度缺失而整体置空。
  收盘补算只读取候选缓存，不再同步抓取 AkShare 研究；板块人口或可靠度阻断时拒绝冻结并保留重试诊断。
  新增延迟报价、历史样本、全市场板块、缓存候选、可靠度和冻结回归测试。

### Verification

- 通过：`tests/unit/domain/test_board_scoring.py`、`tests/unit/domain/test_downside.py`、
  `tests/unit/application/test_board_scoring.py`、`tests/unit/application/test_recommendations.py`
  及收盘恢复集成测试；Ruff 检查通过。
- 实机复核仍发现未解决项：2026-07-23 冷启动收盘补算最高候选仍低于 `0.85`，
  因此按契约拒绝冻结，Web 当前三策略继续 `not_ready`；当天三条错误快照已按用户授权备份并清除，
  历史日期查询已恢复。剩余根因需后续针对真实行情字段覆盖继续处理，不能将本批次宣称为当天荐股已恢复。

### Added

- Web 荐股展示批次：新增独立策略/日期选择状态机和可见策略说明，明确今早、明日、
  2-5 日与长期的持有期、冻结时间和历史能力；显式历史日期跨三种短线策略保持，
  缺失归档时保留所选日期并显示“无数据”，不会隐式切回当前结果。

- 用户反馈此前保存的推荐历史全部无法查询。新增 P6 按策略独立驻留与冷读能力：最近
  20 个交易日内，同日即使只保存 today、tomorrow 或 d25 中的部分策略，已有 committed
  快照仍可通过对应日期列表和历史 API 读取，不再依赖三策略日期交集。

- 用户反馈 Web 同时提供“临时实时”和“正式当前”两个并列按钮，难以判断日常应该选择
  哪一个。新增只读 `view=current` 自动当前视图：同日 P6 未冻结时解析为实时草稿，
  正式冻结后自动解析为正式结果，`close_fallback`、long、历史和未就绪状态分别显示
  “收盘补算”“当前快照”“历史冻结”和“未就绪”；原 `official|live` API 保留供兼容
  调用与诊断。

- 用户反馈 Web 长期只有三板样本不足、可靠度不足、DeepSeek 不完整和 tomorrow 尾盘
  不完整，实时荐股为空。新增 afternoon/final-review/final-quote 评分前的有界尾盘分钟
  刷新，使周期评分不再只读一个永远为空的缓存；新增独立 v17 qfq 历史热缓存，使优化
  或服务重启不再把已预热的 360 只三板历史恢复为仅 40 只旧种子的冷状态。

- 用户确认只执行 `docs/times.md`、暂不执行 `docs/strage.md`；本批完成 T1 真实延迟瀑布
  与性能门禁。新增组合根共享的有界 `LatencyWaterfall`、来源 lane 排队与物理请求/
  本地处理分离计时、状态 API 聚合诊断，以及读取统一预算的 Firefox/geckodriver
  patch-to-paint 与三档桌面验收 runner；不改变候选、评分、风险、融合或排名策略。

- 用户要求把 `plan_c.md`、`plan_sudu.md` 和 `plan_youhua.md` 中仍有效的策略归并到两份
  权威文档后删除，并补充确认已被其他文档取代的 `plan.md` 可直接删除。软件权威契约新增
  provider 三段式、列式类型/dtype 边界、dirty 路由矩阵、P1-P6/DeepSeek/SSE 观测指标和
  长期公共接缝说明；策略权威契约新增 V4 事实映射、证据质量收缩、复核优先级、
  批处理/整批一次修复和 58/66/71 软目标说明。

- 用户要求继续未完成的任务 A，本批闭合 `docs/plan.md` 第 2.6 节。新增按只读能力拆分的
  Web 状态、推荐、事件/SSE、请求解析和服务契约模块，并为 DeepSeek cache identity
  增加独立类型契约，使路由与外部调用参数可由 mypy 静态核对。

- 用户要求把 SDK/API 取股、行情统一、候选/评分、P6/SSE 和 Web 实时展示的性能优化计划
  写入 `docs/times.md`。新增非权威执行计划，归档真实链路审查证据、P0/P1 瓶颈、T1-T5
  独立实施顺序、拟议接口影响、确定性/冻结/资源边界和量化验收矩阵。

- 用户要求将硬过滤、评分、融合、荐股和 Web 展示的全链路收益优化审查方案写入
  `docs/strage.md`。新增非权威执行计划，记录数据覆盖基线、v17/v18 同期影子、风险分层、
  热度组合、流动性对照、分支独立入场形态、候选召回、缺失值收缩、融合归因、TopK、
  Web 决策轨迹及收益晋级门禁。

- 用户继续未完成的任务 A；本批按下一完整章节发布 G5 最终共同门禁，新增
  `docs/reports/youhua-g5-final-gate.md` 和对应失败优先交付契约。报告逐项确认 B5/C5/D5
  均签字通过、A4/A5 仍满足全部门禁、文档/代码/测试/配置一致，并明确本批完成后
  `docs/plan_youhua.md` 全部闭合，不进入其他计划章节。

- 用户继续未完成的任务 A；本批完成 A5.1-A5.5 最终交付审查，新增
  `docs/reports/youhua-a5-final-review.md`，收齐 B5/C5/D5 的
  `PASS / ready_for_gate=yes` 签字并归档完整 diff 审查、提交映射和剩余外部风险。权威设计
  补记 384 MiB 迁移硬上限、`387,186,688` 字节实测峰值、峰值并存场景、纯 columnar
  `254,447,616` 字节结束 RSS，以及未来是否收紧上限必须另立决策；本批不发布 G5。

- 用户继续未完成的任务 D；本批完成 D5.1-D5.2 最终差异审查，并在
  `docs/reports/youhua-d1-p6-web.md` 第 12 节向 A 提交 `PASS / ready_for_gate=yes`
  签字。审查覆盖 P6 current/resident/cold、P6-first 公共接线、持久化分流、SSE、DOM
  四元身份和 ETag resync；新增主动 resync schema 与游标分类竞态回归，不进入 G5。

- 用户继续未完成的任务 A；本批按下一完整章节复核 A4/B4/C4/D4 的阶段 4 交接证据，
  确认 D4 留给 A 的 P6 接纳原子性事项已由 A4-F04 关闭，并新增
  `docs/reports/youhua-g4-gate-review.md` 发布 G4。用户可观察行为不变：不修改推荐、
  冻结、API 或页面逻辑，不进入 A5。

- 用户继续未完成的任务 B；本批完成 B5.1-B5.2 终审签字，新增 B5 行情、三板评分和
  P1-P6 集成内存复验证据及 `report_to_a.md`。终审范围覆盖 P1-P3 provider/列式批次、
  scalar 等价合并、dirty 扩张、A-owned 公共 envelope 适配、性能、内存和 A4-F01 降级闭环，
  结论为 `PASS / ready_for_gate=yes`，不代替 A 执行 G4/G5、squash 或发布。

- 继续 Codex B4：新增完整行情 canonical 行的严格 Polars 列式标准化、Eastmoney/Sina 窄路径合并与
  5500 行/360 候选/100 tick 固定验收 fixture；保留 partial、reference、Tencent overlay 和
  degraded 输入的 scalar 回退。

- 用户要求继续未完成的任务 D；现状确认 G3 已发布而 D4 尚未执行。新增 D4 P6/SSE/API
  固定 18 行性能回归、可执行的 Node 浏览器状态机契约、离线真实页面/SSE 桌面夹具，并在
  `docs/reports/youhua-d1-p6-web.md` 第 11 节形成 D4.1-D4.4 标准交接包；本批不提前执行 D5。

- 用户要求继续未完成的 Codex A 任务；A 完成 A4.1-A4.6 全量验收与问题闭环。新增
  `docs/reports/youhua-a4-acceptance.md`、固定 v17 `perf-check` manifest 和同进程 A4 内存 runner，
  汇总 B4/C4/D4 handoff、关闭 Polars scalar fallback 与 P6 发布原子性两项失败，并覆盖六个
  P1-P6 字节池近上限、双 epoch/双路径、DeepSeek 最大批次、P6 冷读和慢客户端并存场景；
  A4 标记 `ready_for_gate=yes`，本批不发布 G4、不进入 A5。

- 用户再次发送“继续”后，A 按阶段 3 共同门禁复核当前 B3/C3/D3 交接状态。新增
  `docs/reports/youhua-g3-gate-review.md`，记录 A3 handoff 已发布且 B3/C3/D3 标准
  `ready_for_gate=yes` 报告均已到达；A 因此发布 G3，但不启动 A4、不创建 PR/tag/release。
  同批纳入 B3 fixture、C3 raw facts cache identity 修复与测试、D3 P6/Web 差量 patch 修复与报告。

- 用户再次发送“继续”后，A 按 `docs/plan_youhua.md` 执行 A3.1-A3.7。新增
  `docs/reports/youhua-a3-integration.md`，记录 B2 列式 P1-P3/change set、C2 DeepSeek V4
  facts/预算/long 隔离、D2 P6/SSE/Web 增量补丁已按 B -> C -> D 纳入 A 集成工作树；同时
  明确本批只发布 A3 集成 handoff，G3 仍等待 B3/C3/D3 基于本集成提交完成专业复验。

- 用户再次发送“继续”后，A 复核 B2 最新交接报告，确认 B2 已补齐
  `ready_for_gate=yes`、A2 public envelope 适配、component/type-check/性能证据；A 因此发布
  G2。`docs/reports/youhua-g2-gate-review.md` 新增 2026-07-23 再复核与 G2 发布记录，
  明确 A2/B2/C2/D2 均已具备阶段 2 门禁证据，但本批不启动 A3。

- 用户再次发送“继续”后，A 复核阶段 2 门禁新状态：C2 报告已补齐标准
  `ready_for_gate: yes` 字段，但 B2 仍为 `ready_for_gate=no`，因此仍不发布 G2、不进入 A3。
  `docs/reports/youhua-g2-gate-review.md` 增加 2026-07-23 复核记录，更新 C2 状态和当前唯一
  阻塞项。

- 用户发送“继续”后，A 按阶段 2 共同门禁复核当前 B2/C2/D2 交接材料，但因 B2 自报
  `ready_for_gate=no` 且 C2 未使用标准 `ready_for_gate` 字段，未发布 G2、不进入 A3。新增
  `docs/reports/youhua-g2-gate-review.md`，记录三方报告路径、base、gate 状态、阻塞原因和
  后续等待条件；同时纳入 B2/C2 报告与 B2 性能 fixture，保留 D2 追加报告。

- 用户发送“继续”后，A 按 `docs/plan_youhua.md` 执行 A2.1-A2.5。新增
  `src/trader/application/ports/youhua.py`，集中提供
  `youhua_contract_base_v1` 下的 P3/P4 `MarketChangeSet`、P4/P5 高价值复核 manifest、
  DeepSeek V4 facts、P6 projection/overlay event、resync reason 和 248/384 MiB 内存契约；
  新增 `src/trader/application/youhua_test_doubles.py`，为 B/C/D 单域开发提供只记录身份和
  计数的 P4 consumer、review input、projection/overlay producer 替身；新增
  `docs/reports/youhua-a2-public-skeleton.md` 作为 A2 交接报告。

- 用户发送“继续”后，A 复核当前工作树中新到达的 B1/C1/D1 阶段 1 报告，并发布 G1。新增
  `docs/reports/youhua-g1-contract-base.md`，固定
  `CONTRACT_BASE=45bd2fab992d36eb873b7c448fbd9739f0cad43c`、三方 `ready_for_gate=yes`
  状态、唯一 owner/schema 清单、接口申请归并结果和 A2/B2/C2/D2/G2 合并顺序；同时纳入
  B1/C1/D1 交接报告文件，便于阶段 2 从同一公共契约基线开始。

- 用户指定本任务为 Codex A，并要求严格按 `docs/plan_youhua.md` 先完成 A1.x，等待 B1/C1/D1
  报告后再发布 `CONTRACT_BASE` 和 G1。新增 A1 基线报告
  `docs/reports/youhua-a1-baseline.md`，记录当前 `HEAD/upstream`、owner 范围、质量/
  测试/package/性能/Web 三档基线、已知既有失败、B/C/D 报告等待状态和 G1 未发布状态。
  新增契约测试固定 youhua 双层内存口径、公共接缝版本、owner 归属和 G1 等待条件。

- 用户继续执行全工程重构计划 2.5 整节。新增 cache schema v6 的 P1-P6 六池、Polars
  `ColumnarQuoteBatch` 与 `MarketChangeSet`、内存式 `PublishedSnapshotIndex`、20 个完整
  三策略交易日驻留、按日期 single-flight 的三策略冷读、11:19:50/14:49:50 冻结检查点、
  schema-v2 推荐/overlay SSE patch、源目录只读的 `migrate-v17` 和固定无网络 `perf-check`。

- 用户继续执行全工程重构计划 2.4。新增行情源协调器、DeepSeek 复核上下文/请求执行器/
  状态跟踪器、原子预算批次仓库和预算报告器等有类型组合组件；新增统一的适配器失败码，
  覆盖超时、截止、熔断、负缓存、取消、被更新任务取代、无数据、限流、schema 和源失败。

- 用户要求把 SDK/API 采集、结构化和 Web 实时展示的优化方案写入独立文档。新增非权威
  `docs/plan_sudu.md`，将已选定的 Polars 列式方案落实为 provider 三段式适配、P1-P3
  列式批次、dirty code/board/industry 增量重算、P6 热投影、SSE v2 差量补丁、浏览器
  局部 DOM 更新、完整阶段观测、scalar 等价回退和量化激活门禁，并记录 NautilusTrader、
  vn.py、OpenBB、Qlib、Arrow/Polars 及本地开源库的吸收边界。

- 用户继续执行全工程重构计划 2.3。新增按行情、报价、研究、参考数据、快照、事件、复核与
  结果拆分的应用端口，以及不可变的流水线依赖、选项和资源集合。

- 用户要求把 DeepSeek 各类物理请求“什么时候使用”写清楚。`docs/plan_c.md` 新增共享预热、
  today、tomorrow、d25、Pro 和 emergency 的使用时段、准入条件、停止提交时间、跨策略
  归属和物理请求计数规则，并给出主审 58 次、含 Pro 66 次、含 emergency 71 次的正常日
  计划上界。该文档仍是非权威执行计划，不表示活动策略已经切换。

- 用户继续执行全工程重构计划的 2.2 整节，并再次强调源码仍以 800 行为上限而不是机械拆成
  更小文件。新增领域能力拓扑契约，固定 `market`、`recommendation`、`review`、`outcome`
  四个包及旧根级路径零容忍；新增板块横截面、融合、动作/选择、长期研究、风险映射和结果
  结算的有类型请求值对象，复杂调用不再依赖长参数表或动态类型导出。

- 用户诉求：再次审查完整评分荐股策略，并形成让 DeepSeek API 以较低成本发挥更大作用的
  执行计划。现状判断是本地确定性候选、过滤、风险和冻结边界应继续保留，主要浪费来自
  重复五维打分、重复 prompt、整批修复、阶段化缓存和过高正常调用目标。新增非权威
  `docs/plan_c.md`，规划改用“催化与风险事实提取 + 本地确定性映射”、跨策略复用、1-8 股
  自适应批次、逐股修复、Flash/Pro 分层和 70-83 次正常日软目标；仅用正式推荐到期结果
  做在线关联观察，不实施本地回测、shadow 或自动调参。验证要求覆盖文档治理契约及完整
  质量、打包和 wheel 安装门禁。剩余风险：该计划尚未实施，也不能在缺少可靠对照时宣称
  已提高实际荐股收益。

- 用户要求继续实施全工程重构计划，并确认活动源码继续采用 800 行上限而非任意 500 行
  限制。新增六个独立工程重构章节、章节状态和严格 Ruff 债务单调收敛门禁；2.1 当时登记
  `C901=42`、`N818=7`、`PLR0911=16`、`PLR0912=15`、`PLR0913=69`、`PLR0915=14`，
  任一计数变化都必须经 Review 并同步更新，最终目录切换时全部归零。

- 用户诉求：热点本身可以接受，首要目标是避免推荐次日出现大幅回撤。新增 v17 下行保护，
  用 ATR20 日内反转、趋势破位、板内低波动/低回撤尾部和弱市弱收盘四类结构事实，把原本
  可执行的高风险候选降为观察；必要风险输入缺失时关闭可执行入口，单纯热门或高热不触发
  降级。新增缩量回踩/放量突破入场质量，以及冻结推荐 T+1、T+2/T+3/T+5 的收益、20bp
  成本净超额、MAE 和 MAE/ATR 后台审计表。

- 用户诉求：把 SDK/API 取数、标准化、硬过滤、评分和 Web 展示的完整优化分析写入
  `docs/plan.md`。新增非权威执行计划，记录本机固定负载基线、四个开源仓库的可借鉴边界，
  以及按“闭合 v17、历史复权正确性、P1-P6 热路径、候选总体、收益证明”排序的五个批次、
  退出门禁和不承诺收益边界。

- 用户规则：程序持续运行到 15:00 时依赖本次运行已经层层筛选并发布到 P6 的推荐；程序
  重启后先读数据库，数据库缺失才获取同日收盘行情重新得到推荐并写库。新增逐策略收盘
  恢复协调器、P6 选股身份与收盘锚点确定性回放、冷启动三板本地重建，以及
  3/5/10/20/30 秒无重叠退避重试。

- 用户问题：点击推荐股票后的详情抽屉平铺大量空值、计算中间量和技术审计字段，核心结论
  难以识别；同时推荐接口仍为每只股票传输这些页面不再消费的数据。新增 Web envelope
  schema v3 精确字段白名单、精简风险去重投影，以及“推荐结论 / 核心行情 / 评分与风险”
  三组详情契约；核心行情缺失、快照降级和模型未复核仅在实际发生时显示一条可读状态。

- 用户问题：历史推荐中的“今日涨跌”和“锚点至今”再次全部为空，同时同日已经生成的
  临时推荐没有可见页面。新增显式 `view=live` 同交易日临时草稿只读接口与桌面“临时实时”
  视图；新增 14:50 后冷启动的一次性 P2 当日报价索引恢复任务，不经过候选、评分、
  DeepSeek 或冻结写入。

- 用户诉求：适配器层名称过长，希望统一缩短为 `infra`。新增架构契约，要求
  `src/trader/infra` 必须存在、旧目录必须消失，且活动源码必须统一导入
  `trader.infra` 命名空间。

- 用户诉求：后台行情应在 SDK/API 能稳定返回时尽可能实时刷新，并允许按实际耗时自动调整。
  新增周期全市场必须物理刷新、临时空筛选保留候选、实时事件依赖顺序和生产秒级 cadence
  契约回归；状态计数 `candidate_selection_preserved_degraded` 可审计因历史预热或行情陈旧
  而保留最近候选池的次数。

- 用户诉求：把 `.deepseek_key` 统一改为受保护的 `.token_key`，在同一赋值文件中分别保存 DeepSeek API Key 与 Tushare Token，并在 Tushare 120 积分上限内恢复“今早/明天/2-5 天”从实时采集、历史预热、层层筛选到发布/Web 查询的完整链路。新增双凭据严格解析、120 积分能力矩阵、官方 `daily` 批量日线适配、历史预热覆盖/在途/失败状态，以及旧运行库最近有效前复权日线的只读冷启动种子。

- 用户本轮性能与实时字段回归：新增当前快照/overlay 必须从运行态索引读取且不得触碰持久化仓库、最近历史冻结只预热一次并生成紧凑交付视图、未冻结日期不得负缓存，以及运行态 overlay 随刷新/收盘恢复的契约与集成覆盖。

- 用户补充反馈回归：新增“当前日期只有昨日冻结时必须返回空 not_ready 且无 ETag”、前端不再接受或提示上一交易日 current fallback，以及 P2 特征尚未提交时仍可从已合并规范行情读取历史股票当日价的测试。

- 用户诉求：把 `docs/` 下分散的需求、实施计划、问题单、架构清单和运维资料合并为两份文档。现状审计确认目录内共有 8 份文件、3623 行，活动契约、历史执行记录和未完成 v17 路线相互交叉；新增并相互链接 `software-business-design.md`（产品、架构、运行、API/UI、运维、验收和工程路线的唯一权威）与 `recommendation-strategy.md`（候选、过滤、因子、评分、DeepSeek、融合、动作与 TopK 的唯一权威），契约测试禁止 `docs/` 再出现第三份并行业务文档。

- 用户问题回归：历史推荐表“锚点至今”持续为空，且“今日涨跌”显示冻结锚点涨跌。新增历史 API 对 P2 当前报价索引、未再次入选股票、当日行情缺失和旧日 overlay 隔离的契约测试，并新增行情服务当前报价索引优先采用更新腾讯定向报价的组件测试。

- 用户诉求：继续完成 `docs/hi.md` 中尚未闭合的批次二。现状审计确认上一提交虽已推送 v16 半成品，但计划仍为执行中，核心板块评分/缓存专门测试与性能文件缺失，质量门禁存在 18 个 pytest 失败、23 个 mypy 错误、格式/静态检查失败及 7 个超长活动模块。新增三板评分、缓存、风险、集中度边界测试和固定 360 候选性能 runner/fixture；补齐三 lane 单 worker及队列等待观测、板内同行/领先组边界、缓存 epoch 隔离、七项风险去重与 25 分截断、TopK 60% 和竞争组限制证据。

- 用户诉求：将基于 `58e6d39` 的本地 v15 修改恢复到已前进的远端分支；给出的 460KB tar 只包含 13 个重叠文件，不能代表完整工作树。审计确认旧工作目录仍保留同基点的完整安全 stash（66 个文件），因此以完整 stash 恢复源码、配置、测试、性能 fixture 和文档，并把后续三批远端修复按语义合并：页面快照身份对账继续使用 `dashboard.js?v=8`，结构化研究成功缓存继续复用，腾讯候选/TopK 报价在五来源普通 worker 之外保留独立紧急执行位。为保持活动源码 500 行门禁，来源 latest-wins 生命周期独立到应用层 `source_lanes.py`；未把仅含重叠文件的 tar 当作完整实现覆盖当前树。

- 用户问题：顶部持续显示 `TopK live overlay degraded: data source task exceeded its batch deadline`。现场 `/api/status` 显示腾讯定向报价 917/917 成功、P95 约 700ms、无熔断，而共享数据池 6 个 worker 已全部被全市场、历史或研究任务占用且有排队任务；新增数据池紧急 lane 指标与对应排查说明，用来源延迟和 lane 状态区分真实腾讯超时与内部 FIFO 饥饿。

- 用户问题：页面反复提示 `deepseek_incomplete`、`tomorrow_tail_data_incomplete` 和 `d25_structured_research_incomplete`。运行审计确认 DeepSeek 当日存在 73 次无 HTTP 状态的读取超时且相关阶段额度已消耗，tomorrow 尾盘分钟覆盖随后已恢复为 7/7，D25 结构化研究则因每 3 分钟强制重抓全部候选而在 8 秒批次截止下反复处理固定排序前部代码；新增三类提示的可操作排查与恢复说明。

- 用户问题：今早、明日和 2-5 日页面在服务端已经生成今日实时快照后仍可能停留在昨日数据。现场运行库与只读 API 确认 `2026-07-21` 草稿、冻结和秒级候选报价均正常发布；新增前端状态心跳快照身份对账，仅当服务端当前策略 `snapshot_id` 与页面身份不一致时补拉一次推荐，作为 SSE 推送之外的低流量恢复路径。

- 用户诉求：按 `docs/hi.md` 执行 v15 多源并行采集、统一缓存、结构化合并和三板第一批风险门，同时保持 v14 评分、动作阈值与 TopK 不变。新增东方财富、新浪、腾讯、Tushare、AKShare 五个 latest-wins 来源 lane、不可变 `SourceObservation`、确定性 `CanonicalMarketSnapshot`、应用层缓存身份与唯一有界 LRU/负缓存/single-flight、Tushare 可选 extra 和慢数据适配，以及板块身份、上市日龄、交易规则、逐字段来源、冲突与降级的冻结/API/UI 加法审计字段。新增固定 5500 行全市场和 360 候选性能工具与脱敏 fixture；本批不启用 v16 同行、领先滞后、换手冲击、板内评分或新 TopK 选择器。

- 用户诉求：检查 `docs/need.md` 最近三次提交后，补齐整个活动工程中尚未实现或与契约不一致的功能，并以职责边界拆分超大文件。新增配置化结构风险硬过滤、V4-Flash 主审/V4-Pro 挑战者、固定点时证据路由、挑战者保守合并与策略级缓存、模型/指纹/cache-token 审计、结构化风险冻结重放，以及活动 Python/CSS/JavaScript/HTML 的 500 行架构门禁；新增失败、迟到、预算、schema 修复、黑名单、结构风险和一字涨跌停回归。

- 用户诉求：把 `docs/hi.md` 从方向性方案改为 Codex 可直接执行的详细计划。文档现已固定统一执行协议、两个独立交付批次、允许修改的文件范围、失败先行测试、类型和接口、五个数据源 lane、三个板块评分 lane、精确候选/评分权重、DeepSeek 全局协调、融合与故障降级、逐项验收矩阵、提交信息和停止条件；本批仍不修改活动契约、配置或实现。

- 用户诉求：将多源并行采集、沪深主板/创业板/科创板独立评分、结构化合并、DeepSeek全局协调、TopK集中度和分批交付门禁形成完整计划文档。新增 `docs/hi.md`，明确 v15 数据合并批次与 v16 三板评分批次、来源职责、板内候选/评分权重、合并 epoch、故障降级、API/冻结兼容、测试验收和未验证收益风险。本次仅新增计划文档，未改变 `docs/need.md`、运行配置或活动代码。

- 用户问题：`GET /api/status` 先报 `sqlite3.OperationalError: unable to open database file`，随后 Werkzeug 报 `OSError: [Errno 24] Too many open files`。原因已确认：DeepSeek 预算库与共享快照库都把 `sqlite3.Connection` 当作会自动释放资源的上下文管理器使用，但该上下文只提交或回滚事务、不关闭连接；页面轮询状态时会从预算摘要和持久化观测路径遗留数据库文件描述符。修改后两个 SQLite 边界在成功、提前返回、初始化失败和异常退出时都确定关闭；预算运行库暂时不可访问时，DeepSeek 状态返回可解析的 `budget_store_unavailable` 降级结果，`/api/status` 继续只读返回 200，顶部额度显示“不可用”而不是产生 500 或伪造可用余额。
- 用户问题：`TopK live overlay degraded: data source task exceeded its batch deadline` 等最近错误过长时挤压顶部其他状态。原因是最近错误与行情、推送、评分、DeepSeek 和冻结状态共用 `nowrap` flex 行。修改后最近错误成为 Header 独立第二行，标签与正文分列，正文允许对无空格错误码任意断词换行；第一行状态不再被错误长度占用，900px 以下已有窄屏布局顺延错误行但不增加业务分支。验证：静态页面/CSS 契约、`make format-check/lint/type-check/test/package` 全部通过（417 tests），仓库外 wheel 的 CSS v4、独立错误节点、断行规则、CLI 和 `pip check` 通过。剩余风险：1280x720、1440x900、1920x1080 无头截图被宿主 Firefox 的 SWGL framebuffer 映射故障阻断，机器无 Chromium 备选；未发现代码侧已知问题，但本批不能宣称截图门禁通过。
- 用户问题：DeepSeek 五维结果显示 `rejected`。运行审计确认三个策略批次均因 `api_key_missing` 跳过，原因是受保护的 `.deepseek_key` 已存在且权限为 `600`，但 v2 只读取进程环境。修改后配置边界按“`DEEPSEEK_API_KEY` > `DEEPSEEK_API_KEY_FILE` > 项目根目录 `.deepseek_key`”加载密钥，安全解析单行原值或赋值格式并拒绝 POSIX group/other 可读文件；页面按错误类别显示“未配置、禁用、额度、截止、调用失败或结构校验拒绝”，数据库 `rejected` 终态及 `local_degraded` 门保持不变。验证覆盖文件加载、环境优先、不安全权限、零物理调用审计和静态资源契约；`make format-check/lint/type-check/test/package` 全部通过（417 tests），仓库外干净虚拟环境 wheel 导入、资源、CLI 和 `pip check` 通过。剩余风险：外部 DeepSeek HTTP 有效性仍取决于用户密钥、网络和供应商服务，本批未发起消耗额度的真实请求。
- 本批优化（2026-07-20）聚焦评分与荐股策略参数化链路：在 `StrategySettings` 增加 `local_strategy_weights` 配置并下发到 `RecommendationPolicy`，`application/recommendations.py` 的本地评分与评分融合改为透传该权重；`FrozenReplayPolicy` 与快照序列化/反序列化（`infra/persistence/snapshots.py`）支持 `local_strategy_weights`，旧快照缺失字段回退默认组件权重，未改变 68/32 融合公式与风险扣分上限。
- 本轮（2026-07-20）补齐 DeepSeek 审计字段闭环：schema 解析、审核短路/失败路径注入、持久化往返与 API review 节点透出全部落地，并补 `tests/component/test_v2_deepseek.py`、`tests/component/test_v2_persistence.py`、`tests/contract/test_v2_web_api.py` 回归。
- 本批（2026-07-20）补齐 DeepSeek 审计字段闭环，但保持字段只读，不允许 `challenger_status`、`review_stage`、`rating` 或置信度改变动作、融合或 TopK 排序。
- 本次“收益型 shadow”落地：`domain/strategies/shadow.py` 新增 today/tomorrow/d25 的同行收益差/领导对照影子评分路径；`application/recommendations.py` 在快照元数据输出 `shadow_scoring`（覆盖率、`top_shadows`、`rank_gap`）供离线审阅，不改动生产排序门槛与 `68/32` 融合。
- 本批续作（2026-07-20）聚焦“标准化/路由/数据库”风险可回归：新增 `tests/unit/test_v2_market_data_router.py` 覆盖 required/optional 混合降级、无数据优先语义、failed/vendor 汇总；新增 `tests/unit/test_v2_sqlite_migrations.py` 覆盖 schema 初始化、幂等性与旧 schema 升级迁移。
- 本轮“标准化/路由/数据库”继续推进（2026-07-20）：新增标准化输入收口（`MarketQuoteInput` 字段时区/格式校验）并在 `normalize_quotes` 中隔离坏行；路由空结果判定收口到 `_is_empty_payload()`；补充 SQLite 遗留 schema_version（`N/A`）可恢复测试。
- 本轮“标准化/路由/数据库”补齐（2026-07-20）：`normalize.py` 将 `MarketQuoteInput` 进一步限定到 `6` 位数字代码、非空 `source/data_version` 与可用时区，新增非法输入回归，确保坏行情仅在标准化层隔离，不影响后续评分与异常归类。
- 本批（2026-07-20）补充本地评分链路回归：新增 `tests/unit/domain/test_strategies.py` 覆盖 `local_strategy_weights` 覆盖注入后的组件打分变化；新增 `tests/unit/application/test_recommendations.py` 覆盖快照重放策略字段与今日推荐排序是否同步 `RecommendationEngine` 的权重注入。
- 落地 `docs/issues/2026-07-20.md` 第三档 P12 证据落盘统一：`AkshareResearchClient` 的研究源（news/financial/announcement/pledge/unlock）原始载荷统一持久化到 `runtime/evidence_cache/<source>/<code>.json`；`MarketFeatureService` 的 `research` 读取路径加入持久化重放与过期清理，在重启场景可在 TTL 内优先命中 cache 并回放 `ResearchObservation`。
- 本轮“标准化/路由/数据库”继续子项（2026-07-20）路由可观测性细化：`gateway.py` 的 `health()["route"]` 新增 `attempted_count/success_count/failure_count/no_data_count/skipped_count` 并保留 vendor 序列轨迹；`router.py` 将 `circuit_open` 映射为 `status="skipped"`，用于识别熔断跳过与失败差异。
- 本轮（2026-07-20）进一步补齐路由语义：`router.py` 将 optional 供应商的 `no_data` 标记为 `skipped` 而非 `failed`，`gateway.py` 的候选报价腾讯源在熔断窗口内也计入 `route` 健康 `skipped_count`，区分可恢复降级与真实失败。
- 本轮续作（2026-07-20）补齐数据库观测闭环：`infra/persistence/sqlite.py` 将 `SCHEMA_VERSION` 提升至 4 并新增 `data_source_health` 持久化字段 `route_json/route_status/route_fallback_reason/route_degraded`；`infra/persistence/writer.py` 持久化 `market_data.health()["route"]` 结构，形成从路由结果到 SQLite 行记录的可回放链路。
- 用户诉求：标准化收敛。新增 `infra/market_data/normalize.py`，提供 `to_float`、`normalize_quotes`、`MarketQuoteInput` 与 `build_market_quote`；`eastmoney.py`、`sina.py`、`tencent.py` 统一调用该入口组装 `MarketQuote`，非有限值与异常字段统一降噪，行为口径与字段名保持不变。
- 用户诉求：标准化契约闭环。`infra/market_data/features.py` 新增 `FEATURE_SCHEMA` 及版本常量，并在配置层校验 `factor_contract.feature_schema_version` 与注册表一致，`tests/unit/test_v2_settings.py` 增补 `feature_schema_version` 错配拦截与可选 `feature_names/feature_schema_expected` 一致性验证回归。
- 本批数据库健壮性补齐：`tests/unit/test_v2_sqlite_migrations.py` 新增空白 `schema_meta` 值回归（覆盖 `schema_version=''` 与 `N/A` 场景），验证初始化可自动回写 `schema_version=SCHEMA_VERSION`。
- 用户诉求：完成 `docs/issues/2026-07-20.md` 第四档 Web 序列化收敛（P15）。新增 `web/serializers.py`，将路由错误、历史日期列表和事件列表响应统一为独立序列化入口；`web/routes.py` 保持仅作参数校验与状态分支。接口字段与错误码不变。
- 用户诉求：继续推进 `docs/issues/2026-07-20.md` 剩余未完成项目（P6/P10/P11/P12/P13）。1) `bootstrap.py` 保持唯一组合根并通过小型工厂注入具体适配器。2) 新增 `FeatureSchema` dataclass + `RAW_FEATURE_SCHEMA`/`DERIVED_FEATURE_SCHEMA` 注册表。3) `BoundedExecutor` 继续作为有界执行内核。4) evidence 与 observation 缓存分命名空间并共用唯一持久化 worker。
- 用户诉求：按 `docs/issues/2026-07-20.md` 第二至四档继续落地优化（P5-P16）。新增市场数据路由、required/optional 过滤审计、受控 `Rating` 审计枚举和 SQLite 迁移注册表；`Rating` 不映射生产动作，避免模型自由文本绕过本地风险规则。
- 用户诉求：对照 `X:\github\TradingAgents` 开源库对全链路流水线做系统审查并落地第一批优化（P1-P4）。现状是 DeepSeek 客户端为单一具体实现，不支持测试注入 mock，也不处理 `deepseek-reasoner`/`deepseek-v4-pro` 思考模型的 `reasoning_content` round-trip；DeepSeek prompt 中没有独立的权威数字快照，模型可能对本地已计算字段产生幻觉。修改后：1) 新增 `DeepSeekClientBase` ABC + `DeepSeekHttpClient` 实现，`DeepSeekReviewer` 接受抽象接口使测试可注入 mock；2) 新增 `ModelCapabilities` 声明表，按模型自动控制 `temperature`、`reasoning_effort`、`reasoning_content` round-trip 和 `response_format`，解决 reasoner 模型 400 错误；3) 新增 `model_catalog.py` 已知模型白名单 + 非阻塞警告，未知/即将停用模型启动时提示；4) 新增 `GroundTruthRenderer` 把 `FeatureSnapshot` 渲染为确定性数字快照供 DeepSeek prompt 使用，降低幻觉边界。新增 `tests/unit/test_v2_deepseek_base.py` 覆盖 ABC、capabilities、catalog、round-trip 和 ground truth 渲染。
- 用户诉求：把 `docs/back1.md` 中更可能改善收益质量的策略合并到唯一生产契约 `docs/need.md`。现状是 back1 同时混有三板机制、候选权重、离线晋级、机器学习和低星项目引用，不能整体视为已验证生产方案。修改后新增第 26 节 `strategy_v10_board_aware_draft`，固定三板身份、板内总体、同行收益差、领先滞后、换手/成交冲击、执行质量和集中度机制，并将六组精确权重、P50/P80 与 0.85 明确登记为不影响当前生产的 `candidate_initial`。
- 用户诉求：补充当前高关注度的量化和 DeepSeek 荐股开源参考，同时删除低于 20K Star 的链接。修改后新增 OpenBB、NautilusTrader、FinGPT 和 LEAN 四个达到门槛且与金融数据、量化事件模型或金融 LLM 直接相关的 canonical 仓库，并明确只有 TradingAgents、daily_stock_analysis 和 TradingAgents-CN 属于 DeepSeek 接入参考，避免把一般金融 AI 项目误称为 DeepSeek 荐股库。
- 用户诉求：结合网络上一手资料优化 `docs/need.md` 中 DeepSeek API 荐股方案，但暂不处理“离线验证与晋级”。现状是需求仍以即将停用的 `deepseek-chat` 单模型五维复核为中心，16 条证据只有总上限，没有固定类别、反证和点时路由，模型自报置信度也容易被误解为真实概率。修改后固化 V4-Flash 非思考主审、V4-Pro 思考挑战者、16 条点时证据配额、稳定 prompt 前缀/上下文缓存审计、三态挑战结论和模型身份；校准字段只作可空影子审计，不定义样本量、统计门槛或生产晋级。
- 用户诉求：把本库策略所依赖或借鉴的 GitHub 高星项目链接写入 `docs/need.md`，确认使用 DeepSeek API 进行股票推荐的开源库，并恢复 `strategy_and_prediction.md` 历史中的方法来源。现状是第 2 节只有六个无链接名称，末尾另有不可渲染的重复终端表格，未区分实际运行依赖、机制参考和 DeepSeek 荐股类项目。修改后统一记录 canonical 仓库、可复核的 2026-07-19 Star 快照与借鉴边界；12 个历史策略参考可追溯到首次提交 `841355c`，并补入 DeepSeek/A 股项目和实际依赖 AKShare，后续按当前 Star 门槛筛选展示。
- 用户诉求：连续完成第 19-25 节代码，最后统一补测试并 Review。现状是当前/历史响应身份不足、跨日冻结缓存表达含混，状态缺少可重启查询的来源/DeepSeek/冻结审计。修改后推荐 envelope 新增请求日期、当前交易日、历史/fallback 身份，历史行可叠加独立当前行情；新增持久化来源健康、逐物理 DeepSeek 调用和冻结证据汇总，以及本地 Lucide 图标资源。第 24 节已有固定输入完整日证据，需求文档没有第 26 节，均未重复或虚构实现。
- 用户诉求：先统一完成第 14-16 节 DeepSeek 代码，再补测试和 Review。现状是候选会被定性证据门提前清空，批次/候选状态混用，只有六桶总额而没有阶段目标与上限。修改后新增持久化批次和逐股终态、十阶段 133 次目标/188 次上限、受条件约束的 emergency、原始/策略两级缓存、优先复核及重启 `abandoned` 恢复；新闻或公告不再作为调用资格。
- 新增架构契约测试，固定快照编排模块必须使用职责明确的 `snapshot_workflow.py`，并禁止旧 `snapshot_lifecycle.py` 路径重新出现。
- 用户诉求：继续闭合 `docs/issues/2026-07-17.md` 中第 4-7 节未完成任务；现状是配置中的 worker 和刷新频率未形成真实消费者，关键单点可能因调度延迟错过，TopK 无独立报价链，数据年龄、乱序、单飞、熔断和发布延迟缺少统一证据。修改后新增生命周期受控的有界执行器和独立 cadence 计划器，真实启动数据、标准化、三策略、DeepSeek、合并、持久化及 long 消费者，并形成全日计划、来源恢复、实时 overlay 和时效状态回归。
- 新增第 13 节 d25/long 点时研究输入：d25 候选和固定 long 名单按代码获取东方财富财务、精确发布时间公告、累计质押比例与未来 90 天解禁比例；纯领域公式生成价值、成长、质量、行业/政策、风险保护及四类本地风险，来源时间、接收时间、版本、脱敏原始摘要和派生摘要随输入保存。
- 新增第 12 节 tomorrow 候选级未复权 1 分钟输入：按连续 30 个交易分钟计算原始收益和尾盘量比，再通过配置化固定公式映射到 0-100；分钟来源、源时间、接收时间、输入版本和公式中间值随证据及冻结回放保存。
- 用户问题：需要比较 `docs/archive/v1` 与当前 `docs/need.md` 的交易、过滤和评分策略，并判断还能如何优化；现状判断：v1 归档同时包含生产规范、运行配置和不具生产约束力的研究计划，不能拼成一套口径。修改说明：以 v1 自称唯一规范的 `strategy_and_prediction.md` 及其 `config/runtime.json` 为旧生产基线，以 `plan.md` 仅补充研究证据，完成策略角色/时间线、候选与硬过滤、本地/DeepSeek 评分、TopK、退出验证和长期池的可追踪差异归纳，并形成不越过当前只读研究看板边界的分阶段优化建议。
- 新增第 11 节 today 点时新闻信号：候选新闻按配置化正负关键词多数规则生成 75/50/25 极性均值，并按最新有效证据年龄生成 1 小时满分、72 小时线性归零的新鲜度；纯领域计算、配置校验、候选特征、冻结输入和离线回放形成闭环。
- 新增第 10 节配置驱动本地风险表：逐项登记适用策略、触发因子/运算符/精确阈值、严重度、扣分、置信度、证据有效期、互斥/叠加组和稳定事实 ID 字段；风险明细增加实际值与阈值并随冻结、API 和桌面明细返回。
- 新增逐股 `filter_details` 审计记录，保存股票代码、`filter_code`、阈值、实际 JSON 标量、来源和时间，并随冻结 JSON 往返及通过只读 API 返回。
- 新增第 8 节完整因子登记表和严格 schema：52 个生产因子逐项声明策略、输入、公式、单位、方向、时点、复权、窗口、样本、截尾、归一化、缺失策略、范围与版本；横截面统计随冻结输入保存并由 API 返回。
- 新增与冻结快照身份绑定的可恢复 `live_overlay`：冻结后只刷新 TopK 当前报价，独立持久化版本、点时时间和收盘标志，通过 SSE/ETag 通知而不改写冻结 JSON；15:00 后首份有效收盘值禁止被迟到结果覆盖。
- 新增 `trader-cli threshold-report` 冻结输入预注册报告，按策略输出完整回放候选的分数分布、推荐数、空推荐比例、相邻 TopK Jaccard 变化、DeepSeek 覆盖、整版本地降级比例和风险拦截率，并拒绝混合策略/融合版本。
- 用户问题：今日多项 Bug 缺少集中状态记录；修改说明：新增 `docs/issues/2026-07-17.md`，逐项归纳行情降级、Tab 加载、空值展示、缺失原因、AKShare JSONP、DeepSeek 进程注入和 P2 数据缺口，并区分已修复、待运行生效、待验证和待实现。
- 推荐 API 为 `missing_fields` 同步提供可展示的 `missing_reasons`，明细抽屉直接说明缺失数据的上游原因。
- 候选池接入带 8 秒硬超时的 AKShare 兼容个股新闻证据，新闻证据进入特征快照、DeepSeek 输入和冻结回放；成功结果缓存 10 分钟，失败负缓存 60 秒。
- 新增第 25 节最终验收闭环：新冻结快照保存完整市场预选输入、定向候选与经校验 DeepSeek 结果，`trader-cli verify-freeze` 可离线复算并核对过滤、评分、风险、veto 和排名；`/api/status` 新增活动 TopK 报价 P95 与 DeepSeek 物理调用验收摘要。
- 新增固定行情完整交易日影子门禁，按 09:20-15:00 时间线在两个隔离目录运行真实 SQLite/JSON 冻结链，对照 today/tomorrow/d25 manifest、long 非冻结和全部 JSON SHA-256 确定性。
- 新建单一 `src/trader` 安装包，按 `domain`、`application`、`infra`、`web`、`entrypoints` 和唯一组合根分层。
- 新增 today、tomorrow、d25、long 四策略的确定性评分、硬过滤、风险事实去重、TopK 和动作判定。
- 新增东方财富/新浪/腾讯行情适配、AKShare 研究边界、交易日历、历史特征缓存和多源降级。
- 新增 DeepSeek 五维 schema、证据子集校验、共享代际缓存、逐物理请求原子预算及 188 次六桶上限。
- 新增 SQLite/不可变 JSON staged-committed 冻结协议、哈希校验、隔离恢复、优先事件重放和跨平台单进程锁。
- 新增只读推荐 API、ETag、有界审计查询、SSE 游标恢复/慢客户端隔离及包内桌面工作台资源。
- 新增分层单元、组件、契约和集成测试，以及根级 `AGENTS.md`、迁移清单和 v2 运行手册。
- 用户诉求：为 `docs/hi.md` 增加性能、缓存和实时性优化计划。计划新增独立 v17
  等价硬化批次，并把 v15 行情缓存、v16 评分缓存、固定录制负载、延迟/数据年龄/
  内存预算、背压、状态指标、性能 CLI、回归矩阵和停止条件落实到可执行文件与命令。

### Changed

- 用户要求把“已冻结 · 收盘补算 · 降级…”状态和最近错误移到页面上方并固定高度，随后
  明确摘要行应位于四策略按钮行上方、两行紧邻股票表。页面现将快照状态与最近错误放入
  Header 顶部信息区，两栏均固定为 52px，长文本在各自区域滚动；主体顺序固定为摘要、
  策略/日期、股票表，不再由状态长度改变纵向位置。

- Web 荐股展示批次：移除与主表空态重复的“当前策略尚未发布快照”通知，短线无快照时
  只在表格显示唯一空态并在通知栏提示“等待策略数据更新”；长期策略保留专属当前数据
  说明。摘要栏移除重复的冻结状态、评分版本和内部路由健康，改为最高评分、模型复核
  数和数据状态；行情路由故障仍通过行情来源和最近错误反馈。

- Web 荐股展示批次：用户要求将“实时草稿”改为“实时数据”、保持荐股表格不变、
  不展示观察池并理清四个策略与日期选择。当前日期继续只读 `view=current`，历史日期
  继续使用显式 `date`；短线主表只显示正式推荐，long 在同一主表显示当前固定名单，
  进入或离开 long 均从“当前”开始。长期未就绪文案改为“长期策略当前尚无可用数据”。

- 15:00 后冷启动收盘补算的完整性判断从“存在三板收盘价”收紧为主板、创业板、科创板
  分别至少 100 只具备 20 日有效流动性历史；历史热缓存仍在预热时保持 `not_ready` 并
  按既有 3/5/10/20/30 秒退避重试，不提前冻结三板样本不足的半成品。

- 桌面首页改为单一“当前推荐”状态入口，策略或日期切换仍隔离迟到请求；Web 明确请求
  `view=current`，SSE 可在同一页面从草稿无缝切换为冻结结果，冻结完成后拒绝迟到草稿。
  省略 `view` 的 API 仍保持原 `official` 默认语义，不把 UI 调整扩散为调用方兼容破坏。

- 历史预热 360 个槽改为只在主板、创业板、科创板间稳定轮询，每板最多 120 个；保留
  板内至少 100 个样本和可靠度 0.85 的原风控门槛，不通过降低门槛制造荐股。

- 正式 `perf-check` 从 `infra` 迁到入口层并改为调用活动标准化、融合、列式投影、板内
  评分、全局选择、推荐准备/终态化、P6/SSE 和 Web 路由。性能配置升级为 schema v2，
  固定 5500/360/120/18 行与三策略负载，并将标准化、融合、canonical、targeted commit、
  SSE 和浏览器预算收紧到 `docs/times.md` T1 约定值。

- 三份专项旧计划同时包含已实施方案、阶段施工指令和未落地拟议值，继续保留会形成第二套
  策略真相源。本批以活动代码、`config/v2`、G5 报告和现有测试为准，把已实施内容改写为
  长期契约；A/B/C/D 阶段分工只保留在 `docs/reports/` 的历史证据中。活动行为不变：
  V4 仍使用至少 2 个有效维度和 0.50 覆盖门禁，today 主审/挑战者分别以 11:20/11:18
  截止，long 永久零物理请求，融合仍为 0.68/0.32 且固定向量为 `83.40`。`plan.md`
  的重构章节已全部闭合且相对当前权威文档过时，按用户确认不再重复归并。

- 组合根仍唯一位于 `bootstrap.py`，`ApplicationSystem` 继续显式拥有 worker、source
  lane、pipeline、持久化和 DeepSeek 生命周期；流水线初始化、冻结恢复、事件执行、市场
  缓存、评分准备和审查发布改为分阶段函数与不可变请求对象。Web 由单文件路由改为薄注册
  facade，保持原 API、ETag、SSE 游标、错误码和桌面 payload 等价。

- `docs/times.md` 将后续实时性能工作固定为先建立真实延迟瀑布，再依次处理 SDK 连接与
  有界分页、列式/dirty 提交、本地推荐先发布和浏览器逐行绘制；本批不修改生产线程、
  `fusion_mode`、API/SSE schema、评分、冻结或运行配置。

- 本批只补充后续策略研究路线，不修改活动 v17、固定 0.68/0.32 融合、硬预算、冻结、
  运行配置、API schema 或 Web 行为。计划要求先恢复至少 95% 历史覆盖并积累点时配对样本，
  所有判断条件调整先以不增加 DeepSeek 请求的 v18 影子运行。

- G5 不改变任何产品、策略、schema、配置、迁移、运行代码或 Web 资源；最终交付状态从
  “A5 已完成、G5 未发布”推进为“G5 已发布”。“最终一个交付 commit”按仓库强制规则落实为
  G5 独立批次只创建一个新提交，保留此前已推送审计历史且不 squash/amend/force-push。

- C5 在不改变公共 schema 版本、融合权重或 HTTP 硬预算的前提下收紧 V4/Pro 解析：拒绝
  未知字段、错误文本类型、超长响应和 manifest 外证据；正向催化/价格/基本面/政策映射只
  接受合格可信证据，跨时区事件按统一时间去重。raw facts prompt/cache 只包含共同结构化
  输入和证据身份，普通策略分数、board policy 与 merge epoch 不再拆分跨策略原始复核。

- P6 resident 初始化、current 恢复、cold future ownership、完整日期装载和按日期淘汰拆为
  单一职责私有方法；完整日期补足、三策略 single-flight、异常传播、LRU 和字节上限行为不变。
  严格复杂度债务从 `C901=38 / PLR0912=16` 降为 `36 / 15`。

- `SnapshotPublisher.resync()` 现在与 Web 游标/慢客户端 resync 使用同一 v2 事件身份，
  固定携带 `patch_schema_version=2`。SSE 订阅在 publisher 锁内保存打开时的服务端序列，
  游标原因分类不再读取生成器运行时可能变化的 sequence；历史回放、队列和事件 ID 不变。

- B5 将成员插入/删除视为所有登记字段族的脏变更，并让板块/行业 dirty 集合同时覆盖旧、新
  快照维度；普通纯报价 overlay 仍保持窄 dirty 路径。B4 报告同步采用其 acceptance JSON
  已保存的准确 P95、RSS/USS 和已发布提交哈希，不改变历史门禁结论。

- B4 将标准化、观察值构造与两源合并的固定组合路径纳入相对验收，严格复杂度债务基线因拆出
  有效观察合并 helper 从 `C901=39` 下调为 `C901=38`；新增的 Polars 入口只在 infra 内部可见，
  不改变公共行情 schema、应用端口、配置或 Web API。

- publisher 状态把 100ms 内部 SSE 入队耗时与 2s 权威发布年龄拆成两个有界 P50/P95 指标；
  无游标的新 SSE 连接从当前 sequence 开始，只在显式 `Last-Event-ID`/`cursor` 时执行历史恢复。
  patch ETag 现与 `snapshot:trade_date:view` HTTP 身份一致，dashboard 资产版本提升到 15。

- A4 质量复验在安装声明的 Polars 后发现 mypy 会递归解析当前 NumPy 第三方 stub，并以
  Python 3.10 目标拒绝 stub 中的 Python 3.12 `type` 语句；`pyproject.toml` 现把 Polars
  明确作为 mypy 外部导入边界，活动 `src/trader` 仍按完整严格规则检查，未改变运行依赖或
  Python 3.10-3.14 产品范围。

- 运行态推荐发布改为 P6-first 接纳：普通草稿、重算和重启恢复只有在 P6 接纳后才更新
  RuntimeState、session、检查点与 SSE；正式冻结继续先落不可变记录，但 P6 拒绝时不切换
  运行态、不设置冻结标记、不消费检查点、不广播新身份；较旧正式三策略记录进入驻留历史时
  日期索引保持倒序且不替换当前投影。

- A3 集成后，权威策略文档从旧 DeepSeek 证据/预算口径收敛到当前实现：每股 prompt 证据
  上限为 12，long 物理请求永久为 0，today/tomorrow/d25/shared_preheat/emergency 软桶为
  22/14/12/10/5，Pro 挑战者批次最多 4 只且全日软上限 8；普通 quote-only change 命中 raw
  facts cache 时只做本地投影和 P6/SSE 发布，不新增 DeepSeek HTTP。

- A3 集成后，权威 Web/SSE 契约明确 recommendation/overlay patch 均使用
  `patch_schema_version=2`，推荐 patch 携带 base/current projection、ETag、view、upserts 和
  removed codes；overlay 只携带报价字段且必须匹配当前 projection，不匹配时触发 ETag
  resync。

- A3 Review 发现 `scripts/check_refactor_quality.py` 的严格债务基线落后于已推送 `HEAD`
  实际计数；已同步为 C901=39、N818=5、PLR0911=15、PLR0912=16、PLR0913=55、PLR0915=11。
  本批新增的列式 options 值对象把自身引入的 PLR0913 增量降回 0。

- G2 状态从阻塞更新为已发布；A 只发布阶段 2 共同门禁，仍不合并 B/C/D 生产实现、不连接真实
  实现、不修改生产默认，A3 等待下一次用户继续指令。

- G2 阻塞原因从“B2 未就绪且 C2 缺标准字段”收敛为“B2 未就绪”。A 继续只登记报告状态，
  不合并 B/C/D 实现、不连接真实实现、不修改生产默认、不开始 A3。

- G2 状态明确为未发布：A 只接收并登记阶段 2 报告，不合并 B/C/D 实现、不连接真实实现、
  不修改生产默认，也不开始 A3；当前等待 B2 补 `ready_for_gate=yes`。

- A2 将运行配置 `performance_budgets.memory` 从旧 `cache_total_bytes` 单字段改为
  `cache_logical_bytes=260046848` 与 `process_peak_rss_bytes=402653184` 双字段，并更新解析
  校验、性能报告 payload 和权威设计说明；旧字段或把进程峰值当缓存容量的配置会在启动前被
  拒绝。同步收紧 `PublishedSnapshot*Port.status()` 返回类型，避免 application 公共边界继续
  暴露 `Mapping[str, object]`。

- A1 基线报告从“等待 C1/D1，G1 未发布”更新为“B1/C1/D1 均已收到，G1 已发布”，并把
  `ready_for_gate` 更新为 `yes`。本批仍不进入 A2.1-A2.5，不实现公共 port/event/config
  骨架，也不执行 B/C/D 内部算法。

- 权威文档将 P1-P6 内存验收从旧 `248/8/256 MiB` 单进程口径调整为
  `248 MiB` 逻辑缓存和 `384 MiB` 迁移期进程峰值 RSS 双层契约，并冻结
  `p3_p4_feature_snapshot_market_change_set_v1`、`p4_p5_high_value_review_manifest_v1`、
  `p4p5_p6_projection_event_v1`、`p6_overlay_event_v1` 与
  `deepseek_v4_review_facts_v1`。本批只做 A 侧契约和基线，不执行 B/C/D 内部算法；
  具体配置、port/event 实现和集成替身留给 A2。

- 本批现状判断：三组 v16 缓存不能隔离观测、规范快照、特征、评分、模型复核和交付生命周期；
  查询用例直接持有归档仓储导致热读访问 SQLite；盘中草稿与 overlay 被完整持久化；SSE
  失效通知又触发浏览器完整 GET。修改后配置严格固定 248 MiB 六池与 8 MiB 运行预留，
  Web 只读用例只依赖 P6，普通草稿/盘中 overlay 只更新 P6 与局部 patch，冻结、检查点和
  closing overlay 才进入持久化边界；活动运行目录由 `.runtime/v2` 隔离到 `.runtime/v17`。

- 行情、DeepSeek、预算与快照观测从多继承 mixin 改为构造时显式注入的类型化组合；复杂预算
  批次入口改用不可变请求/完成对象。历史 K 线现在强制生产者声明 `raw/qfq` 和来源，120 点
  预热主路径只消费腾讯或供应商明确返回的 qfq 数据；融合公式、排名、冻结、API 和 Web
  行为未改变。

- 现状判断是外部 SDK 延迟之外的主要浪费来自 Python 行对象重复物化、变化范围过度重算和
  SSE 通知后的完整 HTTP 回读；计划固定 Polars 只进入 `infra` 的 P1-P3，最多 360 候选
  在 P4 前物化为现有领域对象，不复制评分实现，也不增加 DeepSeek 请求。权威文档治理
  同步登记 `plan_sudu.md` 仅可细化既有第 7.2 节 P1-P6 原子章节，禁止形成第二套契约。

- 流水线事件审计改为有类型记录和状态枚举，持久化边界负责转换，跨线程 JSON 深层冻结；
  24 参数构造函数收敛为三个显式对象，业务时间线、Web v3 和冻结投影保持不变。

- 用户决定暂缓历史 60 日数据与 300 条配对建设，先完整设计实时荐股和 DeepSeek 低成本
  协同方案。重写 `docs/plan_c.md`，固定本地主链、新闻/公告证据门槛、V4 结构化事实、
  确定性映射、高价值候选路由、Flash/Pro 分工、跨策略缓存、实施批次和延期晋级门禁；
  历史回补、shadow、Bootstrap、自动调权和在线学习均移到后续独立任务。

- 领域值对象和纯函数按行情事实、推荐决策、结构化复核、冻结结果四种能力重组；过滤规则改为
  表驱动注册对象，板内横截面构建拆为样本基准、人口统计和特征丰富三个阶段，长期研究拆为
  估值、成长、质量、事件、行业政策和保护纯函数，排名限制改为显式策略与选择状态。活动源码
  仍全部低于 800 行，领域严格 Ruff 告警由 18 项降为 0，仓库债务基线从 163 项降为 145 项。

- `SelectionPolicy` 与 `RecommendationPolicy` 的空映射和默认硬过滤策略改由 dataclass
  `default_factory` 为每个实例创建，再沿用既有不可变副本逻辑；候选、评分、风险、融合、
  排名、冻结、Web API 和持久化格式均不变。Ruff 开发工具固定为 0.15.21，避免严格诊断
  基线因工具规则版本漂移而失真；原候选总体和收益证明路线保留为工程重构后的独立策略批次。

- tomorrow 三板各把 15% 权重转给确定性入场质量，d25 用入场质量替代原独立
  `not_overheated` 正向组件；融合公式、DeepSeek 预算和冻结时点保持不变。正式推荐与观察
  候选改为独立 Top10/Top8，再分别应用相同集中度规则，Web 当前视图分为“正式推荐”和
  “观察列表”，历史视图与原 `items` 单数组兼容契约不变。

- 文档治理仍保留软件业务设计和荐股策略两份唯一权威，但允许 `docs/plan.md` 作为唯一的
  非权威待办载体；计划与权威契约冲突时以后者为准，实施引起契约变化时必须先修改权威
  文档。交付契约测试同步限制 `docs/` 为两份权威文档加这一份执行计划。

- 用户要求行情服务做“类级完全重构”，而不是继续保留九个 mixin、单个共享状态基类和
  隐式继承依赖。现改为由 `bootstrap.py` 显式组装 `QuoteStore`、`HistoryStore`、
  `HistoryWarmup`、`ResearchLoader`、`IntradayLoader`、`ReferenceLoader` 与执行、健康
  组件；每个组件独立拥有有类型的状态、锁、缓存和外部资源。`MarketFeatureService` 只负责
  协调并实现既有 `MarketDataPort`/当前报价读取边界，不再持有通用 `_lock`、业务缓存或
  模板方法。产品 API、评分、冻结时点、运行数据与序列化格式保持不变。

- 用户诉求：按工程 Review 计划先修复 v15 行情热路径性能。固定 5,500 股双源负载下，
  两源合并和统一快照 P95 分别约为 1,761ms 与 2,252ms，超过 1,000ms/1,500ms 契约；
  根因是逐字段反复规范化来源、分配候选列表并扫描排序，以及规范 JSON 在 Python 中递归
  复制完整对象并重复反射 dataclass 字段。现改为单次扫描选择各字段、对小型来源/字段元数据
  使用有界缓存，并让标准 JSON 编码器原生遍历容器。字段优先级、同序首值规则、冻结 schema、
  缓存键、哈希和内存估算口径均保持不变；本批不处理计划中的下一项历史预热优化。

- `close_fallback` 现在是数据库缺少同日正式记录时创建的正常历史结果，而非临时草稿：
  连续运行保持 P6 股票、评分、动作和排名，仅换入收盘价；冷启动不新增 DeepSeek HTTP，
  使用本地规则完整重建并由正式接口返回 `ready`。页面明确标记“收盘补算”，并提升静态
  资源版本避免浏览器复用旧脚本；已有同日记录仍拥有最高优先级且不可覆盖。

- 用户诉求：将活动源码单文件行数门禁从 500 行调整为 800 行。现状是权威设计与架构
  契约测试同时固定 500 行，单改文档会造成契约与门禁不一致；修改后
  `software-business-design.md` 与架构测试统一以 800 行为上限，超过上限仍必须按职责
  拆分并说明，且继续禁止含义模糊的聚合模块。本批不改变运行逻辑、策略、API、冻结格式
  或打包内容。

- 推荐接口保留原路由并直接升级为 v3，不提供 v2 兼容 shim；逐股响应只保留身份、核心/
  锚点行情、报价身份、动作、四项关键评分、精简风险和复核终态。完整特征、证据、板块
  计算、缺失原因和 DeepSeek 技术审计继续参与领域计算、冻结持久化和离线观察，不因页面
  精简而删除数据采集或改动评分、动作、排序、SSE、ETag 与冻结哈希。

- 历史日期页在可见时每 3 秒重新读取 P2 实时字段，并在同策略报价 overlay 推送时立即
  重读；行级更新继续校验策略、日期、视图和快照身份。正式当前与临时实时使用独立缓存
  和 ETag，临时草稿明确显示“不替代正式冻结”。

- 活动适配器层目录与包名原子统一为 `src/trader/infra`；组合根、
  entrypoints、层内导入、全部测试与性能 runner 同步使用 `trader.infra`，依赖方向和
  业务行为保持不变。`AGENTS.md` 与软件业务设计的架构边界同步采用新名称。

- 实时最短计划间隔调整为：全市场 3-10 秒、120 候选 1-2 秒、TopK 1 秒、本地评分
  3-10 秒；在途任务继续跳过重叠周期，每个来源 lane 只保留最新观察点，因此实际周期
  自动贴合接口完成速度且不补跑旧周期。Tushare 120 积分仍只承担 SDK `daily` 历史，
  盘中全市场使用东方财富/新浪并行路由，候选及 TopK 使用腾讯定向行情。

- 历史日线优先尝试官方 Tushare SDK/API 的 120 积分 `daily` 未复权数据并完成单位归一；
  供应商明确拒绝该接口时永久降级到腾讯完整前复权日 K，东方财富作为第二回退。历史下载
  使用独立 5-worker 有界池并连续链式预热，不再与全市场、候选和 TopK 实时任务争抢
  worker；证券主数据、交易日历、复权因子、`pro_bar`、日度估值和财务指标仍须 2000
  积分并按配置显式启用。

- 推荐 HTTP 当前路径改为读取线程安全的运行态快照与 overlay；服务启动在接收 HTTP 前预热 today/tomorrow/d25 最近 20 日冻结投影，热历史请求不再逐次打开 SQLite、校验哈希和读取完整 JSON。交付投影保留推荐项、评分、证据、锚点和摘要元数据，但不复制逐股全市场筛选审计与冻结重放输入；冻结文件、哈希、数据库和离线核验对象保持不变。

- 当前策略查询只接受 `trade_date == current_trade_date` 的快照；今天尚无快照时直接返回空 `not_ready`，昨日冻结仍可通过日期选择作为历史查看。历史当日行情索引在特征批次尚未提交时可只读复用行情网关已经成功合并的规范报价，不发起新请求。前端资源版本升至 `dashboard.js?v=9`，移除上一交易日 current fallback 的缓存身份和提示分支。

- 文档治理从单一综合需求文件调整为职责互斥的双文档模型；`AGENTS.md`、README 和交付契约测试同步使用新路径与更新边界。合并保留五来源、双冻结、六阶段 256 MiB 目标、v16 三板九组权重、七项本地风险、DeepSeek 188 次预算、68/32 融合、动作阈值、集中度和 long 观察公式，并把尚未完成的 v17 P1-P6 发布池/Web 热路径/冻结检查点/性能 CLI 明确登记为下一完整工程章节；本批不改变任何运行配置、代码、公式或产品行为。

- 历史推荐查询现按历史股票代码只读访问 P2 已缓存的全市场/候选报价索引，不再要求该股票当天仍位于同策略 TopK；HTTP 路径不刷新行情、不评分、不访问网络，也不修改冻结快照、JSON 或 overlay。历史响应继续使用原字段名，当前价、今日涨跌和锚点至今只由同一上海自然日的实时行情派生。

- v16 today/tomorrow/d25 现按三板策略完整启用并保留 long 当前观察语义；将板块评分辅助计算、推荐最终合并、推荐/回放模型、极端结构风险、市场任务执行和快照 review codec 按职责拆分，所有活动源码重新低于 500 行。`docs/need.md` 第 13 节明确旧 d25 双乘数只用于 v14/v15 回放，活动 v16 以第 26.7 节显式不过热组件为准。

- 运行配置继续固定 5 个普通来源 worker；组合根在同一有界数据池中额外创建 1 个 worker 和 1 个等待槽作为紧急 lane，候选及 TopK 腾讯定向报价走紧急 lane，全市场、历史、分钟和研究任务继续走普通 lane。状态 API 新增紧急 worker、容量、在途、提交、完成与拒绝计数，不改变 3 秒候选报价截止、刷新 cadence、来源 single-flight 或冻结规则。

- D25 周期风险刷新改为复用仍在 10 分钟 TTL 内的成功结构化研究，只提交缺失或已过期代码；失败或截止结果继续使用不超过 60 秒的负缓存后重试，不改变空值降级、风险硬过滤、14:50 冻结或来源超时上限。

- 看板脚本资源版本由 `v=7` 提升到 `v=8`，确保浏览器获取包含快照身份对账的脚本；SSE 正常时仍不周期轮询完整推荐响应，历史日期查询也不参与当前身份对账。

- 运行配置升级到 schema 5：生产组合根只创建一个 `source-data` 执行器，包含 `5 normal worker + 5 pending` 的五来源普通 lane，以及 `1 urgent worker + 1 pending` 的腾讯候选/TopK 紧急 lane。东方财富/新浪并行形成全市场快照，腾讯只做候选定向报价，Tushare 只做主数据、日历、前复权日线、估值和财务，AKShare 继续提供研究数据。缓存 TTL、动作年龄、容量、缓存组字节上限和性能预算分别只由 `market_data.cache_policy` 与 `performance_budgets` 注入；旧冻结继续按 v14 回放，新冻结算法标识为 `engine_v15_parallel_market_data_2026_07`。
- 三板身份按 Tushare 主数据、AKShare 清单、行情市场字段和代码前缀降级确定；前缀降级、身份冲突、上市日期/日龄不可验证和多源价格未复核偏差只允许观察，上市第 0-5 个交易日、重上市首日和退市整理首日直接排除。主板 8.00/8.01、创业板和科创板各自 16.00/16.01 使用独立过滤码，无价格限制状态不再计算普通涨停接近度。

- 运行配置升级到 schema 4 和 158/188 阶段目标，策略配置升级到 schema 8；四类结构化负面风险与配置黑名单统一在评分和 DeepSeek 前硬过滤，原本对应的本地风险触发关闭以避免重复扣分。主审/挑战者请求身份包含模型角色、思考模式、reasoning effort、prompt/schema 版本，V4-Pro schema 修复在内存中回传供应商 `reasoning_content` 且不落盘；桌面明细新增两阶段模型、状态、指纹、cache token 和证据 manifest 审计。
- 将 settings、pipeline、recommendations、DeepSeek reviewer/budget、市场服务/AKShare/特征、快照 codec/writer 和 dashboard CSS 按配置模型与校验、生命周期与任务、请求与状态、缓存与研究、序列化与观测等职责拆为显式模块；原门面、依赖方向、组合根、公共入口和运行资源所有权保持不变。

- DeepSeek 预算与共享快照连接边界改为事务与资源生命周期一体的上下文管理器；仅冻结提交保留显式拥有并关闭的原始连接。状态 API 在 SQLite 打开或读取失败时只降级预算依赖，不改变最近有效推荐、冻结记录、评分、188 次原子预算规则或其他只读状态。
- 落地 P12 证据落盘统一细化：`MarketFeatureService._load_research_cache()` 与 `_write_research_cache()` 对 news/structured 证据统一按 `ResearchObservation` 序列化/反序列化落盘，过期缓存返回 `None` 并退回网络；`AkshareResearchClient._cache_payload()` 使用 `atomic_write_json` 统一落地原始 payload，便于 restart 重放与故障复盘。补充组件测试：`test_akshare_news_response_is_cached_with_atomic_writer`、`test_research_cache_is_used_after_restart_before_source_request`、`test_research_cache_expired_calls_research_client`。
- DeepSeek 审计字段闭环完成：`infra/deepseek/schema.py`、`infra/deepseek/reviewer.py`、`infra/persistence/snapshots.py`、`web/schemas.py` 连续透传审计元数据（模型、思考模式、挑战者状态、置信度与 hash）；旧快照在反序列化时回退为 `primary/not_run/neutral`，新增三类测试覆盖解析、持久化和 API 合约。
- `domain/ranking.py` 的 `select_top_k()` 新增可解释性排序维度：在同 `final_score` 下优先保留审计信号较优（挑战者通过、二级阶段、rating 较好、置信度更高）的候选；为保证可复盘与透明，新增 `tests/unit/domain/test_ranking.py` 覆盖同分 tie-break。
- `docs/issues/2026-07-20.md` 第11节“审计信号参与候选重排序”补齐收益型 shadow 阶段：`metadata.shadow_scoring` 作为只读审阅字段接入快照输出，便于后续按 `rank_gap` 和覆盖率复盘 today/tomorrow/d25 候选替换效应。
- 落地 `docs/issues/2026-07-20.md` 标准化收敛：三源行情适配器统一 `normalize.py` 的解析入口，`normalize_quotes` 与 `MarketQuoteInput` 处理空值/非法值与 `MarketQuote` 构建，降低 parser 分支漂移风险并提升 `quotes` 批量解析可读性。
- 落地 `docs/issues/2026-07-20.md` “标准化/路由/数据库”继续子项：路由器增加 `_is_empty_payload()` 空结果收口，`normalize_quotes()` 收紧 `MarketQuoteInput` 校验约束并隔离非法行情输入，`tests/unit/test_v2_sqlite_migrations.py` 新增 `schema_version` 异常元数据回归。
- 落地 `docs/issues/2026-07-20.md` “标准化/路由/数据库”数据库补齐：`src/trader/infra/deepseek/budget.py` 在初始化阶段加入 `schema_meta` 自恢复逻辑，`SCHEMA_VERSION` 按版本向上修复；补充 `tests/unit/test_v2_sqlite_migrations.py` 与 `tests/component/test_v2_deepseek.py` 对空白/非法/缺失 `schema_version` 的初始化路径回归，避免脏元数据导致启动阻塞。
- 落地 `docs/issues/2026-07-20.md` 第四档：新增 `StandardizedFeatureBuilder` 协议并让 `MarketFeatureService` 按协议注入 `FeatureBuilder`，为后续替换特征构建实现保留横向扩展点，不影响 `FeatureBuilder` 当前口径与现网行为。
- 落地 `docs/issues/2026-07-20.md` 性能向量化方向（第三档 #11）：`infra/market_data/history.py` 新增 `HistoryProfile` 与 `summarize_history_metrics()`，在 `FeatureBuilder._raw_features()` 中把 `MA5/20/60`、20 日波动率、20 日最大回撤、20 日成交额中位数、20 日上涨一致性改为一次汇总取值，减少同一历史序列重复计算；`return_pct` 与返回字段值不变，结果可复用性增强，适配后续批次“按批构建 Raw 特征向量”。
- 落地 `docs/issues/2026-07-20.md` 本轮市场路由优化：`infra/market_data/gateway.py` 的 `_fetch_market_once()` 改为一次性将 `eastmoney/sina` 路由表提交给 `MarketDataRouter.route()`，替换逐条 `route((vendor,))` 的循环包装。行为保持 `required` 顺序回退与失败计数、熔断、缓存回退不变，但异常信息从“汇总逐 vendor”转为路由器聚合异常，便于后续可观测性归一化。
- 落地 `docs/issues/2026-07-20.md` 市场数据异常语义：`infra/market_data/router.py` 现在聚合 required 路由失败与无数据，`required` 全部耗尽但存在空返回时抛 `MarketDataNoData`，全失败则抛带 vendor 摘要的 `MarketDataFailed`；`tests/component/test_v2_market_data.py` 新增路由级 no-data/failure 回归，并修订网关路径对无数据与失败上下文的可观测性。
- 落地 `docs/issues/2026-07-20.md` 路由可观测性细化：`router.py` 的路由结果新增 `status/degraded/fallback_reason` 与 vendor 明细（name/status/severity/error/duration），`gateway.py` 在 `health()` 增加 `route` 子字段透传最近一次路由快照；新增/更新 `tests/unit/test_v2_market_data_router.py` 与 `tests/component/test_v2_market_data.py`，验证降级链路与可观测输出。
- 路由可观测性继续细化（2026-07-20）：`/api/status` 的市场数据路由健康进一步透传 `attempted_count/success_count/failure_count/no_data_count/skipped_count` 与 `used_vendor`，并通过前端状态摘要新增“路由健康”卡片，展示 degrade/fallback 与 vendor 级 `status/error`，用于故障归因与熔断排查。
- 落地本批续写可观测性回归：`tests/unit/test_v2_market_data_router.py` 补齐 optional/required 混合、no-data 聚合、错误聚合顺序等边界；`tests/unit/test_v2_sqlite_migrations.py` 补齐 schema 初始化幂等性与版本迁移回归。
- 落地标准化特征注册表契约回归：`infra/settings.py` 在加载策略配置时校验 `factor_contract.feature_schema_version` 与 `FEATURE_SCHEMA_VERSION` 一致，支持 `feature_names` 与 `feature_schema_expected` 与 `FEATURE_SCHEMA` 全量一致性；`tests/unit/test_v2_settings.py` 新增版本错配拒绝与显式注册表一致性回归。
- 落地 P14 评级映射：`domain/risk.py` 的 `Rating/parse_rating` 已在 `infra/deepseek/schema.py` 中应用到 `results[*].rating`，`domain/models.py` 与 `domain/ranking.py` 记录并消费评级，`application/recommendations.py` 将 `APPLIED` 审核中的 `deepseek_bearish/neutral` 映射为显式动作降级；新增 `tests/unit/domain/test_risk.py`、`tests/unit/domain/test_ranking.py`、`tests/component/test_v2_deepseek.py` 覆盖解析与动作映射。
- 落地 `docs/issues/2026-07-20.md` 第一批 P1~P3：新增 `infra/deepseek/factory.py` 的 `create_deepseek_client()`，并把 `infra/container.py` 的 `DeepSeekReviewer` 依赖由直接实例化 `DeepSeekHttpClient` 改为通过工厂注入 `DeepSeekClientBase`，为未来 provider 切换（mock/vLLM）保留入口；同时补充 `tests/unit/test_v2_deepseek_base.py` 工厂测试（默认 provider、兼容 alias 与未知 provider 异常）。
- 下一版契约将 today、tomorrow、d25 的横截面改为主板、创业板、科创板独立总体，拆分两类 20% 板过滤身份，并规定 v10 删除 d25 过热与市场状态双乘数；当前 `baseline_v9_active` 的候选、评分、动作阈值、行业限制和双乘数继续生效，68/32 融合、DeepSeek 158/188、11:20/14:50 冻结及 long 固定观察边界不变。本批只更新文档，不修改配置、活动代码、数据库、API 或 UI。
- DeepSeek 正常交易日目标由 144 调整为 158，阶段目标重新分配为 shared 15、today 68、tomorrow 35、d25 30、long 10；其中主审及预热共 141 次，today/tomorrow/d25 挑战者目标和上限统一为 6/6/5 共 17 次。六个预算桶、emergency 使用条件、冻结截止和 188 次物理 HTTP 请求硬上限均不变，候选不足时仍禁止为凑目标空调用。本批只更新需求契约，不修改配置、活动代码、数据库、API 实现或 Web 行为。
- 开源参考表改为截至 2026-07-19 只展示当前不少于 20,000 Star 且与量化、金融研究、A 股数据或 DeepSeek 接入直接相关的项目；新增项目分别标明可借鉴边界、非运行依赖及非 DeepSeek 项目身份。
- DeepSeek 正常日目标由 133 调整为 144，新增 11 次目标全部在既有策略桶中用于 today/tomorrow/d25 挑战者，策略桶上限、emergency 规则和全局 188 次物理请求硬上限不变；today 挑战者 11:18 停止提交，其余请求仍遵守 14:48 截止和 11:20/14:50 冻结。本批只更新需求契约，不修改配置、活动代码、数据库、API 实现或 Web 行为。
- 本批次只更新开源参考契约和链接，不新增 Python 依赖、不复制外部源码，也不修改本地/DeepSeek 评分、风险、预算、冻结、API 或 Web 行为；DeepSeek 荐股项目仅作为 provider 封装、多智能体分工、证据展示和历史验证机制参考。
- 当前推荐 ETag 绑定当前交易日、快照、overlay 与 fallback；历史响应必须精确匹配请求日期，前端缓存同时校验策略和日期，只接受明确标记的上一交易日 stale fallback。桌面顶部补齐行情来源/时间/年龄、评分时间、DeepSeek 已用/剩余和冻结状态，历史表与明细抽屉补齐今日涨跌、锚点至今、权重、截尾口径和风险评估。SQLite schema 升级到 v3，为来源健康审计补充有界错误摘要。
- DeepSeek 五维原始结果按策略配置中的权重、至少两个已知维度和 0.50 加权置信覆盖在本地分类；全部维度未知或覆盖不足逐股 `abstain` 并回退本地分。每批物理 HTTP 仍最多 8 股、最多两次尝试，429 遵循 `Retry-After`，非法 schema 的修复与网络重试共享两次硬上限，14:48 后不再预留请求。
- 用户诉求：将含义宽泛的快照“生命周期”命名改为职责导向名称。现状判断：该模块实际负责编排评分、冻结和实时 overlay 流程，不拥有应用或资源生命周期；现重命名为 `snapshot_workflow.py`，同步生产导入和 `docs/need.md` 结构契约，不改变评分、冻结、持久化、API 或线程行为。
- 生产行情分页、历史、分钟和研究任务改为复用组合根唯一的 6-worker 数据池；策略评分拆为 worker 内的不可变本地准备、DeepSeek worker 复核和单合并线程融合/TopK，long 使用独立低优先级 worker，并在三策略复核完成后优先复用策略无关缓存。应用层按事件生命周期、worker 阶段和快照/冻结生命周期拆分，SQLite/JSON 发布、冻结、overlay 和事件状态统一经单写线程串行提交；Web 查询端只依赖拆分后的只读事件端口。
- 运行配置升级为 schema v3，以逐任务、逐阶段 `pipeline.cadence_seconds` 驱动全市场、候选、TopK、评分、行业、新闻、风险和参考数据事件；周期错过或同类任务仍在运行时从当前时刻重新计时，不突发补跑。冻结、DeepSeek 截止和收盘单点可在延迟或重启后幂等补提交，14:49:50 最终候选只允许在冻结前补交。
- 策略配置 schema 升级到 v7，d25 的 15/30 过热边界、0.85/0.75 系数、市场宽度 60/40 分类与 1.03/1/0.92 状态乘数，以及 long 的年化、估值、成长、质量、公告关键词、质押和解禁公式全部进入规范化策略哈希；冻结回放算法升级到 `engine_v9_section13_2026_07`，d25/long 输入版本绑定完整因子值与结构化证据版本。
- 策略配置 schema 升级到 v6，新增固定 30/30/25/50 尾盘信号参数及原始/派生因子登记，策略哈希覆盖完整契约且启动时拒绝登记与执行窗口、公式、样本、缺失或范围互相矛盾；冻结回放算法升级到 `engine_v8_section12_2026_07`，tomorrow 输入版本同时绑定候选报价和分钟证据，Evidence/API 增加可选接收时间与数据版本；DeepSeek 证据子集校验与实际进入 prompt 的 16 条上限保持一致。
- 本批次仅新增策略文档审计归纳，不修改 `docs/need.md`、配置、活动代码、测试、公式、阈值或运行行为；当前 `need.md` 继续作为唯一业务契约，v1 的模拟交易、结果回填、产品内验证、自动调参和预测能力没有被重新引入。
- 策略配置 schema 升级到 v5，`today_news_signal` 的 72h/1h 窗口、75/50/25 分值和有界正负词表进入规范化策略哈希；`news_sentiment`、`evidence_freshness` 因子登记升级到 v2，冻结回放算法升级到 `engine_v7_section11_2026_07`。
- 策略配置 schema 升级到 v4，冻结回放算法升级到 `engine_v6_section10_2026_07`；本地风险统一由通用规则解释器触发，缺失和非有限输入不再隐式转为零，API 同时返回 25/30 分封顶前风险明细合计。
- 冻结回放算法升级到 `engine_v5_section9_2026_07`；硬过滤股票数改为按审计明细中的唯一股票计数，Top120 池截断不再误算为过滤，long 不再继承普通候选池的过滤记录。
- 策略版本改为由除声明标签外的完整规范化策略配置生成 `strategy_sha256_*`；运行配置版本与该哈希组合后写入快照，任一因子、权重、风险规则、阈值或融合配置变化都会形成新版本身份。
- 运行配置版本升级到 `runtime_v3_freeze_overlay_2026_07_17`，SQLite schema 升级到 v2；冻结 manifest 新增快照 schema 和逐股锚点来源/年龄，冻结 JSON 新增配置版本，运行库补齐 `deepseek_calls` 与 `live_overlays` 表。
- 策略版本升级到 v9、冻结回放算法升级到 v4；today、tomorrow 和 d25 只在各自执行阶段应用动作门槛，预注册数据禁止跨版本混算。
- 用户问题：此前交付规范只要求更新变更日志，没有明确把用户反馈与修改逐项归纳；修改说明：每个批次现在必须在 `Unreleased` 记录问题/诉求、原因判断、行为变化、验证证据和剩余风险，契约变化仍同步 `docs/need.md`，敏感信息禁止入文档。
- 今早、明日和 2-5日推荐在桌面页面启动后后台预取，推荐日期与快照请求并行执行；相同策略/日期请求合并并使用 ETag 后台刷新。
- 策略版本升级到 v8；新闻只对候选和长期观察池抓取，全市场扫描不发起逐股新闻请求。
- DeepSeek 风险事实不再直接控制生产 veto；策略 v7 和冻结回放算法 v3 由本地风险表按风险代码、允许证据类型、证据有效期和最低置信度确定扣分与重大安全 veto。
- 最终验收矩阵明确区分可重复仓库门禁与真实交易日、真实 DeepSeek 密钥、三档桌面截图等外部发布证据；旧 v2 快照继续可读，但不能充当新增冻结复算门禁证据。
- 迁移清单新增 `docs/need.md` 第 24 节逐阶段完成证据；运行手册固定生产影子留证字段和回退 tag `v1-rollback-20260717`，仓库门禁完成与真实交易日发布观察明确分离。
- “继续”命令的交付粒度从下一个最小独立任务调整为下一个完整未完成章节；章节内全部明确子项统一实现、Review、提交和推送，同时禁止顺带合并相邻章节。
- 项目入口统一为 `trader-server` 和 `trader-cli`；Linux/macOS/WSL、PowerShell 和 CMD 启动脚本只调用安装后的入口。
- 依赖、构建、包发现、console scripts、Ruff、mypy 和 coverage 统一由 `pyproject.toml` 管理。
- 运行配置迁移到 `config/v2`，运行数据隔离到 `.runtime/v2`，配置路径必须显式且为绝对路径。
- 最终分固定为 `clamp(local_score * 0.68 + deepseek_score * 0.32 - deepseek_risk_penalty, 0, 100)`，并以 `ROUND_HALF_UP` 保留两位。
- Web 产品范围固定为个人 PC 桌面浏览器；发布验收分辨率为 1280x720、1440x900 和 1920x1080，手机和平板不在范围内。
- v1 需求、设计、研究登记和配置移入 `docs/archive/v1`，`docs/need.md` 成为唯一活动业务契约。
- `docs/hi.md` 的后续交付由两个功能批次扩展为三个独立批次：v15/v16分别建立缓存
  正确性，v17只在固定业务投影和冻结哈希不变的前提下测量并优化。缓存策略只从
  `runtime.json.market_data.cache_policy` 注入，性能预算只从
  `runtime.json.performance_budgets` 读取，禁止适配器、评分lane或性能脚本自带默认值。

### Fixed

- 修复长快照状态和长错误文本换行时撑高整个 Header、把股票列表向下推移的问题；同时
  修复运行服务热加载新 CSS、但仍缓存旧模板时，“正式推荐”旧标题失去样式后反而按默认
  `h2` 放大的现场表现。最终模板、CSS 与 JavaScript 资源版本同步提升，实际本地服务重启
  后已确认只返回同一版布局。

- Web 荐股展示批次：修复短线策略切换会无条件清空显式历史日期的问题；目标策略没有
  同日归档或推荐接口返回 `snapshot_not_found` 时，现在显示该策略、该日期的正常空态，
  不再用当前日期数据替代。策略、日期列表和荐股请求均绑定选择序号，迟到响应不能覆盖
  新选择。

- 修复 P6 初始化只取 today/tomorrow/d25 日期交集，导致旧库中按策略有效但同日不齐全的
  5 份正式历史全部不可见；同时修复收盘协调器只验证三板行情存在、未验证历史横截面，
  从而在 qfq 预热完成前把 `board_population_insufficient`、可靠度不足和 0 只推荐固化为
  当日 `close_fallback` 的问题。当前机器已用只读 `migrate-v17` 将旧 v2 的 5 份 committed
  快照导入 v17，源目录前后摘要一致。

- 修复用户必须理解内部“可变草稿/不可变冻结”发布状态并手动切换视图的问题。两种后端
  状态继续用于盘中实时性、冻结不可覆盖和失败降级，但页面自动选择同日最新有效状态，
  草稿明确提示“未冻结，结果可能变化”，不会冒充正式推荐或回退到上一交易日。

- 修复历史预热把 `unsupported` 当作第四板平均分配，导致三个活动板块理论上每板最多
  90 只、永远低于 100 样本门槛的问题；同时修复尚未开始就被取消的分钟请求被写入负
  缓存、后续评分无法继续推进的问题。无合格候选现在报告
  `deepseek_skipped_no_eligible_candidates`，不再误报为 DeepSeek 请求不完整；永久不
  复核的 long 不再附加任一 DeepSeek 降级原因。

- 修复正式性能报告用 Polars self-join、排序和 JSON 序列化代替生产链路，导致旧门禁无法
  暴露实时瓶颈的问题。运行态现在对事件和行情周期记录有界完成/失败/超时/被替代/丢弃
  结果；同一 trace 的重复终态不会重复计数，服务端状态不泄漏关联身份。

- 修正文档治理仍把已经闭合的 `plan.md`、`plan_c.md`、`plan_sudu.md` 和
  `plan_youhua.md` 当作活动计划的问题，并补齐此前仅在实现/测试中存在的 V4 本地映射和
  P1-P3 列式增量约束。
  同时明确排除旧计划中与生产配置冲突的“至少 3 维/覆盖 0.60”“today 全部 11:18
  截止”和“long 硬桶 0”等拟议值，避免删除计划时把过期设想误升格为生产规则。

- 第 2.6 节此前仍为 pending，严格质量脚本还接受 `C901/N818/PLR0911/PLR0912/PLR0913/
  PLR0915` 共 137 项既有债务；原因是组合、缓存回退、并发调度、DeepSeek 和 Web 请求职责
  长期堆叠在大函数与宽参数边界。现已拆分恢复、调度、缓存、降级和序列化阶段，使用真实
  `Error` 命名与类型化请求边界，并把上述严格诊断归零；冻结、188 次物理预算、固定融合
  和最近有效快照降级语义由回归保持。

- 文档计划修正“当前毫秒级 P6/API/SSE 是实时链路首要瓶颈”的判断偏差，明确优先处理
  定向报价重复重建 5500 行、DeepSeek 同步等待占用 merge/event 线程、SDK Session/分页
  生命周期、浏览器整表重绘和合成性能门禁失真；生产缺陷仍待各独立实施批次验证和修复。

- 文档计划明确修正后续实施中容易出现的四类设计缺口：结构化风险硬过滤使软扣分失效、
  候选分与最终分不一致造成不可观测误杀、回踩和突破分支错误共用全部输入，以及缺失值在
  评分、可靠度和动作门中的语义不一致。本批只记录修复方案，尚未改变生产判断。

- G5 交付契约修复“最终共同门禁可以在缺少任一 B/C/D 签字、A4/A5 状态或 Git 一致性证据时
  被文字宣告发布”的流程缺口；报告必须同时包含四项完成条件、单批提交语义及
  `HEAD == @{upstream}`。本批未发现需要修改的生产缺陷。

- A5-F01 修复阶段 4 P6 新方法通过抬高严格 Ruff 债务基线接纳复杂度的问题；重构后两项
  新增复杂度诊断消失，P6/Web/pipeline 回归保持一致。C5 同批修复 explicit V4 回退 legacy、
  未确认或软来源正向加分、证据时区去重、prompt/cache 身份缺口、重复 coverage 阈值和
  emergency 资格跨物理批次泄漏；所有模型输出仍由本地规则决定应用与风险。

- D5-F01 修复主动 `resync_required` 缺少 patch schema、同一事件类型载荷不一致的问题；
  D5-F02 修复订阅打开时为 `cursor_ahead`、但流式产出前新事件追平后被误报成
  `cursor_expired` 的竞态。两项均先增加精确失败回归，再修改 publisher/SSE 实现。

- B5-F01 修复局部 P3 重算范围可能小于全量重算：股票删除或跨板/跨行业时，旧板块和旧行业
  现在也会标脏；插入/删除代码进入风险 dirty 集合并触发全部相关字段族。新增同时覆盖插入、
  删除、板块迁移和行业迁移的回归，防止旧横截面继续复用。

- A4-F01（Polars 构造失败会阻断有效标量行情）已修复：行情/候选提交在列式批次构造抛出
  `PolarsError`、`RuntimeError`、`TypeError` 或 `ValueError` 时保留 scalar snapshot，生成完整
  invalidation change set，并记录 `columnar_projection_failed:scalar_fallback`；列式合并自身
  失败也记录 `columnar_merge_failed:scalar_fallback`。新增精确注入回归覆盖返回值、health epoch、
  dirty count 和降级原因。

- A4-F04（P6 拒绝后 RuntimeState/session/checkpoint/SSE 仍可能前进）已修复：公共 pipeline
  统一使用 P6 写端口的显式接纳布尔值并调用 `admit_snapshot_to_p6()`；超限、迟到、冻结替换或
  旧日期拒绝时保留最近有效 P6 和运行态，记录有界计数与策略降级。同步、worker、冻结、收盘
  恢复和重启恢复均使用同一接缝。

- C4 修复挑战者尝试误占普通主审软桶、emergency 以不可达策略硬桶判断“普通额度耗尽”的问题：
  普通软用量只计主审，挑战者保持独立 8 次软上限，emergency 在对应主审软桶耗尽后才可用；
  正常/含 Pro/含 emergency 上界保持 58/66/71，全局原子硬上限仍为 188。

- 修复 overlay projection、patch schema/身份/base/TopK 错配被前端静默丢弃的问题：现在按
  原因执行 ETag resync；有效在线 patch 保持零完整 GET。P6 current pin 拒绝冻结后的同日
  迟到草稿和冻结身份替换，且不会被较旧投影覆盖；publisher 同步禁止这些非权威投影发出
  SSE。P6 当前视图超限改为显式失败，阻止调用链继续广播错误 SSE。新页面也不再在完整
  GET 最新投影后重放旧 SSE 导致短暂回滚。

- D4 最终独立 Review 进一步封闭同身份内容漂移：即使 `snapshot_id` 未变，只要同日冻结
  投影内容不同，P6 与 publisher 也会拒绝替换并保持原冻结版本及 SSE sequence 不变。

- 修复架构契约把仅残留忽略 `__pycache__`、没有任何 `*.py` 的退休目录误判为活动业务包：
  目录拓扑与退休路径检查现在只认真实 Python 源文件，仍零容忍旧业务实现，且不会因本地
  解释器缓存产生伪失败。

- 修复阶段 2 合并后公共契约和权威文档之间的漂移：DeepSeek prompt 证据仍写 16 条、long
  仍有预算、SSE 只描述游标恢复而未固定 patch v2 projection/base/overlay 身份。现在文档、
  生产代码和契约测试使用同一组版本、预算、缓存和增量更新语义。

- 修复最近历史按单策略预热可能形成不完整交易日、冷缓存逐项淘汰可能留下部分三元组、慢
  SSE 客户端与正常客户端共享失效式全量回读，以及冻结边界重启只能依赖旧 published 指针
  的问题。现在只接纳 manifest/SHA 合格的完整三策略驻留日期，冷区整日装载和淘汰，慢客户
  端独立丢弃并返回 resync，边界恢复只读取同日、30 秒内、配置一致且未消费的检查点。

- 修复 Tushare 原始价历史此前缺少复权元数据、可能进入收益率、均线和波动率特征的问题；
  历史存储与特征入口现在双重拒绝 raw，未知数据也不能再通过默认值伪装为 qfq，并在 raw
  审计数据出现时回退到腾讯 qfq 历史。修复带来源前缀的 `late` 失败可能被误归类为普通源
  失败的问题，失败元数据保持有界且不含外部载荷。

- 修复万能行情端口、隐式依赖、裸字符串事件状态和共享可变边界造成的耦合风险；异常、恢复
  摘要和行情快照元数据均改为真实类型，停止顺序和冻结 compare-and-set 保持显式。

- 修正文档中 long 仍配置 DeepSeek 维度和正常预算、软目标只有数字而没有消费规则的计划
  缺陷。新计划明确 long 的主审、Pro、预热和 emergency 均为 0，并规定未使用软额度不
  跨策略转移、缓存命中不计数、失败/超时/重试/schema 修复均计入物理请求。

- 修复单个领域 `models.py` 同时承载行情、复核和推荐对象，并通过模块级 `__getattr__`
  延迟导出推荐类型所形成的循环耦合和隐藏 API；修复过滤、评分、排名和研究规则被长嵌套分支
  或 6-10 个参数接口包裹、难以独立验证的问题。所有调用方已原子迁移到新内部路径，不提供
  兼容别名；构造请求时冻结映射和序列，避免冻结 dataclass 间接持有可变输入。

- 修复 Python 3.11 在 pytest 收集阶段拒绝 `mappingproxy` dataclass 直接默认值、导致全部
  业务测试无法运行的问题；根因是三个不可变映射仍被 dataclasses 视为不允许共享的默认
  对象。新增实例隔离与运行时不可变回归，同时清理本机退休 `infrastructure` 字节码目录和
  `docs/.mypy_cache` 对架构/文档拓扑契约的忽略缓存污染。

- 根因确认：v16 主要按上行强度、稳定性和软风险扣分排序，没有融合后不可被高分覆盖的
  MAE/ATR 保护，也没有把推荐后的真实最大不利波动沉淀为可验证结果，因此热度扣分不能可靠
  阻止大幅回撤。本批增加独立动作保护和结果结算闭环；Review 同时修复连续性校验误把
  11:20/14:50 锚点价当作推荐日收盘价的问题，现使用推荐日真实日线收盘校验下一交易日，
  避免把正常午后波动误报为公司行为或价格断层。

- 解决用户指定新增 `docs/plan.md` 与原“docs 只能有两份文件”拓扑门禁的直接冲突；没有
  把性能测量、复权风险或收益假设写成已实现行为，也没有修改评分、过滤、冻结或 Web 运行
  逻辑。

- 修复 15:00 后数据库没有同日记录时正式接口永久 `not_ready` 的断链。原实现只恢复 P2
  报价和已有快照 overlay，没有从 P6 固化或从收盘行情继续候选筛选、评分、TopK、冻结、
  P6/SSE 发布；现在两条恢复路径均闭合到 SQLite/JSON 和 Web，行情或三板不完整时不写
  单策略半成品并继续后台重试。

- 股票详情不再为“无风险、无缺失、无证据、未复核”等正常空状态生成独立区块，也不再
  直接展示机器动作原因；已登记动作原因和风险代码改为可读中文，空核心行情隐藏对应指标
  并统一提示“部分核心行情暂缺”。

- 根因确认：冻结窗口或收盘后启动时 cadence 不再运行全市场任务，P2 当前报价索引保持
  空；历史序列化因此按契约把当前价、今日涨跌和锚点至今返回 `null`。同时查询层在冻结
  时点后只接受同日正式冻结，虽运行态已有 tomorrow/d25 草稿，页面仍只能看到
  `not_ready`。现在冷启动后台单次恢复当日报价索引，显式临时视图读取同日 P6 草稿；
  默认正式接口、历史冻结身份及 11:20/14:50 不可变规则均未放宽。

- 修复适配器层目录名及 Python 导入路径冗长且与团队期望不一致的问题；安装后的公开
  CLI 仍为 `trader-cli`/`trader-server`，只改变内部包路径，不改变 API、配置、冻结、
  DeepSeek 预算、评分公式或 Web 行为。

- 修复今早/明天/2-5 天在全市场内存已有 5,000+ 行实时行情时仍没有推荐的主断链：
  周期性 P2 以前命中 stale-while-revalidate 上一轮缓存，P3 按 20/30 秒边界把整批淘汰，
  再用空结果清除候选池，导致候选报价与评分持续 `skipped_cold`。现在 P3 前等待本轮物理
  刷新，事件按“全市场→候选报价→评分”优先级执行；只有整批因行情陈旧或历史预热不可用
  时保留最近有效候选身份和显式过滤降级，真实合格数据仍可产生合法空池。

- 现场继续定位出四个后续断点：Tushare REST 曾错误请求 `/daily` 子路径、当前 Token 实际
  返回 `permission_denied`、约 360 只历史预热占满实时来源池，以及排队事件用创建时间把
  到达后的新报价误判为未来数据。现改为官方根地址协议并精确分类权限错误，腾讯前复权
  日 K 主回退保留成交量/成交额/换手率，历史使用独立池连续预热；非冻结周期按实际执行
  时间判断行情，冻结仍严格使用检查点时间。修复后现场 P3 恢复约 101/97 个候选，P6
  发布 tomorrow 5 条、d25 1 条和 long 4 条；today 因服务在 11:20 后启动保持 `not_ready`。

- 长时间运行继续复现“配置 1 秒但数分钟不更新”：单事件线程中候选报价还同步等待整批
  尾盘分钟线，且新全市场事件持续越过旧候选/TopK/评分，现场累计 139 个过期事件并错过
  14:50 合格检查点。候选报价现不再同步抓分钟线；TopK 使用独立高优先级，
  全市场/候选/评分改为同级 FIFO。候选和评分的事件排队窗口包含上游最坏耗时，开始执行
  后仍分别截断为 3/15 秒，冻结事件继续最高优先级且迟到数据不得补写。

- 修复当前日期三个策略页面无推荐的上游断链：此前全市场事件在 20 秒内同步等待约 360 只历史而过期；拆分后 P2 实时快照先原子提交、历史独立均衡预热。随后发现数据源最近成交时间比本轮接收时间旧 2-3 分钟，P2 发现阶段误把 5,000+ 行全部判 stale；现在发现阶段按本轮接收时间判断是否赶上周期，候选复核和评分仍保留原始来源时间及 20/30 秒可执行限制。Tushare/东方财富历史失败时优先异步注入本地最近有效种子，不再等待三次熔断，也不会停在内存池而漏掉后续硬过滤、九组预选、三板评分、TopK 与 P6 发布。

- 修复“已经读取内存但接口仍明显延迟”的实际错配：此前仅历史股票的当前报价来自内存，推荐快照仍在每个 HTTP 请求中读取并校验冻结 JSON，且 10 行表格响应携带最多 1398 条无关筛选明细，现场 TTFB 为 0.94-1.80 秒；现在快照、overlay 和报价均走内存热路径，历史交付响应只保留页面所需对象。同步确认“今日涨幅”取当日实时报价 `pct_change`，“锚点至今”由同一实时报价与冻结锚点计算，不再读取锚点日涨幅代替实时值。

- 修复当前交易日没有发布快照时仍显示上一交易日冻结结果和“仅供观察”提示的问题；同时修复首轮历史行情读取只覆盖已提交特征/候选缓存，在现场全市场事件过期但规范行情已成功合并时仍导致“锚点至今”为空的问题。

- 修复历史响应在当日行情缺失时把冻结 `pct_change` 回填到“今日涨跌”的字段混用：锚点价格和锚点涨跌继续保持冻结值；当日行情存在时返回真实今日涨跌并计算锚点至今，不存在或不是当前上海日期时返回 `null`，页面显示 `-`，不再伪造锚点值。根因是查询层只扫描当前同策略推荐快照且序列化层对缺失实时行情回退到冻结报价。

- 修复 v16 半成品导致同步评分仍向 `prepare_snapshot()` 传入已删除参数、异步 future 类型串线、旧冻结被误标 v16 回放、序列化后 tuple/list 元数据哈希不一致、DeepSeek 缓存被纯报价版本抖动无条件击穿、Web/持久化可空数值类型错误、结构化减持字段仍沿用旧合并名，以及目标股票进入领先组后把有效 3 只领导样本误降为 2 只的问题。任一板块失败继续保留最近完整三板快照，不发布偏置 TopK。

- 修复 TopK 定向报价虽由腾讯在亚秒级成功返回，却因共享数据池前方排有大量历史或研究任务而在开始执行前耗尽 3 秒批次截止的问题；紧急报价不再被普通 FIFO 队列饥饿，真实网络超过截止时仍显式保留原降级错误和最近有效 overlay。

- 修复 D25 结构化研究在固定候选顺序和短批次截止下长期停留于低覆盖的问题：成功代码不再每轮被强制重抓并占满 worker，后续候选可在连续刷新中逐步获得财务、公告、质押和解禁证据；v15 截止路径统一抛出可分类的 `MarketDataDeadlineExceeded` 并禁止迟到结果写入内存或磁盘缓存，不再存在旧实现负缓存 TTL 未初始化而抛出 `UnboundLocalError` 的边界。

- 修复页面已缓存上一交易日 fallback 后，若推荐发布事件未触发当前页重读，状态心跳虽已看到服务端新快照但表格仍继续显示昨日数据的问题；现在最迟在下一次 15 秒状态心跳发现身份变化并切回当日快照，手工历史选择、冻结规则、ETag、评分和行情采集链路保持不变。

- 移除执行计划中误写的 Tushare 明文凭据；活动配置继续只声明 Token 环境变量名和受权限保护的可选文件路径，运行日志、快照、缓存、fixture 与文档均不保存凭据值。

- Review 修复多源路径中的确定性与资源生命周期问题：过刷新期行情现在先返回旧值再由原来源 lane 刷新；跨供应商同时点不再用无共同语义的版本字符串改变腾讯/东方财富/新浪优先级；0.50% 定向复核统一用较低价格作分母；缓存优先淘汰业务时间已降级条目；Tushare 版本元数据在服务锁内复制；五个适配器支持协作式停止，Tushare 分代码批次在当前 SDK 调用返回后停止后续调用。另修复 intraday lane 超时返回后仍可修改调用方限制字典的竞态，并把 5500 只证券逐只扫描完整交易日历的上市日龄计算改为一次排序后二分计数。

- 修复最近需求把负面公告、减持/解禁、质押和财务恶化改为硬过滤后，活动实现仍只在 d25/long 本地风险表扣分且 today/tomorrow 不读取结构化风险的问题；风险字段缺失现在保留本地推荐并记录 `structured_risk_unavailable`，真实正等级在四策略评分和模型调用前剔除。修复行情源未显式标记时一字涨跌停无法识别，以及策略配置黑名单未进入硬过滤的问题。
- 修复生产仍默认旧 DeepSeek 别名、无挑战者执行、动态候选数据位于 prompt 固定前缀之前、同一批候选因上游顺序变化导致 prompt 尾部不稳定、挑战者结果未进入策略融合缓存，以及 V4-Pro schema 修复丢失上一轮临时推理字段的问题；挑战者失败、超时、预算耗尽、schema 错误和迟到继续保留有效主审，重试与修复仍共享最多两次物理尝试并计入原子 188 上限。

- 修复 DeepSeek 预算查询与共享快照读写持续泄漏 SQLite 连接并最终耗尽进程文件描述符的问题；补充两个连接边界的正常/异常关闭回归，以及预算库不可用时 `/api/status` 仍返回 200 的故障注入回归。
- 修复合法 DeepSeek 五维响应缺少 `rating` 时默认 `neutral` 导致所有过阈值候选被降为观察、最终无可执行荐股的问题；恢复最终分/本地分/代码固定排序，并保留评级为只读审计。
- 恢复跨交易日最近有效冻结的显式 stale fallback，不再冒充当日快照，同时保留原推荐日期和降级原因。
- 所有非冻结周期任务越过 deadline 时统一进入 `expired`，不污染全局最近错误；全市场特征与历史缓存均在提交前复核 deadline。
- 原始研究 payload 与标准化 observation 缓存改用独立目录并经同一 persistence executor 串行写入；恢复 `bootstrap.py` 唯一组合根。

- 用户问题：运行中“最近错误”偶发显示 `event deadline expired during execution: full_market`。运行库证据显示该事件前后全市场任务连续成功，来源健康无失败；根因是低优先级事件的排队时间计入固定 20 秒 deadline，完成越线后又被通用异常路径误记为系统失败，同时行情服务存在先提交缓存、后检查截止的不一致窗口。修改后，非冻结 deadline 耗尽进入可审计的 `expired` 终态并计入 `events_expired`，不污染全局最近错误；`full_market` 在特征缓存和候选池提交前执行截止校验，历史预载也受同一 deadline 约束并取消未开始任务。真实行情源失败仍保持 `failed`/降级错误语义，11:20/14:50 冻结边界不变。新增组件与集成回归分别覆盖截止后不提交行情特征，以及事件 `expired`、候选不变、`events_failed` 不增加和最近错误不被写入。
- 用户问题：启动后出现 `today freeze unavailable: no current pre-cutoff snapshot`；原因是冷启动回补会把 `today` 与 `tomorrow/d25` 一起补提交，即使当日今日稿件不存在或已超过今日截止窗口。
  修改说明：`initialize()` 现在在进行启动回补前，先校验对应策略当日预截止稿件是否存在且未越过 11:20/14:50 截止窗口；若不满足则跳过该策略回补，避免把“上游缺稿/过期稿件”误报为全局异常。
  结果：无论何时启动，`run.sh` 冷启动再无该条错误污染 `last_error`，tomorrow/d25 仍保留原有回补与冻结逻辑；验证点为 `tests/integration/test_v2_pipeline.py` 中的新增回归。
- 修复跨交易日复用旧冻结 ETag、历史页面沿用锚点涨跌冒充今日行情，以及刷新/关闭字符图标口径不一致的问题；修复评分耗时若写入快照元数据会导致同步/异步与跨运行冻结哈希不确定的问题，耗时现仅进入线程安全运行状态。DeepSeek 调用终态和数据源健康经单写路径持久化，重启后仍可查看 429、超时、延迟、来源年龄及最近冻结哈希，审计不保存密钥、prompt 或完整外部载荷。
- 修复 DeepSeek 缓存因报价 `data_version` 每次变化而失效、跨策略重复请求以及迟到结果可能进入缓存/融合的问题；缓存身份现在绑定结构化特征、证据、风险事实、模型/prompt/schema 和阶段，价格相对变化达到 1% 或量比差达到 0.3 时用十进制精确比较失效。Review 同时修复非重试 4xx 被重复请求、schema 部分返回拖累有效股票、截止后重试预留及崩溃遗留 `running` 状态。
- 修复事件审计 UPSERT 可无条件覆盖状态且同一幂等键缺少有效执行者门的问题：风险/冻结先以 `pending` 原子预留，执行和终态使用 compare-and-set，崩溃遗留高优先级事件可重放，旧配置事件被失败关闭；普通行情满队列时按主体保留最新版本，容量、深度、合并、拒绝和重放均进入状态。Review 同时修复 DeepSeek 异常阻断本地快照、单策略数据失败阻断其他策略、事件嵌套载荷可跨线程改写或写入非有限 JSON 数值、全部数据 worker 同时借用自身队列会自等待、调度启动中断或关闭超时残留，以及停止时冻结写入未完成便返回的风险。
- 修复旧版或迟到候选报价可能覆盖更新全市场行情、分钟刷新失败清空有效尾盘信号、草稿 overlay 已持久化但只读查询忽略、并发全市场重复物理请求及来源故障持续冲击上游的问题；全市场使用 single-flight，连续失败熔断并半开单探针，候选/分钟应用版本门和最近有效缓存，TopK 失败保留现有 overlay。状态接口使用运行时已记录阶段，不在 HTTP 读取中刷新交易日历，并报告全市场/候选/TopK 年龄与 2 倍/3 倍时效分级、SSE 和 today 报价到评分发布延迟。
- 用户问题：d25 市场状态/过热规则仍硬编码，long 五项和财务、公告、质押、减持解禁风险长期缺失，无法区分真实无风险与来源未接入。修改后 d25 评分只消费配置派生乘数；long 使用明确的点时财务与公告公式，成功空源才生成真实 0，任一来源失败、未来、过期、非有限或结构畸形时依赖字段保持 `null`，快照增加 `d25_structured_research_incomplete` 或 `long_research_incomplete` 覆盖降级。
- Review 修复多语义公告只保留一种证据类型可能漏过减持或监管证据门，以及财务/公告冻结摘要不足以离线复算的问题；公告现在可同时保留通用、持股和监管证据，另存去重关键词命中摘要，财务摘要包含 EPS、BPS、三项同比、ROE 和利润字段。新闻与完整研究使用隔离缓存，已知新闻 JSONP 失败不会缩短或污染 d25/long 结构化缓存；四类来源摘要在 15 条证据上限内优先保留，畸形日期、质押超过 100%、单批解禁比例超过 100% 或累计超过 100% 均按来源失败保持 `null`，研究超时配置超过 8 秒时启动失败。
- 用户问题：tomorrow 的 `tail_return_30m` 和 `tail_volume_ratio` 在生产 FeatureBuilder 中固定为 `null`，尾盘结构长期只靠中性默认分；原因是候选链没有分钟数据端口且因子登记只有不可执行的“映射到 0-100”描述。修改后只为 tomorrow 硬过滤候选抓取东方财富分时，严格执行同日点时和连续交易分钟规则；不可用时仍返回本地推荐，但原始值/得分保持 `null` 并标记 `tomorrow_tail_data_incomplete`。
- 修复 5 日收益精确为 0 时量价确认被 `copysign` 错当正方向的问题；0 收益现在固定为中性 50。分钟抓取增加单次 HTTP 尝试、整批截止、短期负缓存和硬容量上限，全源超时不再按 120 只依次拖住本地评分或冻结；健康覆盖只统计收益与量比均可计算的候选，非空但样本不足的序列不再误报 100%。
- Review 修复观察时点之后才接收的分钟被误纳入、非 tomorrow 快照出现无意义尾盘缺失字段，以及只具派生分却缺少冻结原始值仍被计为完整覆盖的问题；tomorrow 现在要求原始收益、原始量比和两项派生分全部存在，d25/long 与全市场预选不构建尾盘字段。
- 澄清“v2 契约更确定”与“v2 收益更高”不是同一结论：v1 研究计划记录的当前版本真实前瞻样本为 0，当前需求又明确移除产品内验证/回测，因此现有文档只能证明 v2 的点时、过滤、融合、冻结和降级边界更可复算，不能提供收益优越性证据。
- 用户问题：today 情绪组件长期把未接入的 `news_sentiment` 与 `evidence_freshness` 当中性缺失值，无法体现真实候选新闻；原因是候选新闻虽已抓取并进入证据列表，FeatureBuilder 仍固定生成 `None`，且无效发布时间会被错误回填为观察时刻。修改后只接受非空标题、带时区且年龄为 0-72 小时的点时新闻，未来、无效和过期记录不参与评分；有证据时输出真实派生值，无证据或来源失败时 API 继续保留 `null`、缺失原因和本地推荐。
- 修复新闻结果先按请求数量截断、再校验点时时间导致前置无效记录挤掉后续有效记录的问题；适配器现在仍只请求至多 10 条，但先逐条拒绝无效/未来记录，再截取调用方所需的有效证据。
- 用户问题：风险阈值仍硬编码且多个缺失风险因子显示为零；修改说明：删除实现内 `0.5/0.75/4.0` 等触发常量，配置启动时验证完整风险表、因子策略范围和叠加组，风险事实按股票、风险、实际值、来源和交易日稳定生成并只扣一次，互斥组取最高、独立风险正常叠加且本地总扣分封顶 25；本地监管 veto 现可直接阻止执行。
- 用户问题：20 日成交额中位数缺失或非有限时仍可进入评分；修改说明：缺失、NaN/Infinity、低于 5000 万分别使用 `missing_liquidity_history`、`invalid_liquidity_history` 和 `insufficient_liquidity` 剔除，5000 万精确通过，缺失实际值在快照中保持 `null`。
- 硬过滤新增未来报价、非有限涨幅/跨源偏差和明显 OHLC 矛盾检查；主板 8.00/8.01、创业板与科创板 16.00/16.01、跨源偏差 0.5/0.5001 和报价年龄边界均使用确定性比较。
- 用户问题：候选池在历史加载前形成且旧候选自我强化；修改说明：冷启动先并发加载候选池三倍大小的行业分层历史集合，再在同一行情批次 `data_version` 内构建候选横截面，预选拒绝核心历史覆盖不足；覆盖率、失败数和版本进入服务健康状态，避免全市场逐股请求拖慢刷新。
- 用户问题：生产分位未截尾且缺失涨幅被当作下跌；修改说明：横截面因子统一执行 2.5%/97.5% 截尾与并列平均秩，NaN/Infinity 保持缺失，市场宽度只使用有效涨幅，冻结数据保存可复算边界和缺失掩码计数。
- 用户问题：补冻结只检查快照发布时间并把 today 错放宽到 30 秒；修改说明：冻结前逐条以边界时间检查报价，today 仅接受 0-20 秒，tomorrow/d25 仅接受 0-30 秒，未来或任一超龄报价会拒绝整版且记录股票与年龄。
- 用户问题：冻结后页面只能看到锚点、重启恢复不校验 committed 损坏项；修改说明：恢复现在校验 staged/committed 哈希和身份/版本，隔离损坏文件并在当前指针失效时回退上一份有效冻结；当前查询对跨交易日回退显式返回 stale、原日期和原因。
- 用户问题：today 观察期高分候选可能错误显示可执行；修改说明：09:30-09:36 观察阶段现在无条件最多为 observe，09:36 后才按 70 分主窗口门槛判断，跨策略或非执行阶段明确 unavailable。
- 用户问题：TopK 用低分候选补满导致推荐不可能为空；修改说明：排名前先应用“动作门槛减 5 分”的最低观察边界，再按最终分、本地分和代码排序并执行单行业最多 3 只，合格候选不足时不降门槛回填。
- 修正问题记录中“冻结 ID 稳定”的歧义：具体 ID 只作为 2026-07-17 当次连续查询证据，同一 committed 快照应保持不漂移，但新交易日、新数据版本或新合法冻结必须生成新 ID。
- 用户问题：11:20 后 today 无数据；修改说明：启动时会将当日截止前 30 秒内最后有效草稿按固定边界补提交，当前查询在截止后只接受 committed 冻结记录，缺少符合时效的截止前草稿时明确不伪造。
- 用户问题：tomorrow/d25 切换时先显示相同或另一波数据；修改说明：共享候选允许股票重合，但 API 和浏览器缓存按策略/日期隔离，过期草稿不再先显示后被新快照替换。
- 修复浏览器将 JSON `null` 经 `Number(null)` 错误显示为 `0.00` 的问题，并把未执行的 DeepSeek 评分、风险扣分和置信覆盖明确标记为“未复核”。
- 快速切换策略 Tab 时不再清空推荐表等待重复网络请求；已加载快照立即从页面内存显示，后台刷新失败时保留缓存快照并显式提示。
- 全市场行情请求增加有界瞬时故障重试：东方财富三个 host 首轮均断连时再尝试一轮，新浪计数或分页遇到连接错误、5xx 或无效 JSON 时最多重试一次，避免单次 `RemoteDisconnected` 或分页 504 直接触发双源降级。
- AKShare 个股新闻路径不再调用无 timeout 的库内裸请求，新闻发布时间统一归一化为 `Asia/Shanghai`；新闻失败仅增加研究源错误计数并回退到结构化行情证据，不阻塞本地推荐。
- 修复模型声明 `veto=true` 即可阻止执行、风险规则 `evidence_ttl_hours` 读取后未生效的问题；错误类型、未来或过期证据均不会进入 DeepSeek 风险扣分与 veto。
- 东方财富、新浪和腾讯实时行情请求显式绕过会导致 TLS EOF 的系统代理，避免本机代理可用但不兼容行情域名时全市场数据持续不可用；DeepSeek 等其他外部请求仍沿用原代理环境。
- 全市场行情源同时不可用时仅捕获明确的可恢复异常，保留既有候选和最近发布快照继续本地评分，并在运行状态中记录降级原因与失败计数；预期降级日志不再输出误导性的完整 traceback。
- 防止本地风险在 68/32 融合中重复扣除；固定向量 `82 - 2 / 100 - 3` 得到 `83.40`。
- 融合保留未舍入本地分精度到最终计算，修正临界值被提前舍入抬高 0.01 的问题。
- 修正 d25 在 20 日涨幅恰好 30% 时应使用 0.85、仅高于 30% 才使用 0.75 的边界。
- 定向报价刷新后再次执行硬过滤，并沿用同版本全市场横截面分位，避免过热/过期股票继续评分和候选内重排漂移。
- DeepSeek 每次重试前重新检查 14:48 截止，完成时间等于截止也按 late 处理；429、超时和成功逐物理请求独立记账。
- 冻结事件改用保留优先级、入队前持久化并支持重启重放；冻结事务同步提交当前发布指针，消除 commit 后 publish 前退出的旧草稿窗口。
- 过期交易日历刷新失败时严格 fail-closed，不再使用超过有效期的日期猜测交易日。
- 配置拒绝 NaN/Infinity，启动时锁定五维键、预算桶、阈值键和 0.68/0.32 融合契约。
- SSE 对超前或过期游标统一要求 resync；慢客户端不会阻塞发布线程。
- 修正桌面表头覆盖首行以及 Tab/SSE 在途请求竞态，迟到响应不再覆盖用户当前策略。
- 补齐原计划只有功能验收、缺少统一性能基线和缓存容量契约的问题：现固定5500只
  全市场行情、三板各120只候选、冷/热轮次、nearest-rank P95、256 MiB项目缓存
  上限、规范JSON字节估算、100 tick增长公式和绝对/相对退化失败条件。

### Removed

- 按用户要求移除股票表上方重复的“正式推荐”标题 DOM、样式及历史/长期动态标题赋值；
  推荐类型仍由策略按钮、摘要、表格内容和空态文案表达，不改变 API 或推荐业务语义。

- Web 荐股展示批次：移除形似按钮的“当前推荐/收盘补算”状态胶囊及独立观察池 DOM、
  样式和事件监听；`close_fallback` 仅作为“已冻结 · 收盘补算”非交互状态显示一次，
  `official/live` API 兼容入口保持不变。

- 本批未删除或改写任何历史快照、当日冻结记录、推荐公式、风险阈值、DeepSeek 预算、
  API 兼容参数或 Web 资源；仅移除测试夹具为缺失策略伪造同日 companion 快照的旧假设。

- 移除桌面的“临时实时/正式当前”双按钮、对应点击状态和重复 CSS；未移除草稿、正式
  冻结、收盘补算、历史、SSE 或显式 `view=official|live` API 能力。

- 从历史预热和远端历史重试集合移除 `unsupported` 股票；硬过滤和 Web API 兼容不变。
  旧 `.runtime/market_data.sqlite3` 仍保持只读，未被新热缓存替代或写回。

- 删除旧 `trader.infra.performance` 合成 runner 及其全部占位操作；CLI 仍保持原有
  `perf-check` 命令和报告入口，不增加网络调用、线程或 `create_app()` 副作用。

- 删除过时的 `docs/plan.md`，以及已完成归并的 `docs/plan_c.md`、
  `docs/plan_sudu.md` 和 `docs/plan_youhua.md`；文档交付契约以失败测试防止已删除计划
  重新成为并行真相源。

- 移除 Web 单文件中的状态、推荐、事件和 SSE 具体实现，以及严格 Ruff 的非零债务基线；
  活动实现不再依赖宽 `**kwargs` 或超限函数来绕过审查。未移除任何推荐策略、行情源、
  DeepSeek、冻结、历史、CLI、API 或桌面能力。

- 本批未移除或修改任何生产行情源、推荐、DeepSeek、P6、API、SSE、Web、配置或测试能力；
  `docs/times.md` 明确禁止以增加隐藏线程、放宽实时门限或削弱确定性换取表面性能。

- 本批未移除任何生产过滤、因子、风险、推荐、API、持久化或 Web 能力；计划明确禁止在
  收益门禁通过前删除 v17 或把影子结果展示为实际收益。

- G5 未移除产品能力、策略、数据源、API、历史、配置、迁移、测试或资源；仅结束
  `plan_youhua` 的未发布状态。后续工程、收益验证和外部运行风险没有被删除或伪装成完成。

- A5 未移除产品、策略、数据源、冻结历史、公共 API、配置项、迁移或桌面资源；移除的是
  P6 两项新增复杂度债务和 C5 raw facts identity 中无关的策略/板块/merge-epoch 分裂因子。
  legacy DeepSeek V3 仍只为冻结/回放兼容保留。

- D5 未移除产品能力、API、策略、持久化 schema、SSE 兼容字段或 Web 资源；仅补齐既有
  resync v2 契约和并发原因分类，并追加终审签字材料。

- B5 未移除产品能力、数据源、策略、公共 schema、配置、API、冻结记录或 Web 资源；终审仅
  收紧 dirty 失效范围、校正既有证据并新增签字材料。

- D4 未删除活动产品能力、业务数据或兼容字段；SSE patch 继续同时携带
  `patch_schema_version=2` 与 `schema_version=2`，Web envelope 保持 v3。

- 本批未删除活动产品代码、业务契约或历史数据；A4 失败探针只在本地复现期间存在，确认
  缺陷后已移除，复现条件和 owner 保留在 A4 报告中。

- 本批未移除产品能力、API、策略、行情源、DeepSeek 能力或 Web 资源；只收敛旧文档口径和
  集成已有 B/C/D 实现包，避免形成第二套 schema 或公共接缝。

- 移除活动流水线对 `SnapshotRepository.publish/latest`、`published/` 草稿 JSON 和
  `published_snapshots` 当前指针的读写；保留旧 SQLite 表仅供完整旧 release 忽略，不再
  作为 v17 事实源。移除 SSE 正常路径对推荐和 overlay 的完整 HTTP 回读。

- 删除行情源、DeepSeek 复核、预算批次/状态/汇总的 5 组旧 mixin 实现文件和继承装配路径，
  不保留兼容别名；本批未删除任何策略、行情能力、DeepSeek 配额、冻结记录、API 或 Web
  资源。

- 删除旧 `application/ports.py` 聚合端口和旧行情异常名，不提供兼容导入别名。

- 从当前 DeepSeek 优化计划中移除 long 的模型评分与请求额度，以及本批历史行情下载、
  60 日/300 配对验证实现；后者保留为需要用户另行确认的延期晋级批次，未删除权威策略
  已有的验证门禁。

- 删除领域根级 `models.py`、`recommendation_models.py`、过滤/评分/融合/排名/研究/风险/
  结果模块及根级 `strategies` 路径，同时删除动态兼容导出和未被使用的 `FilterReason` 类型
  别名；未删除或改变任何策略、行情源、DeepSeek、冻结、API、CLI 或 Web 产品能力。

- 本批未删除任何活动业务能力、策略、API、配置、冻结数据或 Web 资源；只移除未跟踪的退休
  包字节码和文档类型检查缓存。旧工程计划中的性能基线、候选 shadow 和收益验证边界已迁入
  新章节计划，没有作为“重构”名义下的废弃项丢失。

- d25 不再把“不热门”作为独立正向加分组件；过热事实及其既有软风险扣分仍保留，未删除
  行情源、DeepSeek、本地评分、正式冻结或历史推荐能力。本批不引入自动交易、自动调权或
  自动回退。

- 本批未删除产品能力、运行代码、依赖或既有权威契约；计划明确排除把 Lean、OpenBB、
  TradingAgents 和 daily_stock_analysis 引入为运行依赖。

- 删除行情服务的 `MarketFeatureState` 共享可变状态基类、当前报价 mixin 文件，以及九个
  service mixin 的继承/模板方法路径；组件间依赖全部改由构造参数显式传入。本批没有删除
  `MarketDataPort` 对外能力，也没有增加兼容 shim、第二套服务身份或仓库外迁移要求。

- 从推荐 Web 响应和普通详情中移除原始特征、权重、分位与截尾、板块策略/总体/竞争组、
  完整证据与缺失清单、逐字段来源、交易规则、快照内部版本及 DeepSeek 模型指纹、缓存
  Token、挑战者和证据 hash；这些信息未从领域模型或冻结存储删除。

- 移除历史页面仅首读一次、SSE 正常时永不刷新实时收益列的前端限制；没有删除或改写
  任何冻结记录、历史锚点、评分、动作或推荐日期。

- 移除适配器层旧长名称目录及包导入，不保留双命名兼容层，避免同一实现出现两个导入
  真相源；仓库内历史路径记录也统一改用当前 `infra` 名称。

- 移除 P3 候选发现消费 stale-while-revalidate 上一轮全市场快照的路径，以及评分优先于
  行情/候选刷新的旧事件顺序；展示和全源失败降级仍可读取最近有效行情，冻结边界、评分
  公式、DeepSeek 预算、API 与 Web 资源均未放宽或删除。

- 移除活动配置对 `.deepseek_key` 和独立 Tushare token 文件的依赖，并在 120 积分运行档禁止所有需要 2000 积分的 Tushare 物理请求；没有删除旧运行库或历史快照，本地历史种子边界只读且不向旧库写回。

- 从 Web 交付投影移除全市场逐股 `filter_details` 和 `replay_input` 大对象；这些对象仍完整保存在 committed 冻结 JSON/SQLite 身份链并供离线审计、恢复和哈希核验使用，没有删除或改写任何历史数据。

- 移除浏览器对 `previous_trade_date_snapshot` current fallback 的合法身份判断、冻结状态标签和警告提示；不删除昨日冻结文件、历史日期入口或任何审计数据。

- 删除已被两份权威文档吸收的 8 个旧文档文件及空的 `architecture/`、`issues/`、`operations/` 层级。逐批实现历史继续由本 Changelog 和 Git 历史保存，2026-07-17 审计、2026-07-20 外部项目比较、迁移清单和最终验收记录的仍有效结论已归入软件业务设计文档，不再保留会与活动契约竞争的并行副本。

- 本批未删除历史快照、锚点字段、日期接口、SSE、ETag 或任何策略数据；未以主动 HTTP 抓行情填补历史展示，也未修改冻结身份、评分、动作和哈希。

- 本批未增加第七个数据 worker，未放宽候选报价 3 秒总截止，也未隐藏真实腾讯超时、清空推荐、改写冻结快照或让普通历史/研究任务占用紧急 lane。

- 本批未隐藏任何降级提示，也未清除或返还 DeepSeek 已计数的失败预算；未用昨日数据或伪造零值替代缺失的分钟、财务、公告、质押或解禁证据。

- 本批未删除昨日冻结、跨日 stale fallback、SSE、ETag、历史日期或任何策略数据；昨日快照仍只在当日快照尚未就绪时按契约显式降级展示。

- 移除生产全市场“东方财富失败后才请求新浪”的串行回退路径，以及生产组合根中可由普通慢来源占用全部 6 个数据 worker 的通用采集形态；现在 5 个普通来源位与 1 个腾讯紧急位职责隔离，组件独立测试仍可使用受控本地执行器。未删除 v14 候选、评分、固定 68/32 融合、动作阈值、TopK、旧冻结或只读 API 路径。

- 删除与第 26 节“尚未授权生产启用”冲突的活动 `domain/strategies/shadow.py`、策略导出和快照 `shadow_scoring` 元数据；活动代码、API、UI、草稿及冻结 JSON 不再计算或携带候选初值影子排名。未删除历史文档、既有冻结数据、生产评分、预算或只读 API。

- 本批未删除预算审计、历史调用记录、API 字段、策略、冻结数据或运行依赖；数据库不可用只产生显式状态降级，不以清空数据或重建运行库规避错误。
- v10 目标契约不再允许创业板与科创板共享换手、波动、分位或模糊成长板过滤身份，也不再使用 d25 双乘数缩放总分；本批未删除或改写当前 v9 实现。`back1.md` 中的 long 三板扩池、12 套模型、ECDF、机器学习、FDR、收益标签、离线晋级、影子运行及低于 20K Star 的仓库链接均未合入生产契约。
- 从 `docs/need.md` 当前开源参考表删除 Star 低于 20K 的 Qbot、FinRL、myhhub/stock、QUANTAXIS、RQAlpha、WonderTrader、CZSC、Sequoia-X、UZI-Skill 和 QuantsPlaybook 链接；未删除归档历史、活动代码、依赖或策略实现。
- 本批次未删除现有评分、风险、预算、冻结、API、代码或测试；按用户范围不加入离线收益验证、统计晋级或运行时自动调参规则。
- 本批次未删除任何策略、依赖、代码、测试或既有参考项目；只移除 `docs/need.md` 末尾重复且断行损坏的终端表格，其全部 12 个方法引用已去重并入第 2 节。
- 本批次未删除策略、公式、冻结记录或历史兼容路径；仅用本地固定 Lucide sprite 替换页面中的刷新/关闭字符图标，未引入 CDN、移动端分支或新的运行依赖。
- 移除运行配置中重复的 DeepSeek 置信覆盖阈值；该阈值与最少已知维度继续只由版本化策略配置定义，避免运行配置和策略配置产生两个真相源。
- 移除内部模块路径 `trader.application.snapshot_lifecycle`；该路径不是公共 API，未保留会掩盖半迁移状态的兼容转发文件。
- 移除生产行情服务按调用临时创建 history/research/intraday/Eastmoney 分页线程池的路径，以及可绕过 CAS 的 `append_event()` 无条件事件写入口；组件脱离运行时独立调用时仍使用同一有界执行器适配层完成局部回收。本批不修改评分公式、冻结边界或 Web 视觉布局。
- 移除 d25 市场状态与过热乘数的实现内阈值表，以及 long 五项、财务恶化、公告、质押和减持解禁在生产特征链中的固定缺失占位路径；未删除固定 long 名单、人工目标价或只读观察边界。
- 移除生产特征构建中两个 tomorrow 尾盘因子的固定 `None` 占位路径；DeepSeek 功能未在第 12 或第 13 节中删除或扩展。
- 本批次未删除任何策略、配置、代码、测试或归档资料，也未用历史口径覆盖当前需求。
- 移除 AKShare 新闻缺失或非法时间回填为“当前时刻”的伪新鲜证据路径；本批次未删除相邻策略、API 或 Web 功能。
- 删除活动 `stock_analyzer` 包、根 `app.py`、旧 static/templates、旧配置和重复 requirements。
- 删除验证、回测、自动调参、预测、paper trading、OOS/实验功能及其 Web 路由、资源和旧测试。
- 删除根 `analysis`、`experiments` 活动产物和旧依赖指纹脚本；有保留价值的资料仅归档，不进入 wheel。
- 本批未删除或修改活动策略、风险、融合、冻结、API、UI、配置和代码；计划明确不引入
  第二个数据库、缓存框架、benchmark依赖、移动端分支或用性能优化放宽实时性门槛。

### Verification

- 顶部信息区与紧凑列表布局通过 Web 资源/主表契约测试和真实 Firefox/geckodriver 验收；
  用户提供的完整长收盘补算降级串与长最近错误在 1280x720、1440x900、1920x1080 下
  均保持两栏 52px、各自可滚动，摘要紧邻策略行、策略行紧邻表头，页面无横向溢出、
  浏览器错误或 resync。24 个 SSE patch 的 patch-to-paint P95 为 23ms（预算 100ms）。
  对实际 `127.0.0.1:5000` 服务重启前后分别取 HTML 与 1440x900 截图，确认混合缓存时
  放大的“正式推荐”存在，重启加载最终模板后该节点消失且行序正确。`make lint`、
  172 个源码文件 mypy、除一条既有盘后恢复用例外的全量 pytest、`make package` 通过；
  仓库外 wheel 可从安装目录导入、执行 `trader-cli --help` 与绝对配置校验、读取模板与
  10 项 CSS/JavaScript/SVG 资源，并通过 `pip check`。

- Web 荐股展示批次：`make format-check`、`make lint`、`make type-check`、`make test`
  （784 项）和 `make package` 通过；38 项 Web/API/SSE 定向测试与 Node 策略日期状态机
  通过。Firefox/geckodriver 离线真实页面验证 1280x720、1440x900、1920x1080 均无页面级
  横向溢出或浏览器错误，24 个 SSE patch 的 patch-to-paint P95 为 19ms（预算 100ms）。
  仓库外安装 wheel 后确认 `selection.js`、模板、CSS、其他 JavaScript 与图标均可读取，
  `trader` 从安装目录导入，`trader-cli --help`、绝对配置 `validate-config` 和
  `pip check` 通过。

- 本批先以本机运行库确认旧 v2 有 5 份 committed 历史但三策略日期交集为空，v17 当日
  三策略均已 committed 但各为 0 只，且 qfq 热缓存随后达到三板各 119 只。新增失败回归
  分别复现部分策略历史被隐藏、历史未就绪仍固化收盘结果；修复后验证 resident 与 cold
  部分策略日期、日期级 single-flight、冻结 current pin、历史 API、收盘退避重试，以及
  预热完成后三策略均实际产生股票且不含三板样本/可靠度不足降级。相关 Web、P6 和流水线
  完整测试通过；`make format-check`、`make lint`、172 个源码文件 mypy、全量 pytest
  和 `make package` 通过。仓库外 wheel 从隔离目标导入，`trader-cli validate-config`、
  `pip check` 与模板、4 个 CSS、2 个 JavaScript、2 个 SVG 共 9 项资源通过。
  Firefox/geckodriver 应用 24 个实时 patch，零 resync、零页面错误，patch-to-paint P95
  为 61ms；1280x720、1440x900、1920x1080 均有有效页面且无页面级横向溢出。

- 自动当前视图覆盖冻结前草稿、冻结后正式结果、冻结失败保留同日草稿、上一交易日拒绝、
  空 `not_ready`、历史和显式 API 兼容；Node 状态机覆盖 current 模式的实时 patch、
  草稿到正式切换和冻结后迟到草稿拒绝。`make format-check/lint/type-check/test/package`
  全部通过，仓库外 wheel 的包导入、CLI、模板/CSS/JavaScript/SVG 与 `pip check` 通过。
  Firefox/geckodriver 应用 24 个实时 patch，零 resync、零页面错误，patch-to-paint
  P95 为 19ms；1280x720、1440x900、1920x1080 均有有效页面且无页面级横向溢出。

- 本批先用四项失败回归复现三板配额不足、分钟请求错误负缓存、评分前未刷新尾盘缓存和
  DeepSeek 降级误报；并发回归连续三轮及包含原 deadline 语义的扩展回归通过。另以
  同 TTL 重启零远端调用、过期刷新失败回退、容量淘汰/损坏库不中断三类回归验证独立
  历史热缓存，并覆盖同步 `run_once`、异步评分事件和录制影子适配器。完整
  `make format-check`、`make lint`、`make type-check`、`make test` 和 `make package`
  通过；37 项架构、`create_app()` 无副作用、固定融合 `83.40`、188 原子预算、SSE
  游标/慢客户端、冻结和哈希专项通过。仓库外 wheel 安装、包导入、`trader-cli --help`
  及模板、4 个 CSS、2 个 JavaScript、2 个 SVG 共 9 项资源读取通过。Firefox 无头真实
  浏览器在 1280x720、1440x900 和 1920x1080 均有有效页面、无浏览器错误和页面级横向
  溢出；24 次实时补丁均应用，patch-to-paint p95 为 39ms。

- T1 定向验证通过延迟采集、配置、真实性能入口、行情网关、架构、Web/API、流水线和
  JavaScript 状态机测试；mypy 覆盖 172 个生产文件。真实浏览器应用 24 个 SSE patch，
  最终 patch-to-paint P95 83ms（预算 100ms），1280x720、1440x900、1920x1080 均无
  页面级横向溢出。`make format-check/lint/type-check/test/package` 全部通过；仓库外
  全新虚拟环境成功安装最终 wheel，包导入、CLI、配置、模板、CSS、JavaScript、双图标
  和 `pip check` 均通过。

- 本批已逐项对照四份旧计划、两份权威文档、活动 DeepSeek/列式/P6 实现、`config/v2`
  和 G5 报告；32 项定向契约测试与完整 `make format-check`、`make lint`、
  `make type-check`、`make test`、`make package` 均通过，严格重构债务为零。仓库外
  wheel 安装、顶层包导入、模板/CSS/JavaScript/SVG 资源读取、`trader-cli --help` 和
  `pip check` 通过；首次 `--no-deps` CLI 探测因验收环境按设计没有 Polars 而失败，随后
  使用项目已锁定依赖环境加载同一 wheel 安装目标复验通过。`git diff --check` 通过。

- 任务 A 的隔离候选树通过 `make format-check`、`make lint`（严格诊断为零）、
  `make type-check`、`make test` 和 `make package`；架构/`create_app()` 无副作用、固定
  融合 `83.40`、188 原子预算、冻结恢复、哈希、SSE 游标/慢客户端均包含在全量回归。
  仓库外安装 wheel 后从 `site-packages` 导入包、执行 `trader-cli --help`、读取模板、
  4 CSS、2 JavaScript、2 SVG 共 9 项资源并通过 `pip check`。Firefox 在 1280x720、
  1440x900、1920x1080 精确内容视口均显示 18 个唯一代码，无白屏、页面横向溢出、关键
  重叠、页面脚本错误或更新后布局跳动，详情抽屉 3 分区完整位于视口。

- `docs/times.md` 已核对非权威边界、两份权威文档链接、现有性能证据、固定融合/冻结/
  188 次预算和桌面范围。以当前上游叠加本批 3 个文件的隔离快照执行五项 make 门禁，
  235 文件格式、严格 Ruff 债务、164 个源码文件 mypy 和完整 pytest 均通过；sdist/wheel
  构建成功，仓库外安装可导入、执行 CLI、读取 9 项 Web 资源且 `pip check` 无破损依赖。

- `docs/strage.md` 已按非权威计划边界复核完整链路、固定融合、冻结、预算和不承诺收益
  约束；文档契约 3 项及 `make format-check`、`make lint`、`make type-check`、`make test`、
  `make package` 全部通过。仓库外 wheel 可导入包、读取 6 项 Web 资源并执行
  `trader-cli --help`；`git diff --check` 未发现空白错误。

- G5 复跑 A4 同进程压力，逻辑 `205,468,511 B`、峰值 RSS `385,851,392 B` 均通过；
  正式 v17 `perf-check --suite all` 16 项通过且零网络。B4 相对 CPU 首轮改善
  `16.842%` 未达 20%，相同身份无并行负载重跑为 `26.854%` 并通过，业务哈希、绝对时延、
  内存始终一致；v16 三板四项绝对预算通过且未宣称 CPU 加速。五项 make、仓库外 wheel、
  `pip check`、CLI/配置/9 项资源和 Firefox 三档由本批最终门禁复核。

- A5 定向复验架构、app factory、固定融合 `83.40`、冻结/P6、SQLite 迁移、DeepSeek
  预算/C2-C5、SSE/Web 与端到端；P6 重构后的 83 项及 DeepSeek/P6/Web 联合集 148 项通过。
  Firefox 152.0.4 在 1280x720、1440x900、1920x1080 均显示 18 行，无页面横向溢出、
  关键重叠或浏览器错误，抽屉三分区完整；两次 patch 为零完整推荐 GET、成功应用 2 次。
  五项 make、仓库外 wheel 导入/CLI/资源和最终 `HEAD == @{upstream}` 由本批最终门禁核对。

- D5 的 publisher/P6/Web API/app factory 53 项、P6/冻结恢复定向集成 9 项、D4 性能、
  架构、固定融合 `83.40`、188 并发预算及 Node 状态机均通过。Firefox 152 在
  1280x720、1440x900、1920x1080 精确内容视口均显示 18 行，无横向溢出、关键重叠或页面
  错误，三分区抽屉位于视口内；两次 patch 为零完整 GET，显式 resync 产生一次 ETag GET。
  五项 make 门禁全部通过；仓库外 wheel 可导入、执行 CLI 并读取 9 项 Web 资源。

- G4 发布批次复验五项 make 门禁、固定 `83.40`、C4/D4 定向回归、v17 16 项性能、
  B4 5500 行/360 候选/100 tick、A4 同进程内存和 Firefox 152.0.4 三档桌面；最终 B4
  columnar 改善 `32.404%`，标准化/合并/canonical P95 为
  `169.247/675.536/1219.953ms`。综合逻辑字节 `205,468,511B`、当前/峰值 RSS
  `370,069,504/387,112,960B`、USS `312,655,872B`、Polars `1,282,816B`，均在上限内。
  仓库外 wheel 的包导入、CLI、配置、9 项资源和 `pip check` 通过；三档均为 18 个唯一代码，
  无白屏、横向溢出、关键重叠或页面错误，增量 patch 不产生完整 GET。

- B5 定向 B 域 171 项测试通过；最终固定行情复跑的 scalar/columnar process-CPU P95 为
  `1545.992/1172.998ms`，改善 `24.126%`，标准化/两源合并/统一快照 P95 为
  `140.455/545.278/1314.793ms`。三板预选/评分/三板三策略/稳定选择 P95 为
  `51.411/9.030/519.941/3.700ms`。B-owned 100 tick 逻辑字节 `29,661,328B`、增长
  `0.0%`、峰值 RSS `273,195,008B`；集成并存场景逻辑字节 `205,468,511B`、峰值 RSS
  `387,186,688B`，均通过固定上限，业务和 canonical SHA-256 保持一致。隔离重建的精确
  `HEAD + B5 staged diff` 通过 format、lint、164 源文件 mypy、738 项 pytest 和 package；
  仓库外 wheel 可导入、执行 CLI/配置校验、读取 9 项 Web 资源且 `pip check` 无断裂依赖。
  并行 A 侧补齐 G4 报告后，共享工作树同样通过五项 make 与仓库外 wheel 验收。

- B4 固定验收 runner 通过：标准化+观察值构造+两源合并 process-CPU P95 相对 scalar 改善
  `27.22%`；标准化/两源合并/统一快照 wall P95 为 `134.059/586.035/1130.823ms`，
  100 tick 逻辑缓存 `29,661,328B`、分配增长 `0.0%`、峰值 RSS `288,051,200B`，均在门禁内。
  B 域定向单元 `48` 项、行情组件 `122` 项、Ruff、mypy 和严格债务基线均通过；完整证据见
  `tests/fixtures/market_data/youhua_b4/report_to_a.md`。

- D4 固定门禁记录 P6→SSE 入队 P95 `4.357ms`、权威 SSE 年龄 `0.000s`、当前/驻留/ETag/日期/状态
  API P95 `2.382/1.808/0.797/1.352/1.758ms`；单股 patch `1,133B` 对完整响应 `10,952B`，节省
  `89.655%`。Firefox 152 三档精确内容视口用 10 条正式 + 8 条观察、长错误、详情抽屉和
  实际 SSE 验收，无白屏、关键重叠、页面横向溢出或页面错误；有效 patch 的完整 GET 增量
  为 0、布局位移 0px，显式 resync 只产生一次条件 GET 并命中 304。最终稳定共享树的
  `make format-check/lint/type-check/test/package` 全部通过；仓库外安装 wheel 后可导入包、
  执行 `trader-cli --help`，并读取模板、CSS、JavaScript 和图标资源。

- A4 最终稳定树验证：F01/F04 精确失败用例、pipeline/P6/publisher 定向回归、C4 七项及完整 pytest
  通过；`make format-check/lint/type-check/test/package` 全部通过，mypy 检查 164 个活动源码，
  严格债务保持基线。正式 `perf-check --suite all` 16 项通过且零网络；B4 最终 columnar 改善
  `35.544%`，v15/v16/D4 专业预算全部通过。A4 同进程六池约 70%、双快照/列式 epoch、
  DeepSeek 最大批次与 P6/SSE 压力保持强引用并存时，逻辑字节
  `205,468,511B <= 260,046,848B`，峰值 RSS `387,452,928B <= 402,653,184B`，USS
  `358,887,424B`。仓库外最终 wheel 可导入安装目标、执行 CLI/配置校验、读取 9 项 Web 资源，
  `pip check` 通过；D4 Firefox 三档桌面证据通过。

- G3 门禁复核批次验证：读取阶段 3 计划、A3/B3/C3/D3 报告，确认四方均已提交
  `ready_for_gate=yes` 交接证据并发布 G3；定向契约测试
  `tests/contract/test_delivery_contract.py tests/contract/test_youhua_contract_base.py tests/component/test_youhua_deepseek_c3.py`
  覆盖 G3 发布状态、docs 报告白名单和 C3 raw facts 缓存复验。`make format-check`、`make lint`、
  `make type-check`、`make test` 和 `make package` 通过，生成物已清理。
  仓库外 wheel 安装到 `/tmp/trader-wheel-g3` 后可导入 `trader`、读取 Web 模板/静态资源，
  并可执行 `trader.entrypoints.cli --help`。

- A3 集成批次验证：定向集成测试
  `tests/unit/test_v17_columnar_changes.py tests/unit/test_v17_columnar_provider_adapter.py tests/unit/test_v2_market_data_normalize.py tests/unit/test_v2_market_data_merge.py tests/unit/test_v2_market_data_router.py tests/unit/application/test_candidate_features.py tests/unit/test_v2_deepseek_base.py tests/component/test_v2_deepseek.py tests/component/test_v2_deepseek_v4.py tests/component/test_youhua_deepseek_c2.py tests/unit/application/test_published_snapshots.py tests/unit/application/test_publisher.py tests/contract/test_v2_web_api.py tests/contract/test_v2_app_factory.py tests/contract/test_youhua_a2_public_skeleton.py tests/contract/test_youhua_contract_base.py`
  通过 183 项；`make format-check`、`make lint`、`make type-check`、`make test`、
  `make package` 和 `git diff --check` 均通过。仓库外 wheel 安装到 `/tmp/trader-wheel-a3`
  后可导入 `trader`、读取 Web 模板/静态资源，并可执行 `trader.entrypoints.cli --help`。

- G2 发布批次验证：仅读取 B2/C2/D2 报告和 B2 fixture，确认 A2/B2/C2/D2 均为
  `ready_for_gate=yes`；定向契约测试
  `tests/contract/test_delivery_contract.py tests/contract/test_youhua_contract_base.py` 通过 8 项，
  `git diff --check` 通过。

- 2026-07-23 G2 复核批次验证：仅读取 B2/C2/D2 报告，确认 C2 标准字段已补齐、B2 仍为
  `ready_for_gate=no`；定向契约测试
  `tests/contract/test_delivery_contract.py tests/contract/test_youhua_contract_base.py` 通过 8 项，
  `git diff --check` 通过。

- G2 门禁复核批次验证：仅读取 B2、C2、D2 交接报告和 fixture 路径，未执行 B/C/D 内部算法；
  定向文档契约测试 `tests/contract/test_delivery_contract.py tests/contract/test_youhua_contract_base.py`
  通过 8 项，`git diff --check` 通过。当前判定证据是 B2 报告明确 `ready_for_gate=no`、
  C2 报告缺少标准字段、D2 报告 `ready_for_gate yes`，因此共同门禁不满足。

- A2 公共骨架批次验证：定向契约与配置测试
  `tests/contract/test_youhua_a2_public_skeleton.py tests/contract/test_v2_architecture.py tests/unit/test_v2_settings.py`
  通过；扩展文档契约后 5 个定向文件共 71 项通过，覆盖公共 schema/version、long 零复核、
  DeepSeek V4 facts 证据边界、P6
  projection/overlay CAS、HTTP/DeepSeek 零副作用替身、248 MiB 逻辑缓存拒绝和 384 MiB
  进程峰值拒绝。A2 范围 Ruff format/check 通过；`make type-check` 通过 162 个源码文件；
  `make package` 沙箱内因构建依赖联网失败，提升权限后通过并清理生成物。全局
  `make format-check`/`make lint` 被非 A2 的 DeepSeek/C2 未提交文件格式与导入问题阻断；
  全局 `make test` 运行完成，剩余 2 个既有失败：bootstrap duplicate start 和 final
  candidate cadence 计数。

- G1 发布批次验证：复核 B1、C1、D1 三份标准报告均包含 `ready_for_gate=yes`；确认
  `HEAD == @{upstream}` 后发布 `CONTRACT_BASE`。`make format-check`、定向契约测试
  `tests/contract/test_delivery_contract.py tests/contract/test_youhua_contract_base.py`、
  `git diff --check` 和 `make package` 通过；`make type-check` 通过。全局 `make lint`
  仍失败于既有严格债务计数漂移，全局 `make test` 仍失败 5 项，失败面与 A1 记录一致。

- A1 基线：`make format-check` 通过；`make lint` 在既有严格债务计数漂移处失败；
  `make type-check` 通过；`make test` 在 A1 修正文档白名单后仍有 5 个既有失败；`make package` 沙箱内因
  构建依赖联网失败，提升权限后通过。离线 `perf-check --suite all`、v15 market-data
  runner、v16 board-scoring runner 均通过，且无外部网络调用。1280x720、1440x900 和
  1920x1080 无头 Chrome 截图非白屏；无运行态页面显示 `not_ready`，SSE 因未注入
  publisher 返回 503。完整证据见 `docs/reports/youhua-a1-baseline.md`。

- 本批新增严格配置、列式 dirty set、P6 完整日期预热、日期 single-flight、检查点
  hash/consume、SQLite schema v8、SSE patch 和慢客户端自动回归。首轮 145 个相关用例
  发现 8 个旧契约或实现问题，修复后 8/8 定向复测通过；最终完整质量、打包、wheel、性能
  与桌面门禁结果在本批提交前继续更新。

- 2.4 的 659 项完整 pytest（验收时暂时隔离并随后原样恢复与本批无关的未跟踪
  `docs/plan_youhua.md`）、213 文件 Ruff format、Ruff lint/严格债务、154 个源码文件
  mypy、源码/架构 AST、`create_app()` 无副作用、固定融合向量 83.40、预算并发、冻结恢复、
  SSE 游标/慢客户端、哈希一致性和 `make package` 均通过；PLR0913 从 58 降至 55，活动
  Python 单文件均小于 800 行。固定性能负载通过：v15 为 5500 行行情/360 候选且三个 P95
  均低于预算，v16 为三板各 120 候选且四个 P95 均低于预算。仓库外 wheel 安装、CLI、
  包内模板/CSS/JavaScript/图标以及 1280x720、1440x900、1920x1080 桌面渲染亦纳入验收。

- 本批逐项核对 P1-P6、256 MiB 初始目标、性能 P95、只读 Web、SSE、冻结不可变、固定融合
  和 DeepSeek 预算/截止契约；验证计划明确要求 scalar/columnar 逐字段等价、最终分/动作/
  TopK/哈希完全一致、Python 3.10-3.14 wheel 可安装，以及三档桌面实际渲染。本文档批次
  通过 Markdown 链接、禁词/边界检索、`git diff --check` 和适用仓库文档契约检查。

- 2.3 通过架构 AST、154 个源码文件 mypy 和完整 pytest 回归；严格债务从 145 降至 142
  （N818 7→5、PLR0913 59→58）。

- 本批逐节核对 `AGENTS.md`、`docs/software-business-design.md` 和
  `docs/recommendation-strategy.md` 的固定融合、188 次全局上限、冻结时间、纯本地收盘
  补算及验证门禁；当前文档契约 3 项、`git diff --check` 和 `make package` 通过。起始
  `HEAD=8c81db9` 的隔离源码通过 Ruff format/check、严格债务门禁、146 个源码文件 mypy、
  完整 pytest、无隔离构建及仓库外 wheel 的包导入、CLI 和静态资源验收。当前工作树的
  format/lint/mypy/test 被本批开始后出现的无关 `application/ports` 重构中间态阻断，
  本批未修改、暂存或掩盖该并发变更。

- 本章最终 Review 已通过 Ruff format/lint、领域严格告警归零、146 个源码文件 mypy 和完整
  651 项 pytest；仅保留 10 条既有未知测试模型名警告。固定融合向量仍为 `83.40`。以起始
  提交 `a90eea` 和当前实现分别运行同一整日冻结
  fixture，today/tomorrow/d25 三份冻结 JSON 与四份发布 JSON 的相对路径、SHA-256 和记录数
  逐项完全一致。固定 5,500 行行情性能门禁通过（标准化/合并/统一快照 P95 分别为
  97.151/511.747/749.581ms），360 候选板内评分门禁通过（预选/评分/全局选择/三板墙钟
  P95 分别为 17.533/2.232/1.790/207.246ms）。sdist/wheel 隔离构建通过；wheel 从仓库外
  前缀导入，新领域请求类型、`trader-cli --help` 及模板、4 个 CSS、2 个 JavaScript、2 个
  SVG 共 9 项资源通过。1280x720、1440x900、1920x1080 三档 Chrome 实际截图复核无白屏、
  重叠、页面级横向溢出或明显布局跳动。

- 本章 Review 通过 Ruff format/lint、严格债务精确基线、140 个源码文件 mypy、完整 649 项
  pytest 和架构/无副作用应用工厂定向契约；pytest 仅保留 10 条既有未知测试模型名警告。
  sdist/wheel 隔离构建通过；wheel 从仓库外前缀导入，两个 CLI、`pip check`、模板、4 个
  CSS、2 个 JavaScript 和 2 个 SVG 共 9 项包资源通过。1280x720、1440x900、1920x1080
  三档 Chrome 实际截图复核无白屏、重叠、页面级横向溢出或明显布局跳动。

- v17 最终 Review 通过 Ruff format/lint、140 个源码文件 mypy、完整 647 项 pytest 和
  sdist/wheel 隔离构建；pytest 仅保留 10 条既有未知测试模型名警告。79 项定向回归覆盖
  下行保护、入场点时性、正式/观察分池、Web 加法投影、结果结算及冻结哈希不变；固定融合
  `83.40` 和旧 v16 回放由全量契约继续通过。wheel 从仓库外前缀导入，`trader-cli --help`
  及模板、4 个 CSS、2 个 JavaScript、图标资源均通过。1280x720、1440x900、1920x1080
  三档桌面 DOM 复核无白屏、头部重叠或页面级横向溢出，观察表仅保留内部横向滚动。

- 本批计划文档通过文档拓扑契约、Markdown diff 审查和空白检查；完整质量、测试、构建及
  仓库外 wheel 安装结果在提交前复核。本批没有修改 Web 资源或运行布局，三档桌面渲染
  行为不受影响。

- 以本批起始提交 `d0614bc` 加且仅加类级组合重构 diff 的隔离副本执行：Ruff format/lint、
  137 个源码文件 mypy、完整 627 项 pytest、sdist/wheel 构建全部通过，pytest 仅保留 10 条
  既有未知测试模型名警告；仓库外安装后可从独立前缀导入，两个 CLI、配置校验、模板、
  4 个 CSS、2 个 JavaScript、2 个 SVG 和 `pip check` 均通过。架构契约同时校验协调类
  零继承、旧状态文件消失、六个核心组件存在且各自锁身份不同；行情组件定向回归全部通过。
  v15 固定快照哈希保持
  `234b923cb17d1979365892791f38545598ae2d25f0cbe14817980a3080c3329b`，最终隔离复测的
  规范化/合并/快照 P95 为 151.621/800.098/1,378.152ms，均在 800/1,000/1,500ms 预算内；
  v16 固定评分复测亦在预算内。本批未改 Web 资源和布局，因此未重复三档桌面截图。

- v15 固定录制负载最终复测通过：两源合并 P95 860.711ms/1,000ms、统一快照 P95
  1,090.479ms/1,500ms，规范化 P95 157.525ms；固定完整快照哈希继续为
  `234b923cb17d1979365892791f38545598ae2d25f0cbe14817980a3080c3329b`，内存估算继续为
  28,378,644 字节。新增规范 JSON 字节和代表性合并快照哈希回归；v16 三板评分性能复测仍
  全部通过。`make format-check/lint/type-check/test/package` 全部通过（mypy 139 个源码文件，
  pytest 仅保留 10 条既有未知测试模型名警告），sdist/wheel 隔离构建成功。最终 wheel 从
  仓库外前缀导入，两个 CLI、配置校验、模板、4 个 CSS、2 个 JavaScript、2 个 SVG 和
  `pip check` 均通过。本批未改 Web 资源或 UI 布局，因此未重复三档桌面截图。

- 收盘恢复回归覆盖同进程 P6 身份保留与收盘价替换、冷启动三策略本地重建、数据库优先、
  仅补缺失策略、三板不完整不落盘并重试，以及冻结 JSON 往返、确定性回放、正式查询
  `ready` 和 Web `close_fallback` 标识。`make format-check/lint/type-check/test/package` 全部
  通过（mypy 139 个源码文件、pytest 624 项；仅保留 10 条既有未知测试模型名警告），
  sdist/wheel 构建成功。最终 wheel 在仓库外目录安装并从独立 site-packages 导入，
  `trader-cli --help`、模板、CSS、JavaScript 和图标资源通过。Firefox 152 在精确
  1280x720、1440x900、1920x1080 视口均完整加载 `dashboard.js?v=12`，无白屏、损坏图片、
  Header/主区或工作区块重叠、页面级横向溢出。

- 行数门禁架构契约定向回归通过；为避免把并行中的用户业务修改混入本批，使用已推送
  `08c4d43` 基线叠加本批 3 个文件的隔离副本完成 Ruff format/lint、138 个源码文件
  mypy、完整 618 项 pytest、sdist/wheel 构建。仓库外 wheel 的包导入、`trader-cli`、
  9 项 Web 资源与 `pip check` 通过；当前仓库 editable 安装已恢复并指向活动源码。

- v3 ready/not_ready/error、当前/历史、正式/临时实时、overlay、精简复核和风险去重契约
  通过；JavaScript 语法检查、Ruff format/lint、138 个源码文件 mypy、完整 pytest 与冻结
  持久化关联回归通过。sdist/wheel 构建成功；wheel 在仓库外独立前缀完成导入、v3 schema、
  `trader-cli`/`trader-server`、模板、CSS、JavaScript、图标和 `pip check` 验收。

- 本批定向验证已通过 cadence 冻结后冷启动单次恢复、恢复任务不评分/不发布、正式接口
  继续 `not_ready`、`view=live` 返回同日草稿、非法视图拒绝、历史实时行刷新和包内静态
  资源契约；`make format-check/lint/type-check/test/package` 通过（mypy 138 个源码文件、
  pytest 617 项，wheel/sdist 构建成功）。仓库外 wheel 的 `trader` 导入、`trader-cli`、
  `validate-config`、模板/CSS/JavaScript/图标资源和 `pip check` 通过。Firefox 152 在
  1280x720、1440x900、1920x1080 完成截图与 DOM 尺寸检查，均无白屏、重叠或页面级横向
  溢出，“临时实时”入口可见。

- 失败先行回归覆盖实时核心任务 FIFO、TopK 快通道、候选事件不等待分钟历史、依赖排队
  窗口与实际执行预算分离、历史预热单批在途、上海日期边界和重复系统启动生命周期；
  `make format-check`、Ruff、138 个源码模块 mypy、完整 612 项 pytest、sdist/wheel 构建
  全部通过，仅保留 10 条既有未知测试模型名警告。最终 wheel 在仓库外目标目录导入，
  `trader-cli --help`、`validate-config` 和 9 项 Web 资源通过。真实服务确认修复前已恢复
  tomorrow/d25 的 74/81 个候选与 4/2 条草稿，但旧进程累计 139 个事件过期并错过
  14:50 合格检查点；最终进程启动正常、Tushare 精确显示 `permission_denied`。

- 失败先行架构测试已复现新目录缺失，并在迁移后确认 `src/trader/infra` 可导入、旧目录
  和旧活动导入均不存在；最新上游叠加本批后的 188 个源码/测试文件格式检查、Ruff、138 个
  源码模块 mypy、完整 612 项 pytest、sdist/wheel 构建均通过，仅保留 10 条既有未知测试模型名警告。
  仓库外安装 wheel 后确认从隔离包路径导入 `trader.infra`、旧包不可发现、
  `trader-cli --help`、配置校验、9 项 Web 资源及活动环境 `pip check` 通过。本批不修改 Web
  资源或布局，桌面三档沿用同一已验收资源基线。

- 失败先行回归分别复现并修复周期全市场 `force=False`、历史预热空筛选清除既有候选、
  `SCORE < CANDIDATE_QUOTES < MARKET_QUOTES` 的逆依赖顺序；`make format-check`、Ruff、
  138 个源码模块 mypy、完整 600 项 pytest、sdist/wheel 构建均通过，pytest 仅保留 10 条
  既有未知测试模型名警告。仓库外 `--target` 安装 wheel 后确认从隔离包路径导入、
  `trader-cli --help`、`validate-config`、9 项模板/静态资源和活动环境 `pip check` 通过；
  完整依赖独立 venv 安装受宿主磁盘配额阻断，未伪报通过。本批未修改 Web 资源，桌面三档
  布局沿用同一已验收资源基线。

- 失败先行回归覆盖双凭据权限/优先级、120 积分能力门、`daily` 多代码单请求、HTTPS 直连且不继承环境代理、批内部分成功、实时任务不等待历史、接收时间发现与原始来源时间保留、本地种子只读及有界并行分页。2026-07-22 12:08-12:12 真实服务首轮在 8.1 秒内由 Sina 取得 5,529 行全市场行情，事件成功完成；本地种子首批计划/完成 30/30、失败 0，历史覆盖达到 31/360，后续 34/360。Tushare 请求实际到达官方接口并被分类为 `quota_or_rate_limit`，未泄露 Token；Web 对尚未形成当日冻结快照正确返回 `not_ready`，未用昨日推荐伪装实时结果。`make format-check/lint/type-check/test/package` 全部通过（138 个源码模块、597 项 pytest，只有 10 条既有未知测试模型名警告）；最终 wheel 在仓库外安装后通过隔离包路径导入、配置校验、CLI、9 项模板/静态资源读取，活动环境 `pip check` 无损坏依赖。

- 本地真实运行库预热 5 个历史视图耗时 3469.498ms，发生在 HTTP 接收前；同一工作树 Flask 热请求实测当前空响应 3.694ms/572B、tomorrow 2026-07-20 历史 3.030ms/92,923B、d25 同日历史 3.832ms/107,395B，均低于 200ms 当前/驻留历史预算，且响应 `filter_details` 为 0。现场旧进程对相同有效历史的修复前 TTFB 为 0.942-1.800 秒、响应最大 350,970B；有效样例同时返回实时“今日涨幅”和独立“锚点至今”。完整门禁、wheel 与提交后运行态复验见本批最终记录。

- 用户补充反馈的失败先行回归已复现并转绿：当前查询不会复用昨日快照或生成 current ETag，页面包不再包含上一交易日 fallback 提示，历史报价读取在 P2 特征提交前可命中当日规范行情。`make format-check`、`make lint`、134 个源码模块 mypy、完整 584 项 pytest 与隔离 `make package` 通过，pytest 仅保留 10 条既有未知测试模型名 RuntimeWarning；最终 wheel 在仓库外覆盖安装后通过 `pip check`、配置校验、v9 缓存版本及模板、4 个 CSS、2 个 JavaScript、2 个 SVG 共 9 项资源读取。HTML/CSS 布局未变，桌面三档沿用同布局资源已通过基线。

- 本批双文档结构契约、旧路径残留扫描、相对链接、`git diff --check` 和本批 Python 文件 Ruff format/lint 通过；全量 Ruff lint、134 个源码文件 mypy、584 个 pytest、sdist/wheel 构建通过。wheel 在仓库外临时虚拟环境安装后可从隔离路径导入，模板、CSS、两个 JavaScript 和两个 SVG 资源齐全，`trader-cli --help`、`validate-config` 与 `pip check` 通过。

- 历史行情修复的 Web API 契约测试和行情索引组件测试通过；`make format-check`、`make lint`、134 个源码模块 mypy、完整 583 项 pytest 与 `make package` 全部通过，pytest 仅保留 10 条既有未知测试模型名 RuntimeWarning。仓库外隔离安装 wheel 后通过包导入、配置校验、CLI、`pip check` 及模板、4 个 CSS、2 个 JavaScript、2 个 SVG 共 9 项资源读取；本批未修改 HTML/CSS/JavaScript，桌面布局沿用同资源三档已通过基线。

- 批次二 195 项局部矩阵和完整 580 项 pytest 通过，保留 10 条既有未知测试模型名 RuntimeWarning；`make format-check`、`make lint`、134 个源码模块 mypy、`make package` 和 `git diff --check` 通过。v16 性能报告使用预热 1 轮、测量 5 轮和 nearest-rank，并真实启动三条 lane、对每策略 360 只候选执行全局选择：板内预选 P95 28.446ms、单板评分 3.583ms、三板三策略墙钟 295.877ms、全局选择 2.434ms，均通过 250/250/1000/100ms 配置预算；报告同时保存三板各 18 个队列等待样本、串行参考、墙钟比、3.344947 秒进程 CPU 和 1080 峰值条目。最终 wheel 在仓库外安装全部声明依赖后从独立 site-packages 导入，两个 CLI、配置、9 项资源和 `pip check` 通过；Firefox 152 在实际 1280x720、1440x900、1920x1080 视口均生成有效 PNG，DOM 检查及人工复核确认无白屏、关键同级重叠、裁切或页面级横向溢出。

- 两层失败先行回归已复现普通数据 lane 饱和时紧急任务无法启动，以及候选报价因此超过批次截止；实现后紧急任务和 `MarketFeatureService.refresh_candidate_quotes()` 均在普通 lane 被阻塞时按时完成，组合根契约确认生产 6-worker 池内恰有 1 个紧急 worker，背压回归确认只允许 1 个紧急等待任务且更多提交被显式拒绝。`make format-check`、`make lint`、111 个源码文件 mypy、完整 457 项 pytest、`make package` 和 `git diff --check` 通过，pytest 仅保留 10 条既有未知测试模型名 RuntimeWarning；最终 wheel 在仓库外隔离目标目录安装全部依赖后通过包导入、两个 CLI、9 项 Web 资源与 `pip check`。本批未修改 Web 资源，桌面门禁沿用同资源 1280x720、1440x900、1920x1080 三档已通过基线。

- 失败先行组件回归已复现同一代码在相隔 3 分钟的周期风险刷新中被请求两次，以及整批截止时未初始化 TTL 的异常；修复后成功结构化研究仅请求一次，整批截止写入短期降级并正常返回。`make format-check`、`make lint`、111 个源码文件 mypy、完整 454 项 pytest、`make package` 和 `git diff --check` 通过，pytest 仅保留 10 条既有未知测试模型名 RuntimeWarning；最终 wheel 在仓库外隔离目标目录安装全部依赖后通过包导入、两个 CLI、9 项 Web 资源与 `pip check`。本批未修改 Web 资源，复核上批同资源 1280x720、1440x900、1920x1080 三档截图无白屏、重叠或页面级横向溢出。

- 失败先行契约已复现看板缺少状态快照身份对账；修复后 `make format-check`、`make lint`、111 个源码文件 mypy、完整 452 项 pytest、`make package` 和 `git diff --check` 通过，pytest 仅保留既有未知测试模型名 RuntimeWarning。最终 wheel 在仓库外干净虚拟环境安装全部依赖后通过 site-packages 导入、两个 CLI、9 项模板/静态资源和 `pip check`；Firefox 152 在 1280x720、1440x900、1920x1080 三档截图中无白屏、重叠或页面级横向溢出，`dashboard.js?v=8` 契约通过。

- v15 局部回归矩阵、`make format-check`、`make lint`、124 个源码文件 mypy、完整 pytest、sdist/wheel 构建和 `git diff --check` 全部通过；pytest 仅保留既有未知测试模型名 RuntimeWarning。显式性能报告在固定 5500/360 负载、1 次预热和 5 次测量下通过：标准化 P95 142.228ms/800ms、双源合并 P95 910.468ms/1000ms、统一快照 P95 1296.614ms/1500ms，冷热缓存、峰值条目/估算字节和单慢源隔离均通过。最终 wheel 从仓库外目标目录导入，`trader-cli --help`、schema v5 `validate-config`、`pip check` 及模板、4 个 CSS、2 个 JavaScript、2 个 SVG 共 9 项资源通过。真实 Chrome 在 1280x720、1440x900、1920x1080 下均无白屏、资源失败、脚本异常、同级重叠、文字裁切、页面级横向溢出或抽屉越界，三档 v15 明细截图已人工复核。

- 本批通过 `make format-check`、`make lint`、111 个源码文件 mypy、完整 pytest、`make package` 和 `git diff --check`；架构 AST、`create_app()` 无副作用、固定融合 83.40、预算并发/重试、冻结恢复/哈希、SSE 游标与慢客户端均在完整套件内通过。最终 wheel 从仓库外 `/tmp` 目标目录导入，`trader-cli --help`、`validate-config`、`pip check` 及模板、3 个拆分 CSS、2 个 JavaScript、2 个 SVG 均通过。真实 Chrome 在 1280x720、1440x900、1920x1080 下加载全部 CSS，无白屏、脚本异常、关键同级重叠、文字裁切或页面级横向溢出，三张截图已人工复核。

- 本批 `docs/hi.md` 可执行计划通过 `markdownlint` 和 `git diff --check`；`make format-check`、`make lint`、111 个源码文件 mypy、完整 pytest、sdist/wheel 均通过，pytest 仅保留既有未知测试模型名 RuntimeWarning。最终 wheel 以 `--target` 安装到仓库外 `/tmp` 后从隔离路径导入，`trader-cli --help`、`validate-config`、模板、CSS、两个 JavaScript、两个 SVG 和当前环境 `pip check` 均通过。本批无活动 UI、API 或运行逻辑变化，未重复桌面截图。
- 本批文档验证：`markdownlint docs/hi.md`、`git diff --check -- docs/hi.md CHANGELOG.md` 和 `make package` 通过。`make format-check`、`make lint`、`make type-check` 受到本批开始前工作树中代码拆分改动的既有格式、导入和类型错误阻断；全量 `make test` 仅有既有 `tests/contract/test_v2_app_factory.py::test_dashboard_uses_packaged_v2_assets` 因拆分后的 CSS 未包含 `.runtime-error` 的失败。本批未修改这些实现或测试文件。
- 本批失败先行回归已复现连接离开上下文后仍可用及预算库异常导致 `/api/status` 500；修复后预算与共享快照连接在正常和异常路径均报告已关闭，模拟 `sqlite3.OperationalError` 时状态接口返回 200 与 `budget_store_unavailable`。`make format-check`、`make lint`、77 个源码文件 mypy、420 个 pytest、sdist/wheel 均通过；仓库外隔离目录安装最终 wheel 后，包来源、首页 200、6 项 Web 资源、`trader-cli --help` 和 `pip check` 通过。1280x720 无头 Firefox 默认配置被已有无响应实例拒绝，隔离 profile 超过两分钟仍未生成截图并已安全终止；1440x900、1920x1080 因同一宿主浏览器阻断未重复运行，本批未把三档桌面门禁记录为通过。
- 项目级 Review 回归覆盖 DeepSeek 审计字段不影响动作/排序、跨日显式 stale fallback、`full_market` 执行前/执行中超时、候选池/特征/history cache 迟到隔离、唯一组合根和 JSON/SQLite 共享单 persistence worker；`make format-check`、`make lint`、77 个源码文件 mypy、413 个 pytest 与 sdist/wheel 构建全部通过。最终 wheel 在全新仓库外虚拟环境安装全部依赖后，`pip check`、site-packages 导入、`trader-cli --help`、`trader-server --help` 及模板、CSS、JavaScript、Lucide 图标和产品图标资源验收通过。本批修复未修改 Web 资源；本地临时页面返回 200，三档截图因宿主 Firefox SWGL 无法映射 framebuffer 未生成，未将环境失败记为视觉通过。
- 评分链路回归证据：本批在 `tests/unit/domain/test_strategies.py` 与 `tests/unit/application/test_recommendations.py` 增补 `local_strategy_weights` 覆盖注入与推荐快照字段持久化回归；`local_strategy_weights` 变更后的本地评分通道与推荐排序行为已在代码层落地，验收建议在完整门禁前补跑 `make format-check && make lint && make type-check && make test && make package`。
- 本次 P12 落盘统一提交新增/更新了 3 个组件回归测试，覆盖 `news` raw payload 落盘、缓存命中回放与过期降级。标准化收敛新增 `tests/unit/test_v2_market_data_normalize.py`，覆盖 `to_float` 的空值/非有限值分支、`normalize_quotes` 的生成器输入兼容、`None` 过滤与字段转换边界。当前未执行全局 `make quality` 门禁；如需验收请补充 `make format-check`、`make lint`、mypy、pytest 及 `make package` 验证（包含仓库外 wheel 安装与资源读取）。
- 第 26 节 Review 复算 today/tomorrow/d25 三组候选权重和三组本地评分权重均精确为 100%，并逐项核对 v9/v10 状态隔离、互斥因子无生产方向授权、三板总体、上市日龄、流动性回退、可靠度、集中度、83.40 融合向量及 DeepSeek 158/188 边界。`make format-check`、`make lint`、67 个源码文件 mypy、336 个 pytest、sdist/wheel 和 `git diff --check` 均通过；仓库外目标目录强制安装最终 wheel 后，包从隔离路径导入，`trader-cli --help`/`validate-config` 和模板、CSS、两个 JavaScript、两个 SVG 共 6 项资源验收通过。本批无活动 UI 变化，三档桌面视觉验收沿用既有通过基线。
- 通过 GitHub 官方仓库页逐项核对当前 Star 和项目定位：新增 OpenBB 70.7K、NautilusTrader 24.8K、FinGPT 20.9K、LEAN 20.6K，并确认从 Qbot 18.1K 到 QuantsPlaybook 5.6K 的十个被移除项目均低于 20K；复算 15+68+35+30+10=158、挑战者 6+6+5=17、预算桶上限总和仍为 188。`make format-check`、`make lint`、67 个源码文件 mypy、336 个 pytest、sdist/wheel、`git diff --check` 均通过；仓库外目标目录强制安装最终 wheel 后，包从隔离路径导入，`trader-cli --help`/`validate-config` 和模板、CSS、两个 JavaScript、两个 SVG 共 6 项资源验收通过。
- 对照 DeepSeek 官方 V4 迁移、思考模式、上下文缓存和 JSON Output 文档核对模型名、参数与错误边界；逐项复算阶段目标为 144、预算桶总和为 188，并检查挑战者目标包含在原策略桶上限内。`make format-check`、`make lint`、67 个源码文件 mypy、完整 pytest、sdist/wheel、`git diff --check` 和仓库外 wheel 导入/CLI/6 项包资源验收通过。
- 通过 GitHub 官方仓库页面、可用 REST 结果和本地 Git 历史核对 17 个项目的 canonical 链接、DeepSeek/A 股能力及借鉴边界；确认 12 个策略方法引用出自首次提交 `841355c` 且该章节后续被删除。`make format-check`、`make lint`、67 个源码文件 mypy、完整 pytest、sdist/wheel 构建和 `git diff --check` 通过。仓库外虚拟环境安装最终 wheel 后 `pip check`、`trader-cli --help`、包来源和模板/CSS/两个 JavaScript/两个 SVG 共 6 项资源验收通过。
- 第 19-23 节新增回归覆盖当前/历史/fallback 精确身份、跨日 ETag、历史当前行情叠加、400/404 请求上下文、静态资源与抽屉字段、来源计划/成功/失败/P50/P95、DeepSeek success/failed/abandoned/429/超时/token 审计、持久化健康与冻结哈希重启查询，以及每策略候选/过滤/耗时/TopK/版本/veto 状态。`make format-check`、Ruff、67 个源码文件 mypy、336 个 pytest、sdist/wheel 和 `git diff --check` 通过；仓库外 Python 3.11 环境强制安装最终 wheel 后依赖一致、包从 site-packages 导入、`trader-cli validate-config`/`--help` 正常，模板、CSS、两个 JavaScript 和两个 SVG 共 6 项资源可读。无头 Chrome 在 1280x720、1440x900、1920x1080 均无白屏、页面级横向溢出、区块重叠、图片失败或非预期脚本异常，未启动 publisher 时 SSE 503 按预期回退。
- 第 14-16 节回归覆盖五维 schema/证据子集、未知维度和 0.50 覆盖回退、无新闻候选调用、429/超时/非重试 4xx、schema 修复、部分返回、迟到隔离、两级缓存、价格 1%/量比 0.3 边界、六桶/十阶段/emergency 并发预算及重启恢复；Ruff format/lint、67 个源码文件 mypy、329 个 pytest、sdist/wheel 和 `git diff --check` 通过。最终 wheel 在仓库外 `--target` 强制安装后从隔离路径导入，`pip check`、`trader-cli validate-config`/`--help` 与模板、CSS、两个 JavaScript、SVG 共 5 项资源通过；无头 Chrome 在 1280x720、1440x900、1920x1080 均无白屏、页面级横向溢出、区块重叠、图片失败或非预期脚本错误，未启动 publisher 时 SSE 503 按预期回退。
- 快照工作流模块重命名回归覆盖新路径存在、旧路径禁止、生产导入和流水线集成；`make format-check`、Ruff lint、67 个源码文件 mypy、319 个 pytest、`make package` 和 wheel 模块清单检查均通过。最终 wheel 在仓库外 Python 3.11 venv 强制重装后，`pip check`、新模块导入、旧模块缺失、`trader-cli validate-config` 及模板/CSS/两个 JavaScript/SVG 共 5 项资源验收通过；无头 Chrome 在 1280x720、1440x900、1920x1080 均无白屏、页面级横向溢出、区块重叠、图片失败或非预期脚本异常，未启动 publisher 时 SSE 503 按预期回退。
- 第 4-7 节统一回归覆盖完整事件/CAS/重放与有界 worker 生命周期，虚拟交易日每类 cadence 精确次数、周期错过不补跑、关键单点延迟及重启恢复、同任务在途跳过，TopK 草稿/冻结 overlay、全市场 single-flight、熔断半开恢复、候选与分钟乱序拒绝、失败保留最近有效数据、时效 2 倍/3 倍边界、SSE 与 today 发布延迟，以及状态读取不触发日历 I/O；`make format-check`、Ruff lint、67 个源码文件 mypy、318 个 pytest、`make package` 和 `git diff --check` 均通过。最终 wheel 在仓库外 Python 3.11 venv 安装全部声明依赖后 `pip check` 无 broken requirements，`trader-cli validate-config`/`--help` 正常，模板、CSS、两个 JavaScript 和 SVG 共 5 项资源可读；无头 Chrome 在 1280x720、1440x900、1920x1080 均无白屏、页面级横向溢出、区块重叠、图片失败或非预期脚本异常，未启动 publisher 时 SSE 503 按预期回退。
- 第 13 节回归覆盖 d25 15/30、40/60 精确边界及线性中点，long 3/6/9/12 月年化、估值/成长/质量/行业政策/风险保护公式，质押 10/20/35 与解禁 1/5/10 精确分级，财务公告点时过滤、成功空源、单来源失败、畸形/越界输入、多语义证据、证据上限、双模式缓存、配置缺失/漂移/关键词重叠、输入版本、缺失降级及确定性回放；受控真实请求确认 600036 财务、57 条有效公告、质押和解禁点时源均可解析且无结构化来源错误，未保存完整外部载荷。完整门禁为 63 个源码文件 mypy、Ruff format/lint、265 个 pytest、sdist/wheel 和 `git diff --check`；仓库外 Python 3.11 环境安装全部声明依赖并强制重装最终 wheel 后，`pip check`、site-packages 包及新领域模块导入、`trader-cli validate-config`/`--help`、首页和模板/CSS/两个 JavaScript/SVG 资源均通过。Headless Chrome 在 1280x720、1440x900、1920x1080 均无白屏、页面级横向溢出、关键区重叠、图片失败或非预期脚本异常；未启动 publisher 时 SSE 503 按预期回退。
- 第 12 节 134 项章节回归覆盖 tomorrow 六组件/全部子权重、收益/量比/收盘位置 0/50/100 端点、源时间晚于观察时点/接收时间越界/跨日/非交易时段/重复/缺口/午休/非法与非有限数据、未复权分钟端点和直连超时、候选限定、d25/long 字段隔离、批次截止、缓存容量、四项输入完整性、缺失降级、健康覆盖、配置与登记一致性、输入版本、prompt 证据子集、API/冻结往返和确定性回放。完整门禁为 62 个源码文件 mypy、Ruff format/lint、238 个 pytest、sdist/wheel 和 `git diff --check`；仓库外 Python 3.11 环境安装 wheel 及全部声明依赖后，`pip check`、site-packages 导入、新尾盘领域模块、`trader-cli validate-config`、首页和模板/CSS/两个 JavaScript/SVG 资源通过。Headless Chrome 在 1280x720、1440x900、1920x1080 均无白屏、页面级横向溢出、关键区重叠、图片失败或脚本异常，并保存三档截图；未启动 publisher 时 SSE 503 按预期回退。
- 对照检查 `docs/need.md` 第 1、5、8-17、23、25 节，v1 的 `strategy_and_prediction.md`、`money.md`、`software_design.md`、`plan.md` 和 `config/runtime.json`，并抽查当前 `config/v2/strategy.json`、领域过滤/排名实现及对应单元测试；确认旧口径 75/25 融合、Top5 和模拟退出链与当前 68/32、Top10、双冻结及只读研究边界的差异均有原文或配置证据。
- 第 11 节回归覆盖 today 五组件及全部子权重、正/负/中性关键词多数、重复证据、1/72 小时边界、未来/无效/过期时间拒绝、有效新闻截断顺序、候选缓存、来源失败 `null` 降级、配置缺失/重叠/固定值/版本哈希，以及新闻证据与派生值的冻结 JSON 往返和确定性回放；Ruff format/lint、60 个源文件 mypy、191 个 pytest 和 sdist/wheel 构建通过。仓库外 Python 3.11 环境安装 wheel 及全部声明依赖后，`pip check`、包导入、`trader-cli validate-config`、首页和模板/CSS/两个 JavaScript/SVG 资源通过；headless Chrome 在 1280x720、1440x900、1920x1080 均无白屏、页面级横向溢出、关键纵向重叠或脚本异常，并完成三档截图捕获。
- 第 10 节回归覆盖带宽公式边界/非法参数/非有限输入、配置风险触发边界、缺失值、策略适用范围、稳定事实 ID、实际值/阈值/来源/时间、证据 TTL、本地 veto、互斥去重、独立叠加、25 分封顶、配置 schema、恶意身份字段、桌面渲染文本和因子登记约束；Ruff format/lint、59 个源文件 mypy、182 个 pytest、sdist/wheel 构建及仓库外真实依赖安装、包导入、五项资源和 `trader-cli` 验收通过。
- 第 9 节回归覆盖成交额历史缺失、NaN、Infinity、49999999/50000000 边界，报价年龄和未来时间、OHLC 矛盾、跨源偏差复核、逐股过滤明细冻结往返、旧快照兼容、Top120 计数及离线重放；Ruff format/lint、59 个源文件 mypy、173 个 pytest、sdist/wheel 构建及仓库外 wheel 导入、五项资源和 `trader-cli` 验收通过。
- 第 8 节回归覆盖有界行业分层历史预热、冷启动加载、缓存过期重载、部分失败覆盖率、批次版本横截面隔离、宽度缺失分母、极值截尾、并列平均秩、NaN、单样本、冻结统计往返、完整因子登记及删除登记启动失败；Ruff format/lint、59 个源文件 mypy、162 个 pytest、sdist/wheel 构建及仓库外 wheel 导入、五项资源和 `trader-cli` 验收通过。
- 第 18 节回归覆盖 today 20 秒与 tomorrow/d25 30 秒精确边界、混合超龄/未来报价拒绝、配置/schema/锚点 manifest、两阶段版本复核、staged/committed 损坏隔离与上一冻结回退、跨交易日 stale 身份、overlay 持久化/哈希不变/迟到拒绝/源失败保留/15:00 收盘固化、SSE 和组合 ETag；Ruff format/lint、59 个源文件 mypy、154 个 pytest、sdist/wheel 和仓库外 SQLite v2/资源/CLI 验收通过。Headless Firefox 在 1280x720、1440x900、1920x1080 均完整加载 `dashboard.js?v=5`，页面级横向溢出为 false。
- 第 17 节回归覆盖 09:30、09:35:59、09:36 动作边界、主/降级窗口门槛、跨策略阶段拒绝、TopK 0-18 上界、最低观察分、0 推荐、行业限制、稳定排序、完整候选阈值报告及混合版本拒绝；Ruff format/lint、59 个源文件 mypy、138 个 pytest、sdist/wheel 构建均通过，仓库外安装后可导入 `trader`、读取五项 Web 资源并执行 `trader-cli threshold-report --help`。
- `docs/issues/2026-07-17.md` 已登记 16 项 `need.md` 符合性缺口，每项包含需求条款、证据与影响、修复步骤、验收条件、交付章节和状态；交付契约测试约束完整编号及必备字段。Ruff format/lint、58 个源文件 mypy、126 个 pytest、sdist/wheel 构建及仓库外包资源和 `trader-cli` 验收通过。
- 错过窗口补冻结、截止后冻结优先/草稿拒绝、策略身份及 30 秒缓存回归通过；Ruff format/lint、58 个源文件 mypy、125 个 pytest、sdist/wheel 和仓库外安装通过。重启真实服务后，today 因无截止前草稿明确返回 `not_ready`，tomorrow/d25 以不同冻结 ID 和分数连续稳定响应，页面加载 `dashboard.js?v=4`；Firefox 在 1280x720、1440x900 和 1920x1080 下切换 d25 正常且无页面级横向溢出。
- 今日 Bug 记录逐项包含用户问题、现状判断、修改说明、状态和后续验收，并明确未保存 DeepSeek 密钥或完整外部载荷；Ruff format/lint、58 个源文件 mypy、121 个 pytest、sdist/wheel 构建及仓库外 CLI/包资源验收全部通过。
- 交付契约测试校验 `AGENTS.md` 与 `docs/need.md` 均强制记录问题、修改、验证和风险；Ruff format/lint、58 个源文件 mypy、121 个 pytest、sdist/wheel 构建及仓库外 `trader-cli`/包资源验收全部通过。
- 推荐缺失原因与静态渲染契约测试通过；Ruff format/lint、58 个源文件 mypy、120 个 pytest、sdist/wheel 构建全部通过，仓库外安装后可导入包、执行 `trader-cli` 并读取模板、CSS、JavaScript 和图标。
- Web 资源契约校验三策略预取、策略/日期缓存、同键在途请求合并、日期与推荐并行加载及 `dashboard.js?v=3` 缓存失效版本。
- 组件回归覆盖东方财富三个 host 首轮断连后恢复，以及新浪单页首次 504 后恢复，确认重试次数有界且保留显式直连与 timeout。
- 组件回归覆盖 AKShare 新闻 JSONP 规范化、显式 timeout/直连参数、候选缓存复用和新闻源失败降级。
- 风险融合回归覆盖模型 veto 无效、本地监管规则有效 veto、错误证据类型和过期证据拒绝，以及策略 v3 配置字段解析。
- 第 25 节集成回归覆盖冻结输入 JSON 往返与确定性复算、有效配置和候选触发非零物理 DeepSeek 请求，以及 TopK P95 超过 10 秒时的显式失败状态。
- 本批次完整格式、Ruff、mypy、pytest、sdist/wheel 门禁通过；仓库外强制重装后可导入 `trader`、执行 `trader-cli` 与 `verify-freeze --help` 并读取全部 Web 资源；`run.sh` 实际状态返回 TopK P95 和 DeepSeek 零调用原因，headless Firefox 三档桌面窗口均完整加载且无页面级横向溢出。
- 第 24 节完整日影子测试使用相同固定输入运行两次，验证三个策略 committed 冻结、long 仅展示、四策略发布和跨运行 JSON 哈希一致；迁移矩阵逐项关联 24.1-24.9 的现有门禁。
- 新增交付契约测试，校验 `AGENTS.md` 与 `docs/need.md` 对“继续”整节交付、章节内全部子项和相邻章节边界的语义一致，并排除旧的最小任务措辞。
- 宿主机实测东方财富和新浪经系统代理均触发 TLS EOF、直连均返回 HTTP 200；组件测试覆盖东方财富全市场/历史、新浪全市场和腾讯定向报价的显式直连参数。
- 新增双行情源同时失败的网关契约，以及刷新失败后沿用既有候选、继续本地推荐、记录降级状态且不输出 traceback 的流水线回归覆盖。
- 对 `AGENTS.md` 与 `docs/need.md` 的单任务交付规则执行一致性 Review，覆盖任务边界、Review 基线、审查与交付状态、提交粒度、推送失败重试和成功后停止条件。
- `make quality`：Ruff format/lint、58 个源文件 mypy 和 106 个 pytest 测试全部通过。
- `make package`：从干净生成目录成功构建 sdist 和 `py3-none-any` wheel；sdist 不包含旧包或旧测试。
- 仓库外 `/tmp` 虚拟环境覆盖安装 wheel 后，`trader.__file__` 位于 site-packages，CLI 配置校验、首页和进程锁导入通过。
- wheel 内模板、CSS、两个 JavaScript 和 SVG 均可通过包资源读取，`create_app().test_client().get('/')` 返回 200。
- 无界面 Chrome 在 1280x720、1440x900、1920x1080 下均渲染 3 行 fixture，页面无横向溢出，抽屉在视口内且无脚本异常。
- 浏览器竞态测试通过：延迟 today 响应后立即切换 tomorrow，迟到响应未覆盖当前 Tab。
- `./run.sh validate-config`、架构 AST、无副作用 app factory、冻结恢复、预算并发和 SSE 慢客户端契约均已纳入门禁。
- 本批性能/缓存/实时性计划通过 `markdownlint docs/hi.md` 和
  `git diff --check -- docs/hi.md CHANGELOG.md`；当前工作树的 `make format-check`、
  `make lint`、111个源码文件mypy、完整452项pytest和 `make package` 均通过；pytest
  仅保留既有未知测试模型名RuntimeWarning。最终wheel安装到仓库外 `/tmp` 后可隔离
  导入，两个CLI、配置校验、`pip check` 和模板/4个CSS/2个JavaScript/2个SVG共9项
  资源通过。本批未改活动UI、API或运行逻辑，未重复三档桌面截图。

### Residual Risks

- 顶部两栏固定高度意味着超长状态必须在栏内滚动，避免页面跳动是本次明确取舍；手机和
  平板仍不属于产品范围。当前任务开始前已有的 `src/trader/application/recommendations.py`
  未提交修改不符合 Ruff format，导致全仓 `make format-check` 仍会在该无关文件失败；
  同批既有 `tests/integration/test_v2_pipeline.py::test_after_close_waits_for_reliable_board_features`
  与对应应用修改当前也存在预期不一致，导致不排除该节点的 `make test` 失败。本批 Python
  文件的独立 format-check/Ruff、其余测试、lint、mypy、package 和 wheel 验收通过，且未
  改写上述用户变更。

- Web 荐股展示批次：没有已知未解决代码问题。真实交易日中“某短线策略存在所选历史日期、
  另一策略缺失”的具体归档组合取决于用户运行库；本批用纯状态机、日期/API 契约和
  `snapshot_not_found` 回归覆盖客户端行为，验收过程未修改或伪造用户冻结历史。首次
  仓库外依赖全量复制因临时目录磁盘配额不足中止，已删除该临时目录；随后使用已通过
  工程门禁的开发依赖作为只读依赖路径完成 wheel 本体、资源、CLI、配置和 `pip check`
  验收。

- 2026-07-23 已在本批修复前提交的三份 0 只 `close_fallback` 属于不可变正式记录，按冻结
  契约不能删除、覆盖或用迟到预热结果改写；本批保证旧历史恢复可查，并阻止后续冷启动在
  三板历史未就绪时再次固化同类半成品，但不承诺每日必有推荐或放宽既有收益/风险门槛。

- 单入口只消除人工选态，不改变外部行情、DeepSeek、冻结或收盘补算的可用性；供应商持续
  失败时页面仍会展示最近同日有效草稿及降级状态，不能据此承诺数据始终实时或一定产生
  正式推荐。显式 `official|live` 仍属于兼容 API，普通页面不再暴露对应按钮。

- 本批恢复数据就绪和展示链路，不保证每日一定产生推荐或提高收益；候选仍必须通过既有
  硬过滤、每板 100 样本、可靠度 0.85、风险和动作门槛。外部行情源持续失败时仍会明确
  降级并保留最近有效快照。2026-07-23 15:00 在修复前固化的空 `close_fallback` 按冻结
  契约不可覆盖；新热缓存需完成一次预热写入后才能为后续重启提供即时复用。

- T1 的职责是建立可信测量而非提前优化。固定 Python 3.14.4 全链路报告中，标准化
  148.936ms 通过，但两来源融合 701.392ms、canonical 1620.766ms、targeted overlay
  commit 910.829ms 分别超过 600/900/100ms；其余评分、推荐、P6/SSE 和 API 指标通过。
  这三项继续作为 T2/T3 发布阻断项，TopK 来源年龄仍需真实交易日外部供应商观测。

- 本批只归并和删除文档，不改变生产评分或请求逻辑，也不构成收益改善证据。历史
  `docs/reports/` 为保持审计原貌仍会提到已删除计划的旧路径；活动规则只能从两份
  权威文档读取。四份旧计划内容仍可从 Git 历史恢复，后续收益/性能实验分别以 `strage.md`
  和 `times.md` 的独立门禁推进。

- 本批是工程等价重构，未使用真实供应商或 DeepSeek 网络证明外部时延，也未改变收益策略；
  宿主只实际运行 Python 3.14 与 Firefox 152，Python 3.10-3.13 由 Ruff/mypy/打包契约覆盖。
  Firefox 仍输出宿主 SWGL 与浏览器实验告警，但页面级错误捕获、DOM、SSE 和三档布局均
  通过；用户并行的 `docs/name.md`、`docs/times.md` 及其 CHANGELOG/交付契约修改未纳入
  本任务提交。

- `docs/times.md` 只记录审查结论和实施门禁，T1-T5 尚未实现；供应商真实网络时延、
  Session 复用线程安全、dirty 全量等价和 `local_pending` 用户语义必须在对应批次验证，
  固定离线性能结果不证明真实投资收益。

- 当前运行样本仍不足以证明任何规则能提高荐股收益：审查时 5,548 只股票中 5,349 只缺少
  20 日流动性历史，v2 仅有 5 个冻结快照。风险分层、热度、流动性、形态、候选和 TopK
  方案必须在至少 60 个交易日、300 条有效配对、净超额收益差 95% 置信下界为正且严重
  回撤率下降后，才能另立版本晋级。

- G5 没有已知未解决的仓库内缺陷。B4 首轮与重跑再次证明相对 CPU 指标受共享宿主调度
  影响，发布判断必须同时保留固定身份、业务哈希、绝对预算和重复测量上下文。本机仍只
  实跑 Python 3.14.4；真实行情/DeepSeek 时延、其他宿主、Firefox SWGL warning、fixture
  model warning 和前瞻收益证明继续作为外部或独立后续风险，不因 G5 发布而消失。

- A5 没有已知未解决的仓库内缺陷。固定离线证据只在当前宿主实际运行 Python 3.14.4，
  不证明 Python 3.10-3.13、不同宿主、真实行情/DeepSeek 时延或推荐收益；完整实时
  columnar 窄路径当前只覆盖 Eastmoney/Sina，其他输入保守降级 scalar。Firefox 的 SWGL
  warning 和 fixture model catalog warning 均未形成测试失败。是否收紧 384 MiB 必须另立
  任务收集真实峰值分布，不能把当前余量直接转换成缓存或业务容量。

- D5 没有已知未解决的 D-owned 缺陷。Firefox 的 SWGL framebuffer warning 属宿主日志，
  三档 DOM、WebDriver、SSE 和页面 JavaScript 均成功；真实外部网络时延、Python
  3.10-3.13 本机矩阵及推荐收益证明沿用 G4 已记录的外部或延期风险，不在本批伪称已验证。

- G4 已发布且 A5 尚未开始。共享宿主的 B4 三次预跑有绝对时延抖动，最终普通优先级固定
  样本通过，所有样本的业务哈希和内存一致；因此结果只证明当前固定离线负载，不代表真实
  供应商或 DeepSeek 时延。本机只实际运行 Python 3.14.4，3.10-3.13 仅有 Ruff/mypy/wheel
  metadata 静态兼容证据。Firefox 的 SWGL warning 属宿主日志，三档截图、DOM、WebDriver
  和页面 JavaScript 均成功。复验期间出现的并行 B5/D5 工作树与暂存修改已保留；G4
  使用 start HEAD 叠加本批 4 个文件的仓库外树复验和提交，不混入这些并行修改。

- B5 最终固定身份性能复跑通过，但同一共享宿主的两次预跑曾因调度/频率抖动未达到相对
  20% 门槛；业务哈希与内存始终一致，A4 已有 `35.544%` 集成证据。发布复验应保留固定
  fixture/身份并记录所有结果，不把单次计时泛化。优化路径仍只覆盖完整 Eastmoney/Sina
  全市场行，其他合法形态继续使用 scalar；本机只实际运行 Python 3.14.4，真实供应商时延和
  3.10-3.13 本机矩阵仍是外部风险，性能证据不代表投资收益提高。

- B4 快路径按设计只覆盖完整 canonical provider 行；新浪缺失字段、reference/Tencent overlay
  与 degraded payload 继续走已复验的 scalar 路径，因此不应把所有输入都宣称为 35.544% 改善。

- A4.1-A4.6 已完成且四方阶段 4 handoff 均为 `ready_for_gate=yes`；G4 尚未在本批发布，A5
  尚未开始。宿主只安装 Python 3.14.4；Ruff/mypy/wheel metadata 静态覆盖 3.10-3.14，但
  3.10-3.13 没有本机实际运行证据。Firefox 的 SWGL framebuffer warning 仍是宿主警告，D4
  三档截图、DOM、WebDriver 与页面 JavaScript 均成功。

- G2 已发布但 A3 未开始；下一批才能按计划进入 A3 集成。当前工作树仍有 B/C/D 未暂存实现
  改动，本批只归档门禁发布判断，不解决其内部实现或全局质量失败。

- A2 公共骨架已可用且 G2 已发布，但生产默认仍不得接入真实 B/C/D 实现；需下一批 A3 按
  B -> C -> D 顺序集成和复验。当前工作树还存在其他未暂存生产改动和 B/C/D 测试文件，本批
  保留且不纳入 A 侧提交。

- G2 已发布但 A3 尚未开始；阶段 3 必须继续按 A/B/C/D owner 范围集成。全局 `make lint`
  的严格债务计数漂移和全局 `make test` 的既有失败仍未在本批修复，不能宣称完整质量门禁绿色。

- A2 已实现公共类型、配置内存双字段和测试替身；完整 RSS/USS/Polars 原生估算、真实
  pipeline 100 tick 和 P6 发布峰值仍需阶段 2-4 集成门禁补齐。

- Polars 只改变基础设施层批次与变更集合，不改变领域评分、68/32 融合、风险、动作、排名
  或冻结哈希；性能通过也不代表荐股收益提高。真实供应商、真实 DeepSeek 和真实交易日仍受
  外部网络与数据质量影响。未跟踪的 `docs/plan_youhua.md` 属于用户既有文件，本批保留且不
  提交；若其继续违反三份非权威计划的文档治理契约，完整测试需隔离该文件后证明本批。

- 本批未调用真实外部行情或 DeepSeek 服务，供应商权限、实时限流和网络退化仍由现有超时、
  熔断、负缓存及降级契约承担；Tushare 是否支持明确 qfq 输出取决于运行时 SDK 能力，不支持
  时生产链固定使用腾讯 qfq，raw 只保留审计用途。2.5 应用编排重构仍是下一独立章节；本批
  性能门禁只能证明确定性固定负载不退化，不能证明实际荐股收益提升。并发新增且未跟踪的
  `docs/plan_youhua.md` 不属于本批提交，并会在本地触发现有“仅 5 份活动文档”契约；其归档或
  纳入文档治理须由所属独立批次处理。

- `plan_sudu.md` 仅为待实施计划；活动代码尚未引入 Polars、`MarketChangeSet`、P6 热索引
  或 SSE 差量协议，不能把目标延迟和节省描述为当前能力。Polars 对约 5500 行热路径的
  实际收益、Python 3.10-3.14 wheel 兼容和 256 MiB 内存适配仍须由固定 fixture 证明；
  若失败，生产继续使用现有 scalar 路径。性能优化本身也不是荐股收益提高的样本外证据。

- 2.4 基础设施适配器尚未开始；活动树仍登记 142 项既有严格复杂度/命名债务，后续章节须
  继续单调下降并在 2.6 归零。真实供应商交易日证据不属于本批离线门禁。

- `docs/plan_c.md` 描述的是待实施方案，活动代码仍会为 long 执行主审，权威策略仍保留
  long 预算与旧五维模型；必须按计划先更新权威契约和测试，再分批实现。历史 60 日、
  300 条有效配对和收益晋级验证已经延期，因此现阶段不能声称新方案提高实际荐股收益。
  此外，无关 `application/ports` 并发重构仍需其所属批次闭合导出、格式和债务基线后，
  当前工作树的完整门禁才能恢复通过。

- 工程重构计划 2.3-2.6 仍待后续独立“继续”批次；当前仓库仍登记 145 项应用层和基础设施层
  复杂度、参数数量及异常命名债务，本章没有越界处理。真实行情源、真实 DeepSeek 尾延迟和
  长期收益不由等价领域重构证明；固定性能数据受宿主负载影响，需继续按同一 fixture 复测。

- 2.1 完成时严格基线登记 163 项既有复杂度、参数数量和异常命名债务；该章只建立可执行棘轮，
  没有越界进入领域、应用或基础设施重构。后续每个完整章节必须降低并同步基线，最终章节
  前不得宣称全工程重构完成。真实行情、真实 DeepSeek 和长期收益仍不由本次基线修复证明。

- v17 的目标是减少结构性大回撤，当前自动门禁只能证明实现、确定性和审计正确性，不能证明
  实盘收益已经提高。既有冻结行没有 ATR20 时不会猜测回填结果；历史 qfq 与实时未复权锚点
  的公司行为一致性、v16/v17 同期影子和配对自举仍是后续独立批次。在至少 60 个交易日、
  300 条有效配对样本且净超额置信下界和严重回撤率同时达标前，不得宣传收益改善；必要输入
  缺失时正式推荐数量可能下降，这是有意的 fail-closed 行为。

- 计划中的复权修正、P1-P6 热路径、全市场候选概要和同期影子验证均尚未实施；性能数据是
  单机固定负载测量，真实行情尾延迟仍需持续观测。在至少 60 个交易日、300 条有效配对
  样本及预设置信门禁完成前，不能把该计划表述为已提高荐股收益。

- 组合重构把原先单个共享锁拆为六个组件锁，锁边界与生命周期已有组件回归、全量测试和
  固定负载覆盖，但真实交易日仍需继续观察来源尾延迟与健康计数。v15 性能脚本在宿主并行
  高负载时出现过一次合并/快照 P95 抖动越线，固定哈希未变化，空闲复测通过；这属于仍需
  持续运行门禁的环境敏感风险。本批保持 `MarketDataPort` 行为只是端口兼容，不代表内部
  保留旧聚合实现；旧 mixin 和共享状态类已完全删除。三板 CPU 评分改为单线程是下一
  个独立交付批次，尚未混入本提交。

- P95 数值受宿主负载和解释器版本影响，后续仍须以固定录制负载和现有预算作为回归门禁；
  有界元数据缓存只覆盖稳定、低基数的来源名和 dataclass 类型，不缓存行情内容。下一计划项
  （历史预热路径）尚未在本批修改；实际行情源延迟和三档桌面渲染行为亦不受本次后端纯计算
  优化影响。

- 收盘冷启动重建依赖行情适配器在同一交易日提供 14:59 后的三板完整收盘批次；供应商
  延迟、全源故障或历史/研究字段仍在预热时保持 `not_ready` 并退避重试，不会改用上一日
  数据或提交行情不完整的记录。冷启动分支按用户确认不新增 DeepSeek 请求，因此明确为
  `local_degraded/local_only`，与盘中已有融合结果可能不同。

- 800 行是宽松后的工程上限，不代表 501-800 行模块天然合理；职责、耦合和可测试性仍须在
  Review 中独立判断，超过 800 行仍由架构契约拒绝。本批只调整工程门禁，不产生运行、数据
  或兼容迁移风险。当前工作树另有用户并行业务修改，未纳入本批验证副本、暂存或提交。

- 推荐 Web schema v3 是有意的破坏性收缩；仓库外仍读取 v2 原始特征、板块、证据、缺失
  或完整 DeepSeek 审计字段的私有脚本需一次性迁移。领域快照与冻结格式保持不变，可继续
  用于离线审计和问题追溯。

- 1280x720、1440x900、1920x1080 实际截图仍被宿主 Firefox Snap/AppArmor 拒绝启动，
  且机器无 Chromium 备选；静态资源、CSS/JS 契约和语法检查通过，但本批不把三档截图
  记为已通过。全新虚拟环境安装全部大型数据依赖还受到宿主磁盘配额限制，仓库外独立
  wheel 前缀改用当前已验收依赖底座完成资源、入口与 `pip check` 验收。

- P2 恢复仍依赖东方财富/新浪全市场接口至少一个返回当日有效行情；若全源失败，历史
  实时列按契约继续显示 `-` 并在状态中记录 `current quote index recovery degraded`，不会
  用冻结日涨幅伪造今日数据。已运行的旧进程需重启后才会加载新后台任务和前端资源。
  宿主临时盘配额不足以在第二个干净环境重复下载全部第三方依赖；wheel 本体在仓库外安装，
  并复用当前已验依赖完成上述导入、资源、CLI 与 `pip check` 验收。

- 2026-07-22 的 today 在 11:20 后才启动，tomorrow/d25 的旧进程又未在 14:50 前形成
  30 秒内合格检查点，因此三个冻结查询在当日保持 `not_ready`；这是禁止迟到结果改写
  冻结记录的预期保护，不能用 14:41 草稿或重启后的行情补造。队列修复只能从下一有效
  交易窗口产生新冻结证据；外部来源延迟仍可能使策略合法返回少量或零推荐。

- 本批没有已知剩余风险；仓库内代码、测试、性能 runner、架构契约和历史路径说明已统一
  使用 `infra`。产品 API、命令入口、运行数据、冻结格式与桌面资源均未改变。

- 1 秒/3 秒等配置是最短计划间隔，不保证外部供应商固定延迟；新浪全市场完整分页若耗时
  5-8 秒，实际周期会由在途跳过自动延长，腾讯、东方财富或新浪限流/断连时仍按熔断和最近
  有效值降级。Tushare 120 积分 Token 的实际 `quota_or_rate_limit` 外部风险仍存在，本批
  不把它用于盘中实时行情，也不绕过 11:20/14:50 冻结规则补造已错过的快照。

- 官方权限文档列出的 A 股 `daily` 最低积分为 120，但当前 Token 的真实接口响应仍拒绝
  该请求并被脱敏归类为 `permission_denied`；运行时已自动使用腾讯/东方财富历史回退，
  仍应在 Tushare 控制台核验账户积分和接口授权。服务在 today 11:20 冻结后才重启，不能
  补造当日 today 快照；tomorrow/d25 已在 13:00-14:50 活动窗口形成，冻结后迟到结果仍
  不得回写。最终推荐允许少于目标数或为零，候选池非零不代表股票必然通过硬过滤、分数、
  可靠度、动作门和 TopK 集中度约束。

- 2026-07-21 的 today committed 快照实际为 0 条推荐，因此该日期不会凭空出现可填写两列的股票行；2026-07-20 的 tomorrow/d25 和 2026-07-17 历史已有推荐项并已现场验证实时列。最近 20 日以外首次访问仍是受哈希校验保护的冷读，随后进入 60 视图 LRU；完整 v17 P1-P6 分池、冷读 single-flight 和固定性能 CLI 仍按权威文档作为独立原子章节，不在本次缺陷批次内宣称完成。

- 当前无快照时页面会按用户要求保持空状态；这不会修复上游快照未发布本身。现场观测到当日规范行情已有 5,548 行、但全市场事件连续过期且 `snapshots_published=0`，属于独立流水线上游问题，后续仍需按事件 deadline、历史覆盖与候选形成链排查。本批只保证不再用昨日结果掩盖该状态。

- 本批是文档信息架构重整，没有运行 UI 或业务行为变化，因此不重复桌面截图和真实外部行情/DeepSeek 验收。旧文档的逐日过程日志已压缩为决策结论，完整原文仍可从本批基线 Git 历史追溯；v17 P1-P6 工程章节仍未实施，不得因文档合并视为完成。

- “锚点至今”和“今日涨跌”依赖 P2 当日内存行情已成功覆盖对应股票；服务冷启动尚未取得当日行情或行情源降级时，这两列会按契约显示 `-`，不会回退为旧锚点值。外部行情时效仍取决于来源可用性；本批没有修改 Web 资源，三档桌面结论沿用当前资源基线。

- v16 固定 fixture 与本地门禁已覆盖确定性和绝对性能预算，但本机串行墙钟/lane 墙钟 P95 为 0.843，未显示纯 Python 计算加速，因此三 lane 只宣告失败域隔离、有界并发和 1000ms 绝对预算通过。外部行情/DeepSeek 的真实交易日延迟仍取决于网络与供应商；本批不消耗真实 DeepSeek 额度、不宣称收益改善，也不提前实施 `docs/hi.md` 批次三的 v17 P1-P6 发布池与 Web 性能硬化。宿主 Firefox 仍记录 SWGL framebuffer 警告且高分辨率截图完成较慢，但本次三档 PNG 和 DOM 证据均有效；图形栈变化后仍应复跑发布截图。

- 紧急 lane 消除的是已确认的内部 FIFO 饥饿，不保证腾讯或本机网络始终在 3 秒内响应；紧急 worker 正在执行一个慢请求时只允许再等待一个紧急任务，更多并发请求会显式拒绝而不是无限堆积。普通 lane 从 6 个并发执行位调整为 5 个，可能增加全市场、历史或研究尾延迟，完整门禁不能替代真实交易日对 `urgent_*`、TopK 年龄和普通 lane P95 的持续观察。

- 本次代码修复只解决已确认的 D25 固定顺序饥饿；DeepSeek 读取超时属于外部网络或供应商响应问题，已失败物理请求按契约继续占用当日 188 次上限，当前阶段耗尽后只能等待合法后续阶段或下一交易日。tomorrow 尾盘分钟虽已现场恢复满覆盖，上游仍可能再次短暂失败；三类场景都会保留最近有效快照和显式降级，不承诺外部来源始终可用。新一轮 Firefox 截图受宿主 `RenderCompositorSWGL failed mapping default framebuffer` 阻断，桌面结论沿用本批未改动 Web 资源的上一批三档通过基线，不把本次环境失败记为新截图通过。

- 现场已确认服务端当日快照持续更新，但真实 SSE 丢事件时的恢复时延受固定 15 秒状态心跳约束；身份对账只解决页面停留旧快照，不改变行情源覆盖、候选过滤、DeepSeek 失败或当日快照确实尚未生成时的显式昨日 fallback。三档截图覆盖布局与资源加载，但其中后两档拍摄时运行进程已停止，动态实时数据切换仍由失败先行契约、API 契约和当日运行库证据覆盖。

- 本批固定响应覆盖并发、缓存、乱序、截止、降级、冻结兼容和性能预算，但未携带真实 Tushare Token 调用官方 SDK，也未替代真实交易日的上游覆盖率、429/额度、尾延迟和 TopK 年龄观测；Tushare 按候选逐代码慢调用可能耗时，但被独立 lane 隔离且不会阻塞四个免费来源。合并性能在当前机器满足预算但接近 1000/1500ms 上限，仍需真实负载持续观测。当前树已删除执行计划中的明文 Token，但该值曾进入已推送 Git 历史，仍须在供应商控制台吊销并轮换。v16 三板独立评分与统一选择尚未启用，工程可靠性改进不构成收益证明或投资收益承诺。

- 本批使用 mock DeepSeek HTTP 覆盖 V4 模型参数、429、超时、截断/非法 JSON、挑战者合并、缓存和预算，没有消耗真实 API 额度；供应商实际模型可用性、响应字段与网络质量仍需受控真实密钥冒烟。结构化风险源、真实交易日全市场负载和冻结时点仍受外部数据覆盖与尾延迟影响，失败时按契约保留最近有效快照并显式降级。工程门禁与策略一致性不构成收益验证，不能据此声称推荐收益提高。

- v15 五来源 lane、统一缓存、确定性合并和三板身份风险门已进入活动代码；v16 三板评分 lane、候选/评分权重和 78/76 门槛，以及 v17 等价性能硬化仍是后续独立批次。权重来自固定业务选择而非点时收益验证，后续实现通过工程门禁也不能据此宣称实际收益提高。
- 故障注入已覆盖 SQLite 打开失败，但无法在单元测试中制造宿主级文件描述符耗尽而不影响测试进程；两个生产 SQLite 边界的确定关闭契约直接覆盖已确认根因。若网络套接字或第三方库独立泄漏句柄，仍需依赖运行期进程 FD 监控定位；本批不重构为长连接，也不声称消除所有可能的宿主资源耗尽来源。三档桌面截图仍受宿主 Firefox 无响应阻断；本次 JavaScript 变化仅涉及预算不可用文本且静态契约通过，但发布门禁不能以此替代真实三档渲染。
- 本批生产逻辑没有新增 Web 资源差异，桌面布局沿用此前三档通过基线；本轮 headless Firefox 在宿主图形栈报 `RenderCompositorSWGL failed mapping default framebuffer`，1280x720、1440x900、1920x1080 未重新生成截图，发布前如宿主图形环境变化应补跑三档视觉验收。
- 固定时钟与故障注入已覆盖 `full_market` 队列等待和执行中越过截止的确定性行为，但真实交易日全市场并发负载、上游尾延迟和事件积压仍需运行观测；截止事件会明确记为 `expired` 并沿用最近有效快照，不再制造全局“最近错误”，但这不等同于消除上游变慢或机器资源不足。
- P12 缓存回放依赖服务时钟与 `wall_clock` 一致性：若时钟异常偏移导致 TTL 解析偏差，可能误判新鲜度；运行期应监控 `research_cache` 命中率与 `research_data_coverage_ratio`，并配套异常告警。
- DeepSeek 审计信号保持只读，`metadata.shadow_scoring` 只用于离线观察；尚未做收益验证回放，不代表今日/明日/2-5 日真实前瞻可提升。
- 本次标准化收敛已覆盖行情源解析与构建器协议，但未补齐 `RAW_FEATURE_SCHEMA`/`DERIVED_FEATURE_SCHEMA` 与策略维度版本之间的统一映射验证；新增适配器前仍需补充映射校验与回放一致性测试，避免字段漂移带来的特征口径变更。
- 本批新增的路由/迁移回归主要覆盖单元边界，尚未引入高并发全量行情与高频迁移压测；实际部署前仍需在真实运行环境补充并发源故障注入与迁移时长监控证据。
- 第 26 节只固定下一版机制和候选初值，活动代码仍运行 v9；三板隔离、同行差、换手状态、流动性层级和集中度控制具有机制意义，但没有真实前瞻收益证据。六组权重、P50/P80 和 0.85 不代表最优参数，也未定义生产启用条件；任何启用必须另建完整业务契约并独立交付，不能通过当前配置或运行时开关提前生效。
- GitHub Star 会持续变化，20K 筛选只代表 2026-07-19 快照，不能证明项目安全、许可证兼容、A 股点时正确或策略收益；Star 跌破门槛或候选项目升至门槛后不会自动更新。158 是预算利用目标而非收益承诺，实际调用仍受候选、缓存、熔断、截止和降级约束；本批没有改动运行配置或代码，实施时仍需单独交付并验证原子预算和阶段调度。
- 本批次只固化下一版契约，当前 `config/v2/runtime.json` 和活动实现仍使用 `deepseek-chat`、单阶段 schema 与 133 次目标，必须在 2026-07-24 旧别名停用前另行完成实现、回归和受控真实 API 冒烟。挑战者与影子校准尚无真实 A 股效果证据，且本批按用户要求不定义离线验证和晋级条件，因此不能据此宣称收益已经提高或允许校准值进入生产融合。
- 外部仓库的安全、许可证、数据点时正确性和策略收益未经本项目验证，后续若引入其源码或机制，仍须固定 commit、独立审查并通过 A 股点时和样本外门禁。本批次未修改 UI，三档桌面验收沿用既有基线；本地回环临时服务授权被中断，未重复生成无行为变化的截图。
- 第 19-23 节仓库内行为已有固定输入与故障注入证据；第 25 节仍缺真实 A 股完整交易日的 TopK P95/冻结时延、受保护真实密钥产生的非零 DeepSeek 调用与阶段总结。固定输入、仓库外安装和本地桌面检查不能替代这些生产证据，齐全前不得宣告发布完成。
- 第 14-16 节仓库内状态机、预算、缓存和降级行为已有固定响应与故障注入证据，但尚未用受保护的真实 `DEEPSEEK_API_KEY` 重启服务并在 A 股交易时段验证非零物理调用、133 次阶段目标、上游 P95/限流和 schema 分布；这些仍是第 25 节发布阻塞证据，密钥不得写入仓库、日志、快照或进程参数。
- 本批次是内部模块纯重命名，仓库内引用和 wheel 均受门禁覆盖；若仓库外代码绕过公共入口直接导入旧内部路径，将需要改用 `trader.application.snapshot_workflow`，不提供旧名兼容层。
- 第 4-7 节及 v15 已有固定输入、虚拟全日时间线和故障注入证据，但尚未在真实交易日验证五来源共享池的全市场 P95、队列峰值、15 秒停机、TopK 全天 P95、来源熔断恢复和 today 报价到评分发布延迟目标；Python 不能中断已进入第三方 SDK/HTTP 的当前调用，停机会先协作取消后续批次并等待显式 I/O timeout，以无残留线程优先。固定输入门禁不能替代第 25 节真实交易日证据。
- 第 13 节使用录制响应覆盖全部边界，并用 600036 完成单股受控真实结构化请求；尚未在真实交易时段验证 120 只 d25 候选与 10 只 long 名单的整体覆盖率、P95 延迟、上游限流和缓存恢复。来源失败会保持 `null`、中性评分和显式降级，但首次生产运行仍需观察研究源成功率与 `research_data_coverage_ratio`。第 12 节分钟输入同样仍需真实交易日覆盖率与延迟证据。
- 本批次没有真实前瞻交易日、成本后组合结果或 DeepSeek 同池反事实数据，因此不判断 v1/v2 哪套更赚钱。进一步优化应先补充不进入产品运行链的离线点时评估：按交易日组合配对 local/hybrid、计入 T+1/涨跌停/停牌/费用与滑点、逐项预注册并控制多重检验；任何权重、硬过滤、动作门槛、TopK 或市场状态规则变化仍须先更新 `docs/need.md`、提升策略版本并单独交付。
- 当前候选预选的一级 35/25/20/10/10 权重已进入契约，但流动性和短期动量的二级权重及带宽参数仍由领域实现给出；在调整策略收益前，应优先把这部分身份纳入可版本化、可冻结复算的契约，避免代码变化未形成新的策略身份。
- 第 11 节 today 评分输入已闭环；配置化关键词是可审计的保守极性规则，不能理解否定、反讽或复杂语境，首次真实交易日仍需抽样核对标题分类分布。AKShare 线上 JSONP 形态尚未用真实脱敏响应闭环，当前证据仅来自录制响应与失败降级测试。
- 旧 `recommendation_snapshot_v2` 文件若生成时尚未写入 JSON 内 `config_version`，仍以 `legacy-unrecorded` 兼容读取；只有 runtime v3 后的新冻结可提供完整配置版本证据。
- 第 17 节 AUDIT-20260717-02、03，第 18 节 AUDIT-20260717-01、05、06，第 10-16 节评分/DeepSeek 状态与预算，第 4-7 节数据编排，以及 AUDIT-14、AUDIT-15 的仓库内实现均已完成；仍待 AUDIT-07/AUDIT-16 的真实交易日、真实 DeepSeek 进程调用与阶段总结证据。
- 回放算法 v9 不会把旧 v8 及更早冻结输入当作当前规则重新解释；旧快照须由对应旧 release 验证，当前阈值预注册只接受 v9 新冻结快照。
- 2026-07-17 运行目录没有 today 截止前草稿或冻结文件，因此不能合规恢复当日 today 推荐；修复只保证后续冻结和持有截止前 30 秒内有效草稿时的重启补提交。
- 问题归纳的内容完整性仍依赖交付 Review 判断；契约测试只能防止必备栏目和目标文档被删除，不能自动证明原因分析正确。
- 待办状态：AKShare 新闻 JSONP 仍需真实脱敏响应闭环，真实 DeepSeek 进程调用与线程/队列/逐阶段刷新仍按后续独立整节交付；tomorrow 尾盘分钟和 d25/long 结构化输入已完成仓库门禁，但仍需真实交易日覆盖率、延迟和上游限流证据。
- 可复算 latest/frozen 文件会增加本地 JSON 体积和序列化 I/O；全市场部分已裁剪为硬过滤和候选排序必需字段，发布前仍需在真实全市场规模下记录文件大小、冻结耗时和磁盘保留策略。
- 第 25 节仓库门禁可重复执行，但生产最终验收仍需真实交易日证明活动 TopK P95 不超过 10 秒、真实 DeepSeek 密钥产生非零调用并输出阶段总结，以及保存三档桌面截图；任一证据缺失时不得宣告发布完成。
- 本批次风险明细 renderer 契约已通过，且未修改布局 CSS；但宿主 snap Firefox 在重采三档截图时因 `RenderCompositorSWGL` 默认帧缓冲映射失败而无法创建 WebDriver 会话，只能沿用上一批三档无横向溢出基线。正式发布截图仍属于第 25 节阻塞证据。
- 固定输入完整日影子已覆盖冻结链和确定性，但真实 A 股 09:15-15:00 不间断影子观察仍未完成；生产发布前必须按 runbook 留存行情年龄、冻结哈希、桌面三分辨率和 v1 运行库未修改证据。
- 单个章节可能包含较多子项，交付 diff 和 Review 时间会相应增长；仍必须维持一个章节、一个提交、一次推送，并通过章节内逐项证据控制范围。
- 行情直连依赖本机网络可直接访问对应域名；若所在网络强制要求代理，三路实时行情会按既有熔断与最近快照策略显式降级。
- 行情提供方的 TLS 可用性仍由外部网络环境决定；全部来源首次启动即失败且没有内存缓存时不会生成新推荐，只保留仓库中最近有效的只读快照并等待后续刷新恢复。
- 尚未完成一个真实 A 股完整交易日的 v2 影子运行，因此 TopK 报价 P95、冻结点实时时延和阈值分布仍需在生产发布前留证。
- 用户已确认现有 `DEEPSEEK_API_KEY` 有效，但当前运行服务状态为 `configured=false`，说明密钥未注入该进程；密钥有效性不再列为阻塞原因，使用该密钥重启后产生非零真实调用与阶段总结仍是待留存的发布证据。
- 当前 Linux 环境没有 PowerShell，`run.ps1`/`run.bat` 已静态审查，仍需在 Windows PC 实机验证创建虚拟环境、单进程锁和 Ctrl+C 停止。
- 外部行情提供方可能发生字段或限流变化；组件测试使用脱敏固定响应，首次真实运行应观察来源覆盖、熔断和降级状态。
- 本批只完善待执行计划，v15-v17缓存、性能CLI和实时性硬化尚未进入活动实现；256 MiB、
  各路径P95及5%相对退化值是后续验收预算，不是已测得的性能提升。真实交易日上游
  尾延迟、数据覆盖和前瞻收益仍需另行留证，工程性能通过也不得表述为收益提高。
- 本批无界面变化，三档桌面验收沿用此前已通过证据；后续活动UI变化后必须重新实测，
  不能用本批文档验收替代。
