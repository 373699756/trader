# qfq 共享数据优化计划

## 1. 目标

将 `data/qfq/v2` 和 `data/qfq/v3` 从 SQLite 分片改为适合 Git 跨 PC 共享的按月 JSONL 文本分片，同时保持实时荐股、`run.sh qfq_download`、Tencent 有界并发更新、15:10 更新和断点续传行为。

完整 `data/history/baostock` 仍是训练、研究和结算事实源；qfq 只保存 V2/V3 的滚动派生窗口。

## 2. 分片布局与命名

```text
data/qfq/
  v2/
    2026-10-c000000-000511.jsonl
    2026-10-c000512-001023.jsonl
    catalog.json
    update-progress.json
  v3/
    2026-10-c000000-000511.jsonl
    catalog.json
    update-progress.json
```

命名含义：`2026-10` 是交易月份，`c000000-000511` 是代码范围，`p01`/`p02` 是同一范围超出容量后的稳定分片序号。例如：`2026-10-c000000-000511-p01.jsonl`。

不再使用 `range-00002-0000.sqlite3`、`00002.sqlite3` 或按股票维护的 `index.json` 路由映射。

## 3. JSONL 内容合同

每行表示一只股票一个交易日的完整 raw/qfq 配对：

```json
{"code":"000001","day":"2026-10-08","status":"complete","raw":[...],"qfq":[...]}
```

约束：

- 文件内按 `code, day` 升序排列；
- 同一 `(code, day)` 不得重复；
- raw 和 qfq 必须来自同一股票、同一交易日、同一供应商事实；
- 缺失或损坏行 fail-closed，不得进入评分；
- 使用紧凑 ASCII JSON，不使用 gzip/zstd，保留 Git 文本差异能力；
- 输入边界解码为类型对象，内部业务不传递 JSON 字典。

## 4. 容量规划

```text
目标大小：4~6 MB/文件
预拆分阈值：6 MB
硬上限：8 MB
代码范围：每组 512 只股票
```

V2 预计保留约 12 个月，V3 预计保留约 3 个月。固定代码范围优先保持稳定，不对历史文件重新平衡；某个分片超过 6 MB 时只追加 `p01`、`p02`，不得重写无关文件。

这样单文件远低于 GitHub 50 MB 警告线，同时避免按交易日产生约 250/60 个小文件。每天通常只修改当前月份的文件，每月新增一组文件。

## 5. catalog.json

每个档位保留一个小型 `catalog.json`，只保存分片元数据，不保存逐股票路由：

```json
{
  "schema": 1,
  "profile": "v2",
  "sessions": 251,
  "partitions": [
    {
      "file": "2026-10-c000000-000511.jsonl",
      "month": "2026-10",
      "code_start": "000000",
      "code_end": "000511",
      "row_count": 11220,
      "code_count": 510,
      "latest_day": "2026-10-08",
      "bytes": 4289120,
      "sha256": "..."
    }
  ]
}
```

catalog 用于发现月份、校验文件范围、行数、大小、最新交易日和摘要。只有分片、行数、摘要或最新日期变化时才原子写入。

## 6. 读取与本地索引

新增文本窗口读取器，实现现有 `QfqWindowPort` 和 `PublishedHistoryReadPort` 的同等接口，推荐和下载业务不感知存储格式。

读取流程：

1. 通过 catalog 找到覆盖目标窗口的月份；
2. 按代码范围筛选候选 JSONL 文件；
3. 使用本地偏移索引定位代码块；
4. 读取目标股票的 251/61 个交易日；
5. 校验顺序、配对、窗口长度和内容摘要；
6. 返回现有 `PublishedHistoryWindow` 类型。

本地偏移索引不提交 Git：

```text
data/qfq/v2/.offsets.json
data/qfq/v3/.offsets.json
```

索引记录文件摘要和代码块起始字节偏移；文件摘要变化时重建。索引损坏时退化为扫描 JSONL，不能改变结果正确性。

## 7. 更新与续传

`./run.sh qfq_download` 保持以下行为：

