# V2-only 可执行迁移计划

> 状态：V2-0 已完成，V2-1 待执行。
>
> 本文只定义施工顺序和退出条件，不定义产品或策略行为。权威边界分别位于
> `docs/software-business-design.md`、`docs/recommendation-strategy.md` 和
> `pyproject.toml`。用户每次发送“继续”或语义等价指令，只执行下一个完整未完成章节。

## 1. 总体原则

- 最终 release 只保留 V2；不保留旧 URL、旧 schema、旧快照读取、双读、双写或运行时开关。
- 开发期间旧实现只能作为尚未替代的待删除代码存在，V2 不得新增对它的依赖。
- 新运行目录固定为 `.runtime/v2`；现有旧目录不迁移、不读取、不写入。
- 回退依靠完整旧 release 和对应旧目录，不在新代码中实现兼容分支。
- tomorrow 优先；today、d25 和 long 依次迁移，最终统一根页面和只读外壳。
- 当前评分研究 P0 与 V2 工程迁移相互独立；迁移不得提前改变权重、阈值、融合或收益晋级。

## 2. 每批统一交付规则

每批必须完成：

1. 记录 `HEAD`、上游、已有工作树和本批文件范围。
2. 先修改权威契约与失败测试，再修改实现。
3. 审查完整 diff，修复正确性、冻结、并发、资源、降级、安全、类型和可安装性问题。
4. 运行定向测试以及适用的 `make format-check`、`make lint`、`make type-check`、
   `make test`、`make package`。
5. 涉及 Web 时完成 1280x720、1440x900、1920x1080 浏览器验收。
6. 仓库外安装 wheel，验证包导入、`trader-cli`、模板、CSS、JavaScript 和图标。
7. 更新 `CHANGELOG.md` 的 Added、Changed、Fixed、Removed、Verification 和 Residual Risks。
8. 只暂存本批文件，创建一个 Conventional Commit，推送并确认 `HEAD == @{upstream}`。
9. 推送成功后停止，不自动进入下一章节。

## V2-0：唯一产品契约重置

状态：已完成。

目标：删除双版本长期共存、生产指针和旧格式读取要求，固定 V2 唯一链路、运行目录、API、
删除边界和完整 release 回退方式。

交付：

- `docs/V2.md` 改为 V2-only 目标概览。
- `docs/software-business-design.md` 固定唯一活动架构、运行目录、API 和无兼容规则。
- `docs/recommendation-strategy.md` 固定 V2 原生决策为唯一活动策略口径。
- 本文替换旧 P0-P13 影子迁移路线。
- 契约测试拒绝重新引入旧 API 保留期、旧数据读取或双链切换。

退出条件：文档不存在要求新 release 读取旧数据或保留旧 Web/API 的活动契约；定向契约测试、
Ruff 和文档记录门禁通过。

## V2-1：统一 V2 数据平面

状态：待执行。

目标：所有策略只消费一组不可变、点时一致、带字段来源和质量状态的 V2 epoch。

实施：

- 收敛证券主数据、交易日历、全市场行情、候选报价、历史特征、研究事实和公司风险端口。
- 复核现有 `SourceCapability` 清单与 `docs/reports/v2-p1-source-capability-baseline.md`；未验证来源不进入评分、冻结、组合根或生产配置。
- 固定 `DailyFeaturePack`、`MarketEpoch`、`CandidateQuoteEpoch` 和 `ResearchEpoch` 的父子身份。
- 所有字段携带来源、源时间、接收时间、质量状态和内容版本。
- 字段级合并禁止价格-only 响应清空证券身份、历史、风险或更完整行情字段。
- 数据源失败保留最近有效 epoch，并以 `stale`、`degraded` 或 `not_ready` 显式暴露。
- 持久化证券主数据、交易日历、历史摘要和风险组件，使重启不从零预热。

退出条件：同一快照无新旧拼接；潜在可执行代码主数据覆盖 100%，候选核心历史覆盖不低于
99%，无效空不得覆盖最近有效数据。

## V2-2：统一决策核心与持久化

状态：待执行，依赖 V2-1。

