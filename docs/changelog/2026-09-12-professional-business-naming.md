# Delivery Record: Professional Business Naming

## User Request

用户要求继续清理活动代码中不专业、临时分工化、重复或过度泛化的命名，并将前一轮 Review 计划完整落地。

## Cause

- 根因已确认：研究链曾将 `C3`、`E1`、`new/legacy` 等阶段或相对时序语言固化为类型、字段、schema、文件和候选 ID。
- 市场数据、调度、推荐和研究层存在多个同名不同义类型，调用者需要依赖导入路径才能判断语义。
- `models.py`、`runtime.py`、`facade.py`、`service_*.py`和 `base_client.py` 等文件名未表达单一稳定职责，制品哈希实现还分散在多个报告模块。

## Added

- 新增专业命名 AST/文本契约，禁止退役路径、C3/相对 holdout 名、含混公共类型和意外重复英文词。
- 新增 `domain.research.artifact_identity` 作为研究制品规范 JSON 和 SHA-256 身份的唯一职责模块。
- 新增 AKShare 共享 HTTP 协议模块，统一响应对象和请求函数类型，消除 provider 内部的重复同名协议。

## Changed

- C3 全系列改为日收盘模型选择与 `daily_close_ensemble`，同步类型、端口、终态、父 hash、schema、候选 ID、持久化 codec、脚本和测试。
- H1 只保留为“点时历史覆盖能力”的稳定业务边界；泛化的 `H1Strategy`、`H1HTTPSession`、`H1UniverseProvider` 改为具体业务角色。
- 推荐设置/组合选择、覆盖摘要/覆盖证据、公开/归一化变更集分别改为唯一语义名，`TradingCalendarPort` 收敛到单一定义。
- 前置检查、预注册规则候选、特征构建、因子诊断、成本选择、Shadow 评估和评分投影类统一使用责任型名称。
- 研究报告、运行监督、DeepSeek 完成客户端、市场数据加载/缓存/持久化、历史归档和诊断报告模块按真实职责重命名。

## Fixed

- 消除公共类型同名不同义、模块职责依赖路径猜测、相对时序字段和重复词文案带来的可读性与维护性问题。
- 联合研究明确区分“日收盘模型选择终态”与“未融合日收盘集成对照”，避免父档位键误用终态对象名。

## Removed

- 删除活动源码和脚本中的 C3、E1、`new_holdout_*`、`legacy_holdout_*` 及无职责的局部 `owner` 命名。
- 删除退役模块路径和旧公共名，不保留导入别名、双 schema 或双读 fallback。

## Verification

- 新命名契约 5 项通过；研究链 398 项定向 unit/contract 回归通过；市场数据、DeepSeek、调度、设置和诊断 448 项定向回归通过。
- `make format-check` 通过；`make type-check` 通过（396 个源码文件）；`make package` 通过。
- `make test-release` 首次受沙箱代理阻断，获准环境重跑通过；仓库外 wheel 安装、CLI、包资源读取全部通过，`resource_count=10`。
- `make test` 收集 1851 项，1850 项通过；唯一失败是上一批已登记的 systemd `WorkingDirectory` 引号问题，本批没有修改模板或安装逻辑。
- `make lint` 的常规 Ruff 检查通过；严格债务检查仍只报告已登记的 V3 训练函数 `C901=1`、`PLR0913=1`，本批未修改该文件。

## Residual Risks

- 旧 C3 研究制品和旧字段不做隐式迁移；遇到时将失败关闭，需从已验证父证据重新封存。
- 已有 `score_h0_*` 和 V1 特征工件字符串属于不可变历史解码/评分工件身份，依照版本命名边界保留只读兼容，本批不复制或晋级该命名模式。
- `docs/reports/` 和 `docs/changelog/archive/` 中的不可变历史事实保留原始代号，不属于活动命名契约。
- 本批不改变评分、候选、风险、融合、Top6、冻结、DeepSeek 预算、生产授权或 Web 公开 schema。
- `Regression-Key: semantic-responsibility-naming-cleanup`。
