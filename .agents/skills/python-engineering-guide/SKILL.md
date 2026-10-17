---
name: python-engineering-guide
description: "Design, scaffold, modernize, implement, and review maintainable Python projects with an evidence-backed engineering guide and coding standard. Use when Codex needs to start or restructure a Python library, CLI, service, data/automation project; choose package boundaries or dependency direction; define pyproject, testing, typing, linting, CI, logging, error-handling, and release conventions; write or refactor Python code; review a Python change; or produce an engineering checklist."
---

# Python 工程设计与编码规范

## 核心原则

先服从用户目标和仓库现有约定，再应用本 skill 的默认值。保留已经有效的结构与工具，不为“统一风格”制造无关重构。把上游指南当作证据和启发，不把任何单一公司的规则当作不可变真理。

## 按需读取资料

- 设计、新建、拆分或现代化工程时，必须读取 [references/engineering-design.md](references/engineering-design.md)。
- 编写、修改或审查 Python 代码时，必须读取 [references/coding-standard.md](references/coding-standard.md)。
- 涉及规范出处、许可证、星数、旧建议或版本时，读取 [references/sources.md](references/sources.md)。
- 同一任务同时涉及架构和代码时，同时读取前两份资料；不要只检查格式而忽略依赖方向、状态和错误边界。

## 工作流程

### 1. 建立项目上下文

1. 查找并阅读 `AGENTS.md`、`CONTRIBUTING*`、`README*`、`pyproject.toml`、锁文件、CI 配置和测试配置。
2. 确认项目类型、支持的 Python 版本、发布方式、运行环境和既有工具链。
3. 用现有代码和测试识别实际边界；不要仅根据目录名猜测架构。
4. 记录冲突：用户要求优先，其次是仓库强制规则，再其次才是本 skill 的默认建议。

### 2. 选择与规模匹配的设计

- 单文件脚本：保持简单，分离纯逻辑与 I/O，避免套用多层架构。
- 可复用库：稳定公共 API，隔离可选依赖，验证构建与安装产物。
- CLI 或服务：把入口、用例/业务逻辑和外部适配器分开，在组合根注入依赖。
- 数据或自动化任务：显式描述输入、输出、幂等性、重试、检查点和数据质量约束。
- 仅在存在多个入口、复杂业务规则或可替换外部系统时引入 service layer、repository、ports/adapters 等模式。

### 3. 形成可验证的方案

对设计或重构任务，至少给出：

1. 当前约束和目标。
2. 模块职责、公共接口与依赖方向。
3. 状态、配置、错误、资源和并发模型。
4. 测试层次、质量门禁和发布方式。
5. 分阶段迁移计划、兼容性与回退策略。

设计应能映射到具体文件、接口和测试。避免只给“高内聚、低耦合”之类无法验收的口号。

### 4. 实现最小一致变更

1. 先处理契约和边界，再处理内部实现。
2. 保持公共 API、序列化格式、数据库结构和 CLI 行为兼容，除非任务明确要求破坏性修改。
3. 同步更新测试、类型、文档和配置；不要留下半迁移状态。
4. 不主动替换仓库已有 formatter、linter、type checker、测试框架或依赖管理器。
5. 新增工具前说明收益、迁移成本和与现有工具的重叠。

### 5. 分层验证

优先运行仓库声明的命令。若没有统一命令，则按变更范围逐步执行：

1. 受影响的最小测试集。
2. formatter/linter 检查。
3. type checker。
4. 完整测试或集成测试。
5. 对可发布包执行构建，并在干净环境验证安装和导入。

不要声称未运行的检查已经通过。无法运行时，报告具体阻碍和仍需验证的风险。

## 交付格式

### 设计结果

先给推荐方案，再说明关键权衡。包含目标结构、依赖方向、核心接口、质量门禁和迁移顺序；只在关系复杂时画图。

### 实现结果

说明完成的行为变化、关键文件、验证结果和剩余风险。避免逐文件流水账。

### 审查结果

按严重度列出可执行问题，给出文件位置、失败场景和最小修复方向。优先正确性、安全性、兼容性、资源生命周期和可测试性，最后才是纯样式问题。
