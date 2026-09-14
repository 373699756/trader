# Recommendation modular boundaries and training isolation

## User Request

将荐股逻辑拆成独立模块，令荐股运行链与训练链互不干扰，并把跨业务的技术能力收拢到 `infra`；要求先改代码、
补测试、Review 和回改后再提交。下载与训练命令及其既有工件不属于本批行为变更。

Regression-Key: `recommendation-modular-boundaries`

## Cause

已确认原运行适配器直接编排候选规划、本地投影、特征组装、风险和 DeepSeek 融合实现。调用者无法替换这些能力，
测试必须穿过整条运行链；已发布模型能力也没有完整转发候选资格所需的端口，训练/运行边界缺少可验证的契约。原子
JSON 与上海时钟还分别混在持久化/运行资源模块中，公共 owner 不清晰。

## Added

- 新增候选过滤、特征计算、本地评分、已发布模型评分、风险控制、排名选择和复核融合端口及无状态服务。
- 为 `MarketDataAdapter`、`ScoredSelectionUseCase`、`DeepSeekAdapter` 和投影流程增加显式能力注入点。
- 新增推荐边界、训练隔离、模型能力完整转发、原子 JSON 和业务时钟回归测试。
- 新增 `infra/clock` 与 `infra/atomic_files` 公共技术能力包。

## Changed

- 市场适配器只编排候选过滤和本地评分端口；投影只通过排名选择与风险控制端口生成决策；DeepSeek 适配器只通过
  融合端口生成 hybrid 结果。
- 已发布模型包装器完整实现 `ModelScoringPort`（策略支持、历史窗口、输入资格、评分和状态），不再让候选规划
  依赖训练实现。
- 训练命令仍只负责生成已校验 bundle；荐股运行只读取注入的已发布模型能力，不调用下载、样本构造或拟合流程。
- 原子 JSON 读写和上海时钟迁移到各自唯一的 `infra` owner，删除旧的重复持久化实现。

## Fixed

- 修复模型包装器在候选资格阶段缺少 `uses_model`、历史窗口和输入资格转发导致的运行时失败。
- 修复投影决策项中风险控制依赖未显式传入的问题，并保持本地风险只计算一次。
- 保持固定 68/32 融合、TopK/集中度、冻结、下载、训练和公开 CLI 行为不变。

## Removed

- 移除 `infra/persistence/runtime_json.py` 的重复公共能力 owner；没有删除历史数据、训练工件、推荐记录或运行数据库。

## Verification

- 定向推荐边界、输入运行、Today/Tomorrow/D25 投影与选择、模型路由、调度集成测试通过。
- `make lint`、`make type-check`、`make package` 和 `make performance-check` 通过；性能实证确认 `network_calls=0`，增量重算与全量结果一致。
- `git diff --check` 通过。
- `make format-check` 仍受本批之前的 4 个文件格式基线失败影响：`daily_history_cache.py`、
  `test_baostock_history_cli.py`、`test_entry_contract.py`、`test_v3_profile.py`；本批未扩大范围修改这些无关格式问题。
- `make test --maxfail=3` 暴露 3 个既有基线/历史契约失败：历史命名文本契约、旧模型 ID 断言和上一批交付记录章节契约；
  定向测试未发现本批回归。完整 `make test` 曾因相同失败中止，未宣称全量通过。
- 按用户要求执行 `./run.sh --profile v2`；脚本检测到 `.runtime/trader/server.lock` 已被占用并安全退出，
  当前沙箱内未发现可连接的服务进程，因此没有伪造运行时/API 成功证据，也未删除锁文件。

## Residual Risks

- 全量测试中的既有文档命名契约仍可能因此前批次已跟踪历史文本失败；本批不改动无关历史记录。
- 未进行真实外部行情、DeepSeek 或浏览器运行验证；本批只改变应用能力装配和基础设施 owner，不改变供应商协议或 Web schema。
