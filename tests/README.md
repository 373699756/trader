# 测试组织与运行

测试按 `tests/<业务或边界>/<测试类型>/<功能>/test_*.py` 组织。功能层只在文件较多时继续拆分，不机械复制源码目录。

| 目录 | 覆盖职责 | 业务标记 |
| --- | --- | --- |
| `download/` | 行情、公司资料、历史下载与发布 | `history` |
| `training/` | 样本、模型拟合、研究与评估 | `train` |
| `recommendation/` | 候选、评分、风险、冻结与运行调度 | `recommendation` |
| `http_api/`、`web/` | 只读 API、页面与前端行为 | `recommendation` |
| `infra/` | 跨业务技术 primitive | `crosscut` |
| `system/` | 组合根、跨业务合同、集成及全仓约束 | `crosscut` |
| `tooling/` | 工程工具与统一工具入口 | `crosscut` |

第二层目录为 `unit`、`component`、`contract`、`integration`、`performance` 或 `js`，对应同名测试标记。根 `conftest.py` 从目录推导业务和类型；未知目录直接报错，不维护单文件归属名单。

## 共享代码和夹具

可复用工厂、假实现与数据构造放在 `helpers/<业务>/`，跨业务最小共用代码放在 `helpers/shared/`。测试不从其它 `test_*.py` 导入辅助代码。静态输入文件放在 `fixtures/`，辅助模块不使用 `test_` 前缀。

局部 `conftest.py` 只注册该业务或功能使用的 fixture。跨业务 fixture 可在根注册，产品及重依赖延迟导入。可变对象、数据库和线程池保持隔离；只有可安全共享的准备结果才提升 fixture 作用域。扫描源码的缓存只用于整轮保持不变的仓库文件，修改临时文件的测试自行读取。

## 常用入口

Makefile 是测试命令的唯一维护位置，默认使用 4 个 worker 和 `loadgroup`。业务入口直接传目录，避免收集其它业务；按类型运行时使用类型标记。

```bash
make test
make test-history
make test-train
make test-recommendation
make test-unit
make test-component
make test-contract
make test-integration
make test-full
```

`test` 与 `test-fast` 排除 `slow`。`test-full` 包含慢测，仍遵守下述格式工具授权边界。需要调整本轮资源时可显式使用 `PYTEST_WORKERS=1` 或 `PYTEST_DISTRIBUTION=load`，例如 `make test-train PYTEST_WORKERS=1`。

慢测在实际模块或函数上标记。`slow_history`、`slow_migration`、`slow_runtime`、`slow_supplier`、`slow_static`、`slow_training` 自动附加 `slow`；`performance/` 中的测试也自动附加 `slow`。`test-recommendation-runtime`、`test-recommendation-suppliers`、`test-static-contracts` 分别选择相应专项。运行调度和供应商专项同时选择推荐与共享 infra 中对应的 `slow_runtime`、`slow_supplier` 用例，覆盖共享 worker 生命周期和行情缓存。

需要按测试路径或查看耗时时，直接使用现有 pytest：

```bash
.venv/bin/python3 -m pytest tests/download --durations=20
.venv/bin/python3 -m pytest tests/recommendation -m "not slow" --collect-only
```

训练准备使用 `xdist_group` 的模块由 `loadgroup` 放在同一 worker，复用 module fixture。分组与并发变化须比较真实耗时、资源和通过结果；不以测试文件减少或夹具性能代表真实生产收益。

## 格式与代码变换专项授权

格式检查、格式化、重命名等代码变换及相关工具入口、写入保护回归逐函数使用 `format_tools` 标记。仅在内存验证命名规则、源码审计和 manifest 解析的用例默认保留，不随模块整体排除。

默认收集后排除已标记的专项，并报告 `format_tools deselected` 数量；并行运行按 nodeid 去重，避免把四个 worker 的收集数相加。`-m format_tools` 不构成运行授权。

只有用户明确授权运行对应格式或代码变换工具后，才可显式添加 `--run-format-tools`。授权隔离自身的回归使用假对象验证，不调用这些工具。

默认测试通过表示本轮已选择用例通过，不包含被排除的格式工具专项，也不能据此宣称完整发布门禁通过。仅运行单业务、快速集或离线性能夹具时，须如实报告覆盖范围。
