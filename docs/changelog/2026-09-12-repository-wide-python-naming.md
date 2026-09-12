# Repository-wide Python naming consistency

## User request

全局 Review 并修复 Python 命名类别不一致和语义职责不清的问题；同时纳入 `ElementTree`
导入、严格命名门禁、存储所有权命名、权威文档和独立交付记录，保留用户已有未提交修改。

## Evidence and cause

- `import xml.etree.ElementTree as element_tree` 把类型模块别名降成了普通 `snake_case`，与导入对象的命名类别不一致。
- 原严格质量检查只对活动源码执行综合 Ruff 规则，没有将命名规则独立覆盖到 `scripts` 和 `tests`。
- `history_archive`同时指向月度行情归档和回溯筛选归档，多个 `*Store`、H1 短名、Tomorrow
  历史风险短名和评分档位内的重复公共类名需要通过导入路径才能分辨职责。
- `Regression-Key: semantic-responsibility-naming-cleanup`。

## Added

- 严格质量检查对 `src/trader`、`scripts` 和 `tests` 统一执行 Ruff `N`，覆盖类/异常、
  函数/参数/变量、常量、模块和导入别名类别。
- 专业命名 AST 契约新增旧路径、旧公共名、`store/stored` 项目符号和公共类名唯一性门禁。
- Tomorrow V3 训练内存证据独立类型模块 `training_memory_evidence.py`。

## Changed

- 测试改为 `from xml.etree import ElementTree`，并使用 `ElementTree.fromstring(...)`。
- 历史月库使用 `HistoryRevision`、`history_revision_codec.py` 和 `SQLiteHistoryArchiveReader`；
  回溯筛选库改为 `HistoricalScreeningArchive*` 与 `historical_screening_archive.py`，避免同一
  `HistoryArchive` 名称承载两种不同数据所有权。
- 历史压实全链路统一为 `HistoryArchiveRepack*`，包括激活日志、源文件身份、分区证据、
  读写函数、fence 原因码和 `repack_baostock_history_archive.py` 入口。
- 项目自有的持久化所有者按行为改为 `Archive` 或 `Repository`；局部变量使用
  `archive`、`repository`、`persisted_*` 或 `recorded_*`，不保留旧符号别名。
- H1 点时数据、Tomorrow 历史风险、训练输入兼容性、分区校验进度和日收盘 H1 证据改为完整
  业务前缀；V1/V2 的 Tomorrow 工件和预测器显式携带评分档位。
- 权威工程设计、回溯文档、SQLite 运维说明和 Makefile 入口同步到新名称。

## Fixed

- 消除驼峰类别对象被别名为小写下划线、同名公共类跨模块碰撞、以及依赖路径才能判断
  归档、工件或仓储职责的问题。
- 存储职责命名门禁现在同时检查源码、脚本和测试的类、函数、参数及普通变量。

## Removed

- 删除旧模块路径和旧公共 Python 符号，不提供兼容别名、forwarder、双实现或隐藏 fallback。
- 不改名 SQLite 表、运行目录、JSON 字段、schema identity 或 content-hash 输入；不修改评分、
  风险、冻结、DeepSeek 预算、公开 API/SSE/Web schema 或业务行为。

## Verification

- 专业命名契约与严格 refactor/naming 检查通过。
- 历史月库、repack/fence、回溯筛选库、研究工件、Tomorrow 风险与训练、V1/V2/V3 评分工件、
  运行诊断和脚本入口定向回归通过。
- `make format-check`、`make lint`、`make type-check`、`make test` 和 `make package` 全量门禁通过。
- 从仓库外安装新 wheel 后的包导入、`trader-cli` 与 10 项模板/CSS/JavaScript/图标资源验证通过。

## Residual Risks

- 本批不读写真实运行库，不触发供应商网络或背景线程；持久化 schema/hash 字面和历史交付记录保持原样。
- 用户未跟踪的 `docs/v1v2.md` 与 `data/historyless/` 原样保留，不纳入本批提交。
