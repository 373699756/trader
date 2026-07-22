# youhua A1 基线与契约冻结报告

状态：A1.x 已完成本地基线采集与契约冻结；已收到 B1 报告，G1 未发布，等待 C1/D1 标准报告。

## 1. 工作树封存

| 项 | 值 |
| --- | --- |
| codex_and_phase | A1.x |
| branch | `branch` |
| upstream | `origin/branch` |
| base_commit | `777e73d445f88c165126d1a09d02b833453b9d3e` |
| upstream_commit | `777e73d445f88c165126d1a09d02b833453b9d3e` |
| start_worktree | clean |
| owner 范围 | A 独占权威文档、公共契约、报告、契约测试、CHANGELOG、集成提交和推送 |
| B/C/D 内部算法 | 未执行、未修改 |

## 2. A1.2 当前基线

| 命令或项目 | 结果 |
| --- | --- |
| `make format-check` | 通过，220 files already formatted |
| `make lint` | 失败；严格债务计数基线漂移，expected `C901=37,N818=5,PLR0911=15,PLR0912=15,PLR0913=55,PLR0915=12`，actual `C901=39,N818=5,PLR0911=15,PLR0912=16,PLR0913=55,PLR0915=11` |
| `make type-check` | 通过，159 source files |
| `make test` | A1 修改前失败 6 项；A1 文档白名单修复后仍失败 5 项，涉及 application port `Mapping[str, object]`、bootstrap 构造参数、历史报价 overlay 和 final candidate cadence 计数 |
| `make package` | 沙箱内失败于构建依赖联网；提升权限后通过，生成 wheel/sdist |
| `trader-cli perf-check --suite all` | 通过，固定 fixture，无外部网络，`network_calls=0` |
| v15 market-data perf | 通过；5500 行标准化 P95 `99.263ms`，两源合并 P95 `528.437ms`，统一快照 P95 `781.095ms` |
| v16 board-scoring perf | 通过；单板预选 P95 `43.483ms`，单板本地评分 P95 `13.671ms`，三板墙钟 P95 `264.968ms`，全局选择 P95 `6.315ms` |
| 三档 Web 截图 | 1280x720、1440x900、1920x1080 均非白屏；无运行态页面显示 `not_ready`，SSE 因未注入 publisher 返回 503 |

## 3. 资源与内存基线

| 指标 | 当前观测 |
| --- | ---: |
| `settings_cache_total_bytes` | `260046848` |
| `settings_pool_total_bytes` | `268435456` |
| `settings_runtime_reserve_bytes` | `8388608` |
| `performance_memory_cache_total_bytes` | `268435456` |
| `python_traced_bytes` | `85927` |
| `python_traced_peak_bytes` | `105378` |
| `process_peak_rss_bytes` | `36175872` |
| `process_uss_bytes` | 不可用 |
| `perf-check` 100 tick allocation growth | `0.0%` |

`create_app()` 仅做无副作用 smoke；未启动真实 pipeline，因此 scalar 启动、P1 预热、全市场评分、
DeepSeek 批次、P6 发布和完整 100 tick 的 RSS/USS/Polars 原生估算仍需在阶段 2-4 集成门禁
补齐。当前配置仍暴露旧 `256 MiB` performance memory 字段，A1 已冻结新的
`248 MiB 逻辑缓存 + 384 MiB 进程峰值 RSS` 双层契约，具体配置与状态字段实现留给 A2。

## 4. 公共接缝与 owner 清单

公共基线版本：`youhua_contract_base_v1`。

| 接缝 | 版本 | owner | 状态 |
| --- | --- | --- | --- |
| P3 -> P4 | `p3_p4_feature_snapshot_market_change_set_v1` | A 公共 schema；B producer | 已冻结文档契约 |
| P4 -> P5 | `p4_p5_high_value_review_manifest_v1` | A 公共 schema；C consumer | 已冻结文档契约 |
| P4/P5 -> P6 | `p4p5_p6_projection_event_v1`、`p6_overlay_event_v1` | A 公共 schema；D consumer | 已冻结文档契约 |
| DeepSeek facts | `deepseek_v4_review_facts_v1` | A 公共 schema；C 实现 | 已冻结策略契约 |

B/C/D 的独占生产范围仍按 `docs/plan_youhua.md` 第 2.1 节执行。B/C/D 需要公共接口变化时，
只提交接口申请；A 是唯一公共文件修改者和集成提交者。

## 5. G1 等待项

| 报告 | 状态 |
| --- | --- |
| B1 P1-P3 盘点报告 | 已收到，路径 `tests/fixtures/market_data/youhua_b1/report_to_a.md`，`ready_for_gate=yes` |
| C1 DeepSeek 盘点报告 | 未收到 |
| D1 P6/Web 盘点报告 | 未收到 |
| CONTRACT_BASE | 未发布 |
| ready_for_gate | no |

收到 C1/D1 后，A 将汇总 B1/C1/D1 接口申请、唯一 owner 清单、schema/version 清单和合并顺序，
再发布 `CONTRACT_BASE=<commit>` 与 G1。
