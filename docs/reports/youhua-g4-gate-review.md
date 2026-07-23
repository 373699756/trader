# youhua G4 阶段 4 完成条件复核记录

状态：G4 已发布。A4、B4、C4、D4 的阶段 4 报告均已具备 `ready_for_gate=yes`
证据，D4 交给 A 的 P6 接纳原子性事项已由 A4-F04 关闭；本批复验完整质量、正确性、
性能、内存、兼容、仓库外 wheel 和三档桌面，不启动 A5。

## 1. 工作树封存与批次范围

| 项 | 值 |
| --- | --- |
| codex_and_phase | Codex A / G4 gate review |
| start_head | `8e7ab24985ff73f7ec54cf62c9440f97b5d179c6` |
| start_upstream | `origin/branch` / `8e7ab24985ff73f7ec54cf62c9440f97b5d179c6` |
| CONTRACT_BASE | `45bd2fab992d36eb873b7c448fbd9739f0cad43c` |
| start_worktree | clean |
| 本批范围 | G4 整节：阶段 4 handoff 汇总、D4 跨 owner 阻塞关闭确认、全量门禁复验、报告、Review、提交和推送 |
| 后续边界 | 不进入 A5，不创建 PR、tag 或 release |

复验期间工作树先出现并行的 columnar/单测修改，最终 Review 时又出现 B5/D5 报告、
publisher/SSE/Web 回归等并行修改和暂存项。本批保留这些修改，不把它们纳入 G4 提交；
最终提交从 start HEAD 仅叠加 G4 的 CHANGELOG、报告和两份契约测试。正式性能输出因此
如实记录 `dirty=true`，固定提交身份仍为上述 start HEAD。

## 2. 阶段 4 交接材料与阻塞关闭

| Codex | 材料 | 原报告状态 | G4 复核 |
| --- | --- | --- | --- |
| A | `docs/reports/youhua-a4-acceptance.md` | `yes; A4.1-A4.6 complete` | 正确性、故障、质量、兼容、性能、内存和资源证据齐全 |
| B | `tests/fixtures/market_data/youhua_b4/report_to_a.md` | `yes` | scalar/columnar 等价、绝对预算、三板评分和 100 tick 内存通过 |
| C | `tests/fixtures/deepseek/youhua_c4/report_to_a.md` | `yes` | 58/66/71 软目标、188 硬上限、重试/修复物理计数和保守合并通过 |
| D | `docs/reports/youhua-d1-p6-web.md` 第 11 节 | `yes; D4-owned gates pass` | P6/SSE/API、增量传输和 Firefox 三档通过；原报告唯一跨 owner 阻塞已关闭 |

D4 要求 A 在集成层把 P6 接纳前置，避免拒绝后 RuntimeState、session、checkpoint 和 SSE
身份前进。A4-F04 已把同步、worker、正式冻结、收盘恢复和重启恢复统一接入
`admit_snapshot_to_p6()`；拒绝时保留旧 P6/RuntimeState，不写 session/checkpoint，也不广播
SSE。因此 D4 报告中的 G4 跨 owner 阻塞已关闭，没有遗留 owner 不明的失败。

## 3. 正确性、故障与质量门禁

- 完整测试继续覆盖架构 AST、`create_app()` 无线程/网络/数据库/写文件副作用、固定融合
  `83.40`、scalar/columnar 业务哈希等价、DeepSeek 预算并发/重试、冻结恢复/哈希一致性、
  SSE 游标/慢客户端、P6-first 接纳和本地降级。
- C4 定向 7 项、D4 Web 性能回归及 Node dashboard 状态机契约再次通过；正常增量更新没有
  完整 recommendation GET。
- `make format-check`、`make lint`、`make type-check`、`make test`、`make package`
  最终全部通过；没有为发布 G4 修改产品、架构、策略、API、配置或持久化 schema。

## 4. 性能复验

正式 `trader-cli perf-check --suite all` 在固定 v17 离线 fixture 上通过 16 项、零网络、
100 tick 分配增长 `0.0%`。主要 P95 为：标准化 `6.282ms`、合并 `3.451ms`、统一快照
`5.528ms`、三板墙钟 `1.731ms`、三策略评分 `1.810ms`、P6/SSE `0.041ms`；无绝对或相对
失败。

B4 固定 5500 行/360 候选/100 tick 专业 runner 的最终稳定样本通过：

| 指标 | 最终结果 | 门限 | 结论 |
| --- | ---: | ---: | --- |
| scalar -> columnar process-CPU P95 改善 | `32.404%` | `>=20%` | 通过 |
| 标准化 P95 | `169.247ms` | `800ms` | 通过 |
| 两源合并 P95 | `675.536ms` | `1000ms` | 通过 |
| canonical snapshot P95 | `1219.953ms` | `1500ms` | 通过 |
| 100 tick 分配增长 | `0.0%` | `<=20%` | 通过 |

