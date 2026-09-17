# Resolve Refactor Blueprint Architecture Conflicts

## User Request

修复重构蓝图只读 Review 发现的 15 类冲突及次要问题，并按用户明确裁决固定目标顶层目录、HTTP 所有者、
策略与档位范围、板块组 TopK、profile 选择、推荐端口、缺失字段扣分和阶段类型合同。

Regression-Key: `refactor-blueprint-single-architecture-truth`

## Cause

已确认 `docs/项目重构详细.md` 同时把未来业务线目录描述为现状，并混用了当前权威架构与目标架构；目录树、
正文路径、profile 所有者、推荐端口、启动降级、静态/动态事实、质量缺失、TopK、观测对象和后续摘要之间也存在
互相冲突的定义。根因是目标蓝图缺少明确的当前/未来边界，并在多个章节重复定义同一合同。

## Added

- 增加当前活动架构与未来目标架构的明确边界，以及切换时同步更新源码、权威文档、消费者、测试和 Changelog
  的门禁。
- 增加推荐 application ports、V2/V3 已加载档位端口、公共/业务 infra 所有权矩阵和唯一公开进程入口。
- 增加严格类型的 `PipelineStageSnapshot`、阶段专用不可变对象、受限单股诊断边界和档位按钮生成合同。

## Changed

- 目标产品能力目录固定为 `download/`、`training/`、`recommendation/`、`http_api/`、`web/`；
  `http_api/` 是目标唯一 HTTP/API 所有者，`web/` 只负责页面与资源。
- 目标生产范围固定为 Tomorrow、D25 与不评分的 Long；评分档位只允许选择 V2、V3 或二者同时运行。
- 每个档位 × 策略 × 板块组独立产生最终 TopK，组间不混排、不借位，不建立跨板总榜。
- 目录正文统一使用树中的 `base_scoring.py`、`grouped_ranking.py`、`topk_selection.py` 和
  `freeze_publish/`；profile 目录统一使用单数，应用层不再保存 profile 实现。
- 启动期对全部已选档位和 Tomorrow/D25 模型头原子校验；合法启动后的单次在线推理失败才允许规则结果降级。
- 停牌、当日交易状态和涨跌停价格移入动态事实；`code`、`price` 成为唯一证券代码与最新价字段名。
- 静态、动态、历史、质量、评分、模型、风险、融合、动作和选择对象改为逐字段列出真实类型与中文含义，
  并与第 8 章不可变类型定义保持同名；每个 `@dataclass` 示例的每个字段都附有对齐的中文业务含义注释。
- 仓库辅助目录统一为 `config/`、`docs/`、`tests/`、`scripts/` 和 `data/`，并固定历史归档、
  模型工件与冻结数据的目标树；SQLite WAL/SHM 仅是运行时伴生文件，不属于固定契约。
- 字段缺失按档位合同在评分时扣分，关键缺失固定扣至 0 分并停止模型、DeepSeek 与动作升级。
- 第 6、9、10 章改为引用唯一目录、14 层链路和观测合同，不再维护过期摘要。

## Fixed

- 消除目标目录与当前活动目录、`http_api/` 与 `web/api`、目标策略范围、板块组 TopK 与当前全局选择合同之间
  未标明阶段的冲突。
- 消除公共 infra 与业务 infra、训练 profile、推荐 profile 和应用 profile 的重复所有权。
- 消除将评分、风险、融合、动作和发布都表示成过滤，以及公开或冻结未入选股票身份的错误观测语义。

## Removed

- 从目标蓝图移除 Today、旧评分档位、跨板总榜、application profile 实现、逐股公开监控、全能
  `CandidateEvaluation` 和 Long 分数/排序语义。
- 移除正文中的同义路径、九步旧链路、弱化监控字段集和非规范性的备选措辞。

## Verification

- 逐项正向合同扫描通过：当前/目标边界、五个产品能力目录、`http_api/` 唯一所有者、V2/V3 选择与 Web
  按钮、推荐端口、infra 所有权、启动/在线失败分界、质量归零、组内 TopK、Long 和观测边界均存在。
- 逐字段合同扫描通过：静态、动态、历史、质量以及评分链各类型均以“字段名、真实类型、中文含义”列举，
  121 个 `@dataclass` 字段的行内中文含义注释均对齐到同一列，且可信缺失通过 `quality_pending` 进入评分扣分，
  身份不可信才进入 `refresh_pending`。
- 负向扫描通过：目标蓝图不含 Today、旧评分档位、复数 `profiles/`、四个树外同义文件名、旧字段同义词、
  逐股公开列表、`FilterSnapshot` 或浮点过滤率合同。
- 14 个层级标题数量与顺序检查、Markdown 围栏配对、根 Changelog 大小、交付记录六个必需章节和
  `git diff --check` 通过。
- 15 个定向契约测试通过：根 Changelog 索引与链接、权威文档一致性、当前产品合同和版本命名边界。
- 完整 `test_changelog_archive_contract.py` 的前两个用例通过，审计章节用例命中本批前既有记录
  `2026-09-15-data-readiness-filter-separation.md` 使用了大小写不匹配的 `## Residual risks`，未满足门禁要求的
  `## Residual Risks`；`git show HEAD:...` 已确认该问题存在于基线，本批新记录六个必需章节齐全，
  未扩大范围改写该历史批次。
- 本批只修订未来重构蓝图和交付记录，不改变活动源码、配置、API、Web、评分、冻结或持久化行为；因此不运行
  运行时、供应商、DeepSeek、性能、浏览器、打包或 wheel 安装门禁。

## Residual Risks

- 本蓝图尚未实施，不能作为当前运行行为证据；活动系统仍以 `docs/01_评分逻辑.md`、
  `docs/02_工程设计.md`、`pyproject.toml` 和现有源码为准。
- 真正切换将触及架构、评分、档位、冻结、API/SSE、Web、入口和历史兼容，必须作为高风险实施批次完成完整
  门禁后再更新两份权威合同并删除旧活动路径。
