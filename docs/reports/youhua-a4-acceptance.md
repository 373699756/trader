# youhua A4 全量验收与问题闭环记录

状态：A4.1-A4.6 已完成。B4/C4/D4 标准报告均为 `ready_for_gate=yes`，A4-F01 与
A4-F04 已修复并从失败用例开始复验，完整质量、打包、性能、资源和桌面门禁通过。
本批只交付 A4 handoff，不发布 G4，不进入 A5。

## 1. 工作树封存与范围

| 项 | 值 |
| --- | --- |
| codex_and_phase | Codex A / A4.1-A4.6 |
| start_head | `7a8a0282d025cbc23fffff5736e94c2d1bf883e0` |
| start_upstream | `origin/branch` / `7a8a0282d025cbc23fffff5736e94c2d1bf883e0` |
| start_worktree | clean |
| integrated_phase4_bases | B4 `69de151c79ab9502d0742bfc52601c455ad26a2a`; D4 `cad5910a15ad21d1990f4322a8803cc6805ac1dc`; C4 patch/report |
| A parent before final commit | `cad5910a15ad21d1990f4322a8803cc6805ac1dc` |
| A 本次范围 | 跨域正确性/故障、专业 handoff 汇总、公共 P6 原子接线、完整门禁、性能/内存汇总、报告、Review、提交和推送 |

B/D 在 A4 执行期间分别完成独立提交并推送；A 保留其提交历史。C4 的预算/保守合并修复、
7 项固定回归和标准报告由 A4 集成提交归并。

## 2. A4.1 正确性

- domain/application/final-acceptance/full-day-shadow 回归覆盖 local/deepseek/risk/final score、
  动作、稳定排名、TopK、业务 JSON、冻结输入重放及同步/worker 投影等价；
- 固定融合向量继续由 `tests/unit/domain/test_fusion.py` 验证为 `83.40`；
- `test_recorded_full_day_shadow_is_deterministic_and_freezes_real_repository` 的两次运行保持相同
  manifest 与 JSON SHA-256；
- C4 证明传闻不加分、重复事件不形成双来源、Pro 不提高主审维度且不能解除硬过滤/下行保护；
- B4 scalar/columnar 固定快照 SHA-256 相同；A4 并存 runner 再次得到相同业务哈希
  `af791c795eb6447976b0542986c60bf85f229d771146d2b046a4abbcac9436e3`。

## 3. A4.2 跨域故障与修复

已通过 provider 全源失败保留候选、DeepSeek worker/all-fail 本地降级、429/重试/schema 修复
原子计数、预算重启回收、持久化崩溃与哈希隔离、冻结边界/重启、旧日期/旧 epoch、SSE 游标
过期/超前及慢客户端丢弃。

### A4-F01：Polars 失败回退标量行情

| 字段 | 内容 |
| --- | --- |
| owner | Codex B |
| 状态 | closed |
| 修复 | 列式投影或合并抛出 Polars/运行时类型异常时保留有效 scalar canonical snapshot，生成完整 invalidation change set，并记录 `columnar_projection_failed:scalar_fallback` 或 `columnar_merge_failed:scalar_fallback` |
| 复验 | 两个精确失败注入、48 项 B 定向单元、122 项行情组件、B4 runner 与完整测试通过 |

### A4-F04：P6 拒绝后的公开身份原子性

| 字段 | 内容 |
| --- | --- |
| owner | Codex D（P6 显式拒绝）+ Codex A（pipeline 公共接线） |
| 状态 | closed |
| 修复 | P6 写端口返回显式接纳布尔值，`snapshot_publication.admit_snapshot_to_p6()` 同时处理超限异常和迟到/冻结替换/旧日期拒绝；拒绝时记录 `p6_snapshot_rejections` 与 `p6_snapshot_rejected`，保留旧 P6/RuntimeState，不写 session/checkpoint，不发 SSE。同步、worker、冻结、收盘恢复和重启恢复共用该接缝 |
| 正式冻结 | 不可变冻结先持久化；若随后 P6 拒绝，不标记运行态冻结、不消费检查点、不广播，正式记录仍保持不可覆盖 |
| 复验 | 同步与 worker 超限投影、正式冻结与收盘冷启动拒绝、重启加载超限正式记录、重启冻结替换静默拒绝、P6/publisher 全域及完整测试通过 |

## 4. A4.3-A4.4 质量、兼容和桌面

- `make format-check`、`make lint`、`make type-check`、`make test`、`make package` 全部通过；
  mypy 检查 164 个活动源码文件，严格债务保持
  `C901=38/N818=5/PLR0911=15/PLR0912=16/PLR0913=55/PLR0915=11`。