共享宿主的前三次预跑分别出现绝对门限失败：`475.627/1360.700/3760.363ms`、
`201.664/797.289/1604.805ms` 和 `270.760/1006.602/2037.569ms`
（标准化/合并/canonical），但业务哈希、相对改善和内存均保持正确。调高进程优先级的请求
被宿主拒绝，最终通过样本实际仍按普通优先级运行；未删除失败样本或把单次时延泛化为稳定
外部服务性能。

## 5. 内存证据

同进程固定压力场景同时保留两套 5500 行 scalar/columnar epoch、360 dirty code、六池约
70% 载荷、8 股 DeepSeek 批次、20 日 60 个 P6 视图、冷读三策略、12 次原子替换和 32 个
慢客户端。

| 池/对象 | 逻辑字节 | 上限字节 |
| --- | ---: | ---: |
| P1 observation | `93,955,976` | `134,217,728` |
| P2 canonical | `41,105,520` | `58,720,256` |
| P3 features | `17,617,812` | `25,165,824` |
| P4 local scoring | `11,745,828` | `16,777,216` |
| P5 review | `8,809,764` | `12,582,912` |
| P6 delivery cache pool | `8,809,828` | `12,582,912` |
| P6 delivery resident views | `2,806,645` | 已计入综合逻辑量 |
| Polars 两个 epoch 原生估算 | `1,282,816` | 已计入综合逻辑量 |

| 综合指标 | 结果 | 上限 | 结论 |
| --- | ---: | ---: | --- |
| 综合逻辑字节 | `205,468,511` | `260,046,848` | 通过 |
| 当前 RSS | `370,069,504` | 信息项 | 通过 |
| peak RSS | `387,112,960` | `402,653,184`（384 MiB） | 通过 |
| USS | `312,655,872` | 信息项 | 通过 |
| Polars 原生估算 | `1,282,816` | 已计入逻辑量 | 通过 |

峰值原因是双 scalar/columnar epoch、六个有界缓存池、DeepSeek 最大批次、P6 驻留/冷读/
原子替换和未排空慢客户端队列在同一测量点保持强引用；网络调用为零，未用
`tracemalloc` 单独代替进程与 Polars 口径。

## 6. Python、wheel 与三档桌面

- 宿主 Python `3.14.4` 实际执行全部门禁；Ruff `py310`、mypy
  `python_version=3.10` 和 wheel `Requires-Python >=3.10,<3.15` 静态覆盖 3.10-3.14。
  宿主未安装 3.10-3.13，未声称这些版本已本机运行。
- `make package` 首次在受限沙箱内因隔离构建无法获取 setuptools 失败，获准联网后成功。
  从只含 start HEAD + G4 四个文件的仓库外树重新执行完整门禁和构建；安装该 wheel 后，
  从安装目标导入 `trader`、执行 `trader-cli --help` 与绝对路径配置校验、读取模板/4 CSS/
  2 JavaScript/2 SVG 共 9 项资源，并通过 `pip check`。
- Firefox `152.0.4` 在精确内容视口 `1280x720`、`1440x900`、`1920x1080` 重新验收固定
  18 行投影、长错误、详情抽屉和持续 SSE。三档均为 18 个唯一代码、无白屏、页面级横向
  溢出、关键同级重叠或页面 JavaScript 错误；抽屉有 3 个分区并完全位于视口内。两次有效
  patch 的 recommendation request/full response/resync 增量均为 `0`，patch 应用增量为
  `2`。截图为 `/tmp/trader-g4-1280x720.png`、`/tmp/trader-g4-1440x900.png` 和
  `/tmp/trader-g4-1920x1080.png`。

## 7. Review 结论与后续边界

G4 完整 diff 已按正确性、冻结一致性、并发/资源生命周期、错误降级、安全、类型、API
兼容、桌面 UI 和可安装性复核；本批只新增门禁发布记录和相应契约/CHANGELOG，未发现死代码、
重复实现、遗留 TODO、密钥或运行产物。已知剩余风险仅为共享宿主性能抖动、未安装
Python 3.10-3.13 的本机矩阵、真实供应商/DeepSeek 网络时延，以及 Firefox 自身非产品性的
SWGL warning；这些风险均未改变固定离线门禁结论。

阶段 4 到此完成。A5 必须等待下一次用户继续指令，本批不提前执行终审阶段。

ready_for_gate: `yes; G4 is published and A5 has not started`
