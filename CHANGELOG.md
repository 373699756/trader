# Changelog

All notable changes to this project are documented here.

## Unreleased

### Added

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