- 从已发布 history 读取交易日历和股票集合；逐股 raw/qfq 行情使用 Tencent，默认 8 个有界 worker；
- 上海时间 15:10 后更新，晚启动补执行；
- history 优先；缺少或不完整窗口由 Tencent 重取，供应商失败保留旧窗口并记录 pending；
- V2/V3 复用一次供应商结果；
- 两档均持久化成功后才确认股票完成；
- 支持取消、重启、失败重试和断点续传。

续传文件改为：

```text
data/qfq/v2/update-progress.json
data/qfq/v3/update-progress.json
```

至少记录 schema、profile、目标交易日、阶段、总数、完成数、待处理数、当前股票、最近成功时间和每只失败股票的受控失败原因。进度文件使用原子写入，只保留本机，不提交 Git。旧 `.checkpoint.json` 只允许一次性迁移读取，迁移后删除兼容分支。

## 8. SQLite 到 JSONL 迁移

迁移必须持维护锁、可重入并支持 dry-run：

1. 扫描旧 V2/V3 SQLite 和现有索引；
2. 校验 identity、raw/qfq 配对、窗口长度和文件上限；
3. 在仓库外临时目录生成月份代码范围 JSONL；
4. 生成 catalog 并校验行数、摘要、日期范围和文件大小；
5. 用新读取器逐股票读取，并与旧 SQLite 结果比较；
6. 通过后原子切换 V2/V3 目录；
7. 确认新路径稳定后再清理旧 SQLite；
8. 迁移失败保留旧数据，不进行半完成切换。

迁移期间禁止训练、推荐后台和 qfq 更新同时写入活动目录。

## 9. Git 与跨 PC 共享

提交：

```text
data/qfq/v2/*.jsonl
data/qfq/v3/*.jsonl
data/qfq/v2/catalog.json
data/qfq/v3/catalog.json
```

忽略：

```text
data/qfq/**/update-progress.json
data/qfq/**/.offsets.json
data/qfq/**/*.tmp
data/qfq/**/*-journal
data/qfq/**/*-wal
data/qfq/**/*-shm
```

更新 PC 执行 `qfq_download` 后，只提交实际变化的当前月份文件和 catalog。其他 PC 执行 `git pull` 后按 catalog 校验文件，并自动重建失效的本地偏移索引。不得让多台 PC 同时修改并提交同一月份文件。

JSONL 不压缩，以便 Git 进行文本差异和增量压缩；catalog 的 SHA-256 用于识别不完整拉取或文件损坏。

## 10. 测试与验收

必须覆盖：

- JSONL 编码/解码 round-trip；
- raw/qfq 同日配对和重复日期拒绝；
- 月份、代码范围和 `p01/p02` 路由；
- 跨月份读取 V2 251 日和 V3 61 日窗口；
- 当前月份更新只修改必要文件；
- 无变化重跑保持 JSONL、catalog 和索引字节不变；
- 6 MB 预拆分、8 MB 硬上限；
- catalog 摘要不匹配 fail-closed；
- 本地偏移索引损坏后自动重建；
- 中断重启不重复已完成股票；
- SQLite 迁移前后逐股票结果一致；
- 缺失窗口保持 `pending`，不得伪造评分输入。

定向门禁：

```bash
.venv/bin/python3 -m pytest -q tests/component/test_qfq_windows.py
.venv/bin/python3 -m ruff check <受影响 Python 文件>
.venv/bin/python3 -m mypy <受影响模块>
git diff --check
```

完整 history 实测还需记录总文件数、最大文件大小、总磁盘大小、初始化耗时、连续两次无变化更新结果、断点恢复结果和实时荐股读取覆盖率。

## 11. 完成标准

- V2/V3 所有读取路径切换到 JSONL，不保留双读或兼容第二 owner；
- 新旧实现逐股票窗口结果一致；
- 所有文件小于 8 MB；
- catalog 能独立说明月份、代码范围、大小、行数和摘要；
- 日更只修改当前月份必要文件；
- 续传记录可检查、可恢复且不进入 Git；
- `run.sh qfq_download` 可从已发布 history 初始化并由 Tencent 补齐；
- 实时荐股继续执行 V2/V3 完整窗口资格校验；
- 删除 SQLite 后无残留消费者、旧索引或双写路径。