- 架构契约只把含真实 `*.py` 的目录视作活动源码，避免本地 `__pycache__` 伪失败，同时仍
  零容忍退休业务实现；`create_app()` 无副作用契约包含在完整测试中。
- 新增 `tests/fixtures/performance/v17/manifest.json`，使正式 `trader-cli perf-check --suite all`
  可直接使用仓库固定 fixture；16 个指标通过、零网络、分配增长 `0.0%`。
- `trader-cli --help` 与 `validate-config` 通过。仓库外 `/tmp` 安装最终 wheel 后，从安装目标
  导入 `trader`、执行 CLI、校验配置、读取模板/4 CSS/2 JavaScript/2 SVG 共 9 项资源，
  `pip check` 无断裂依赖。
- 宿主 Python 3.14.4 实际运行全部门禁；Ruff `py310`、mypy `python_version=3.10` 与 wheel
  `Requires-Python >=3.10,<3.15` 静态覆盖 3.10-3.14。宿主未安装 3.10-3.13 解释器，未伪造
  这些版本的本机运行结果。
- D4 使用 Firefox 152 在 1280x720、1440x900、1920x1080 对 18 行真实投影、长错误、详情
  抽屉和持续 SSE 完成检查：无白屏、重叠、页面级横向溢出、脚本错误或布局跳动。

## 5. A4.5 性能与资源

### 专业门禁复跑

- B4：scalar 到 columnar 的 process-CPU P95 改善 `35.544%`；标准化/两源合并/统一快照
  P95 为 `169.557/566.678/1143.527ms`；100 tick 逻辑 `29,661,328B`、峰值 RSS
  `289,492,992B`、USS `257,961,984B`、增长 `0.0%`。
- v16：单板预选/评分、三板三策略墙钟、全局选择 P95 为
  `32.012/4.585/322.631/3.728ms`。
- C4：正常主审 `58`、含 Pro `66`、含 emergency `71`，224 并发探针只接纳 `188`；
  429 重试、schema 修复、缓存和跨策略复用计数正确，8 股结果重发布小于 1 秒。
- D4 最终复跑：P6->SSE 入队 P95 `3.556ms`；当前/驻留/ETag/日期/状态 API P95 为
  `1.436/1.385/1.019/0.960/0.559ms`；增量传输节省 `89.655%`。

### A4 同进程近上限并存

`tests/performance/run_youhua_a4_integration.py` 在无网络单进程内同时驻留：5500 行 P1 预热、
P2/P3 全量 scalar/columnar 快照及列式批次、新旧 epoch 与 360 dirty code、六个 P1-P6 字节池各约
`70%`、8 股 DeepSeek 批次、20 日 60 个 P6 驻留视图、一次冷读三策略预取、12 次原子替换和
32 个不消费队列的慢客户端。

| 指标 | 结果 | 上限 | 结论 |
| --- | ---: | ---: | --- |
| P1-P6 + 双快照 + Polars + P6/SSE 逻辑字节 | `205,468,511` | `260,046,848` | 通过 |
| 当前 RSS | `371,552,256` | 信息项 | 通过 |
| 峰值 RSS | `387,452,928` | `402,653,184` | 通过 |
| 结束 USS | `358,887,424` | 信息项 | 通过 |
| Polars 估算 | `1,282,816` | 已计入逻辑总量 | 通过 |
| 慢客户端丢弃 / 冷读次数 | `32 / 1` | 必须均大于 0 | 通过 |

峰值原因是两套 5500 行 scalar/columnar 快照和列式 epoch、六池 70% 字节载荷、DeepSeek
最大批次、P6 驻留/冷读/替换和慢客户端队列在测量点保持强引用并存；不是网络请求或无界增长。

## 6. A4.6 问题闭环和退出状态

| ID | owner | 状态 | 关闭证据 |
| --- | --- | --- | --- |
| A4-F01 | B | closed | scalar fallback 精确注入与 B 全域复验通过 |
| A4-F02 | A | closed | 五项 make、wheel/CLI/资源、静态 Python 范围和 D4 三档桌面通过 |
| A4-F03 | A + B/C/D | closed | 专业报告齐全，同进程逻辑/RSS 总门禁通过 |
| A4-F04 | D + A | closed | P6-first 接线及同步/worker/冻结/收盘/重启失败回归通过 |

当前没有已知未解决 A4 缺陷。外部剩余风险只有本机缺少 Python 3.10-3.13 的实际运行矩阵，
以及真实供应商/DeepSeek 网络延迟不属于固定离线验收；二者均未被写成已实测结论。

ready_for_gate: `yes; A4.1-A4.6 complete, G4 is not published, and A5 has not started`