目标：建立四类视图共享的 V2 原生身份、CAS 当前索引、冻结仓储、报价 overlay、查询模型和
事件模型。

实施：

- today、tomorrow、d25 使用统一 scored decision identity，保留各自纯领域评分策略。
- long 使用独立无评分 projection identity，不伪造评分字段。
- 当前索引按策略保存最后一个已提交不可变版本，发布必须携带 expected version。
- hybrid 只能引用当前 local 父版本；旧交易日、旧 sequence、冲突内容和迟到结果拒绝。
- 报价 overlay 必须匹配 decision/projection version。
- V2 冻结仓储只接受当前 schema，按策略和交易日唯一提交并校验 SHA-256。
- 统一查询只读取索引、V2 冻结仓储和匹配 overlay。

退出条件：并发 CAS 只有一个胜者；重启恢复、损坏文件、半提交、哈希冲突和跨策略隔离测试
全部通过。

## V2-3：独立调度与生命周期

状态：待执行，依赖 V2-2。

目标：建立不依赖旧 `RecommendationPipeline`、旧发布器或旧全局状态的唯一 V2 运行时。

实施：

- V2 调度器直接驱动数据刷新、原生输入、决策 worker、DeepSeek、发布、冻结和结算。
- 每策略使用有界 latest-wins 队列；tomorrow 获得行情、计算、模型和发布保留容量。
- 共享 DeepSeek 预算、缓存和 single-flight，物理请求每日总上限保持 168。
- 后台线程可停止、可等待、可观察；关闭共享一个有界 deadline。
- `bootstrap.py` 只构造 V2 资源；`create_app()` 只接收只读查询。

退出条件：停用旧 Pipeline 后 V2 fixture 仍可完成数据、决策、发布、冻结和有界关闭；HTTP
请求不产生外部 I/O。

## V2-4：Tomorrow 正式接管

状态：待执行，依赖 V2-3。

目标：把现有 tomorrow 原生能力改为正式 V2 tomorrow，不再接受旧 snapshot baseline、旧
冻结状态或影子比较输入。

实施：

- native input 直接生成 local，合法结构化 facts 生成引用当前 local 的 hybrid。
- 删除 `ShadowObservingSnapshotIndex`、tomorrow cutover gate、shadow evidence 和 baseline 关联。
- 14:49:20 checkpoint、14:50 封口、冻结重试和 15:00 后恢复只使用 V2 决策。
- 当前、历史、冻结、overlay 和事件全部绑定同一 V2 决策身份。

退出条件：固定融合向量为 `83.40`；local/hybrid CAS、迟到拒绝、冻结不可覆盖、重启恢复和
合法空结果测试通过。

## V2-5：Today 正式接管

状态：待执行，依赖 V2-4。

目标：使用统一 V2 数据与决策核心实现 today，并严格保留 11:20 边界。

实施：

- today 原生输入和纯领域选择直接发布 local/hybrid。
- 11:20 当场冻结；错过后保持 `not_ready`，不得 checkpoint、启动追补或收盘补算。
- 已有同日正式记录只允许匹配报价 overlay，不修改名单、分数、动作和排名。
- 正式记录只保存 `executable`，观察池只存在于冻结前内存。

退出条件：11:19:59、11:20:00、边界后启动、冻结重试、迟到模型和报价 overlay 测试通过。

## V2-6：D25 正式接管

状态：待执行，依赖 V2-5。

目标：使用统一 V2 数据与决策核心实现 d25，并保持 14:50 冻结和合法收盘恢复。

实施：

- d25 原生输入、专属评分、local/hybrid CAS 和查询接入统一核心。
- d25 与 tomorrow 使用独立索引键、冻结唯一键、事件身份和错误状态。
- 15:00 后仅在同日正式记录不存在且无待重试封口时创建一次 `close_fallback`。

退出条件：14:50 边界、热运行/冷启动恢复、正式空记录、跨策略隔离和不可覆盖测试通过。

## V2-7：Long 正式接管

状态：待执行，依赖 V2-6。

目标：把固定长期研究池接入 V2 projection、查询、状态和 SSE，不进入评分与冻结核心。

实施：

- 固定池和分组继续由 `long_watchlist.json` 唯一维护。
- 腾讯定向报价和最近有效行情生成无评分 current projection。
- long 不调用 DeepSeek、不执行候选/TopK、不冻结、不写推荐历史或收益结算。
- 部分行情失败保留同日最近有效报价或明确缺失占位，不自动换股。

退出条件：long 始终报告 `score_status=not_applicable`；当前报价、分组唯一性、失败降级和零
DeepSeek 请求测试通过。

## V2-8：统一 API、SSE 与根页面

状态：待执行，依赖 V2-7。

目标：四类视图只通过统一 V2 只读外壳和一个桌面工作台访问。

实施：

- 根页面 `/` 渲染统一 V2 工作台，不再存在独立 tomorrow 旁路页面。
- 只保留 `/api/v2/decisions/<strategy>/current`、`history`、`dates`、`/api/v2/status` 和
  `/api/v2/events`。
- SSE 使用单调序列、有界历史、有界客户端队列、游标恢复和慢客户端隔离。
- 正常 SSE 在线时不轮询完整 current，断线后才低频恢复。
- 页面展示数据年龄、覆盖、过滤漏斗、预算、冻结、降级和逐股诊断。
- 所有原因码映射具体中文，未知码使用受控兜底并限制本地诊断容量。

退出条件：三档桌面分辨率无白屏、重叠、页面级横向溢出或明显跳动；API ETag、SSE 重同步、
慢客户端和无外部 I/O 契约通过。

## V2-9：唯一组合根与入口

状态：待执行，依赖 V2-8。

目标：`trader-server`、`trader-cli`、启动脚本、配置和 `bootstrap.py` 只装配 V2。

实施：

- 运行目录改为 `.runtime/v2`，配置 schema 只接受当前 V2 版本。
- 删除旧环境变量映射、旧迁移命令、旧 archive 命令和 cutover evidence 命令。
- CLI 只保留 V2 配置验证、V2 冻结验证、性能验收和必要只读运维命令。
- 启动、初始化、关闭和进程锁只操作 V2 资源。

退出条件：全新目录启动、热重启、异常恢复、进程锁和 graceful shutdown 通过；旧目录存在时
新进程不读取、不写入。

## V2-10：删除旧生产链

状态：待执行，依赖 V2-9。

目标：删除全部不可达旧实现、测试、资源、配置和依赖，形成唯一活动代码树。

删除范围：

- 旧 `RecommendationPipeline`、P1-P6 编排、旧 snapshot publication、旧查询和旧 publisher。
- 旧 `RecommendationSnapshot`、旧 replay 分支和只服务旧身份的策略代码。
- 旧 snapshot repository、旧文件/SQLite schema、旧迁移器和旧运行 JSON writer。
- 旧 API/SSE 路由、序列化器、模板、Dashboard JavaScript/CSS 和静态名单副本。
- shadow runtime、shadow evidence、cutover gate 和双链一致性测试。
- 只服务旧链的 CLI、性能 fixture、依赖、配置字段、文档断言和测试。

退出条件：AST 和运行覆盖证明不存在旧模块调用；源码、测试、配置、文档和 wheel 中不存在
仍可到达或仅服务旧链的资源。

## V2-11：最终验收与发布

状态：待执行，依赖 V2-10。

目标：完成 V2-only release 的全部质量、运行、桌面、打包和回退验收。

门禁：

- `make format-check`、`make lint`、`make type-check`、`make test`、`make package` 全部通过。
- 架构 AST、`create_app()` 无副作用、固定融合向量、168 预算并发与失败计数通过。
- today/tomorrow/d25 冻结恢复、long 零评分、合法空与降级保留矩阵通过。
- SSE 游标恢复、慢客户端、断线重连、版本单调和 ETag 通过。
- 三档桌面和仓库外 wheel 安装通过。
- 新 release 只读写 `.runtime/v2`；完整旧 release 回退演练不与新目录混用。
- `CHANGELOG.md`、README、启动文档和两份权威文档只描述 V2 活动产品。

退出条件：零已知 Review 发现，所有适用门禁通过，创建一个最终发布提交并推送，确认
`HEAD == @{upstream}` 后将本计划标记完成并退役。
