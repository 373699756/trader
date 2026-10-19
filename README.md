# A股策略看板

本项目是在个人 PC 上运行的 A 股推荐研究工具。它从公开行情构建候选，执行确定性本地评分，可选使用 DeepSeek 结构化风险复核，并通过只读 Web 看板展示 Tomorrow、D25 和 Long 三类结果。

结果只用于研究，不构成投资建议，不提供真实下单能力，也不保证收益。

## 运行范围

- Python 3.10-3.14。
- 当前稳定版 Chrome、Edge 或 Firefox 桌面浏览器。
- 仅支持个人 PC；手机和平板浏览器不属于产品范围，也不纳入发布验收。
- 默认仅监听 `127.0.0.1`，不提供远程身份认证。
- 持久数据写入配置中的 `data_dir`（默认 `data/`）；`.runtime/trader` 保存进程锁、日志和临时产物。不需要 Redis、Celery、Node 或外部数据库。

## 一键启动

Linux、macOS 或 WSL：

```bash
chmod +x run.sh
./run.sh
```

Windows PowerShell：

```powershell
.\run.ps1
```

Windows CMD：

```bat
run.bat
```

脚本在需要时创建 `.venv`，从 `pyproject.toml` 安装项目，然后使用绝对配置路径启动 `trader-server`。默认地址为 <http://127.0.0.1:5000>。

常用命令与配置：

```bash
TRADER_PORT=5050 ./run.sh
DEEPSEEK_API_KEY=your-key ./run.sh
TRADER_CONFIG=/absolute/path/runtime.json ./run.sh
.venv/bin/python3 scripts/diagnose_runtime.py --profile local --output -
./run.sh --profile v2
./run.sh --profile v3
./run.sh --profile both
./run.sh train
./run.sh sync
./run.sh sync market
./run.sh sync company
./run.sh help
```

日常启动不需要参数，默认使用 V2 并加载 `data/training/models/v2/{tomorrow,d25}` 双头工件；
追加 `--profile v2|v3` 可单独启动一档，`--profile both` 在同一进程启动两档；这些选择不会写回配置。双档模式的 Tomorrow/D25 结果、冻结和历史按档位隔离，Long 固定由 V2 单一采集并供两档页面只读。启动不执行训练，所选档位任一bundle损坏时失败关闭。
`local`诊断检查配置、只读历史维护状态、研究状态和离线性能，支持`--scoring-profile v2|v3|both`。`train`先维护完整历史，验收后在同一进程联合训练V2/V3；双档工件与当前数据、配置、实现身份完整匹配时复用。`sync market`独立更新在线短窗口；`sync company`增量更新公司资料单库。`sync`共用一次官方总体与ST准备，顺序执行market和company；单项失败继续，取消、配置或公共准备失败停止。stdout仅最终JSON，进度走stderr，退出码0/1/2/130分别表示完成、失败或待补、参数/配置错误、取消。

`./run.sh train`使用Tencent取得完整历史的raw/qfq价格对，BaoStock只提供历史行业证据。已发布快照中的价格缺口会继续报告和补取；训练只使用有效样本，并由各档样本覆盖门禁判断能否发布。历史维护失败或参考数据不完整仍阻断训练；不跨来源拼价格。

`./run.sh sync market`：独立取得官方股票名单与 Tencent 日历，默认8个worker下载短窗口，不调用 BaoStock 网络。
在线 V2/V3 共读 `data/market/daily_window.sqlite3`，按最近251/61个市场交易日范围读取真实raw/qfq价格对；价格、两档身份与完成断点在同一个WAL/FULL事务提交。下载与单写入者有界并行，失败保留有效窗口。后台仍仅在进程运行到15:10时尝试一次，晚启动或失败后由`sync market`续传。
完整history继续用于训练、研究和结果结算；在线请求只读已有投影，不等待下载。

历史自动化固定为“每日检查、归档周更、训练到期只提醒”。当前版本不再提供公开的安装/卸载入口；已有系统任务仍可调用内部
`trader.entrypoints.cli sync history` 零参数命令；旧任务须更新命令配置，模板已同步。每天按上海时间 15:10 和
20:30唤醒同一维护命令，只有归档截止日落后至少7个自然日且上海时间不早于15:10才执行下载；未到期返回`not_due`，
无归档返回`blocked`并等待手工`./run.sh train`初始化。手工下载不受周更门槛限制，失败后下次到期唤醒可续传。
任务日志写入`.runtime/trader/logs/history-automation.log`并按大小轮转。同一训练due
身份在每个上海日期最多通知一次；桌面通知不可用只记为 `notification_degraded`，不会把成功下载改成失败，也不会
自动训练、切换档位或重启服务。只读 `history-automation-status` 仍可查看持久化 due、提醒和活动快照状态。

历史使用 `data/market/history/catalog.sqlite3` 与 `prices_YYYY.sqlite3` 年库；逐月逻辑摘要和观察序号保留旧快照隔离，绑定参考与发布指针同库提交。旧格式不参与活动读取，重建或真实切换另行授权。

冻结、运行快照、共享预算、资格与研究持久证据统一归入`data/`；`.runtime`只保留进程锁、日志和临时产物。隔离迁移使用`scripts/migrate_runtime_data.py build --source <仓库外旧工作区副本> --target <仓库外新data目录> --default-profile v2`，不替换活动数据。`research-status`和研究诊断只读同一业务状态。

启动脚本只读取 `TRADER_HOST` 和 `TRADER_PORT`；旧 `HOST`/`PORT` 不再映射到 当前进程。

## 荐股漏斗诊断

优先使用统一入口一次执行 Web 漏斗、沪深交易所基础资料、历史源、腾讯实时报价和 Tushare 能力检查；各专项能力由
`scripts/runtime_diagnostics/` 内部职责模块维护，不再保留多个顶层包装脚本：

```bash
.venv/bin/python3 scripts/diagnose_runtime.py \
  --profile live \
  --base-url http://127.0.0.1:5000 \
  --output -
```

`runtime` 只检查运行中的 Web，`sources` 只实测数据源，默认 `live` 合并两者；`full` 额外执行 Firefox
刷新链与离线生产性能门禁。命令会在单项失败后继续扫描，最终报告使用
`trader-runtime-diagnostics`，只保留聚合计数、延迟、状态和定位结论，不转发股票代码、价格、Token、
供应商原始载荷或子进程 stderr。需要留档时，`--output` 只能指向仓库外绝对路径。
也可执行 `make diagnose-live`；耗时更长的浏览器与性能组合必须显式执行 `make diagnose-full`。

历史为空时可继续通过统一入口拆分生产组合路由和单一供应商；默认值为 `composite`，该参数只影响
只读诊断，不改变运行服务的来源优先级：

```bash
.venv/bin/python3 scripts/diagnose_runtime.py \
  --profile history \
  --history-source tencent \
  --codes 688981 \
  --output -
```

只复测单一边界时仍使用同一个入口，profile 可选 `web`、`security-master`、`history`、`tencent`、`tushare`、`browser`
和 `performance`。例如只检查荐股漏斗：

```bash
.venv/bin/python3 scripts/diagnose_runtime.py \
  --profile web \
  --base-url http://127.0.0.1:5000 \
  --web-samples 6 \
  --web-interval-seconds 5 \
  --output -
```

脚本同时读取 status 与 tomorrow/d25 current，检测候选、特征、证券身份、历史和完整评分
持续为 0、已形成阶段回退为 0、input quality 消失、release/schema 及 status/current 身份不一致。
正式推荐或观察数量单独为 0 可以是合法业务空集，不会据此报警；合法空 current 缺少
`selection_diagnostics.empty_reason` 才会报错。报告只包含聚合计数和版本身份，不输出股票代码。
存在错误时进程退出码为 1；连接失败同样生成结构化 JSON，便于定时任务留证。

## 关闭与重启

在运行服务的终端按一次 Ctrl+C 会开始安全关闭，Web、冻结任务和后台资源共享一个最长
30 秒的总期限；不是每个组件分别等待 30 秒。关闭期间再次按 Ctrl+C 会立即强制退出。
Linux/macOS 的正常 `SIGTERM` 和 Windows `SIGBREAK` 使用相同规则。关闭浏览器不会停止
服务。

如果再次执行 `./run.sh` 时已有服务持有同一运行目录，脚本会返回非零并显示现有浏览器地址。
这表示旧服务仍在运行，并不表示锁文件损坏或新代码已经加载；请在原启动终端按一次 Ctrl+C，
等待正常退出后再启动，不要删除 `.runtime/trader/server.lock`。

正常重启会重新预热行情、候选、观察池和研究/review 等纯内存状态；正式推荐、合法检查点、
预算、统一数据平面和收盘 overlay 按各自持久化契约恢复。强制结束进程或断电属于异常终止，
正式冻结会在下次启动时按恢复载荷和 SHA-256 校验恢复或 fail closed。

## 手动安装

```bash
python3 -m venv .venv
.venv/bin/python3 -m pip install -e ".[dev]"
.venv/bin/trader-cli --config "$PWD/config/runtime.json" validate-config
.venv/bin/trader-server --config "$PWD/config/runtime.json"
```

配置路径必须为绝对路径。`TRADER_CONFIG` 可代替 `--config`。DeepSeek 密钥优先从
`DEEPSEEK_API_KEY` 读取，也可使用 `DEEPSEEK_API_KEY_FILE` 或项目根目录
`.token_key` 的 `DEEPSEEK_API_KEY` 字段；密钥不写入配置、快照或日志。

## Tushare 慢数据

当前120积分档的Tushare只保留`daily`未复权日线的独立研究审计。生产股票身份使用交易所官方列表，qfq使用Tencent，公司财务和风险使用已发布公司资料；无消费者的Tushare估值、财务增强和前复权接口已删除。权限不足的参考能力不提交空任务，Token缺失仍明确报告。
`scripts/diagnose_runtime.py --profile tushare --output -` 可只读实测当前 Token，统一报告输出延迟、
能力和进程内配额计数，不会输出 Token、价格、逐股载荷或完整供应商响应。

已有完整日线归档时，可用下列研究命令只读审计历史行业事实；它不请求供应商、不修改归档，数据不足时以
退出码 1 和 `historical_data_insufficient` 失败关闭。默认输出只有聚合结果；逐股票与逐事实 hash 必须配合
`--include-details` 写到仓库外绝对路径。

```bash
.venv/bin/trader-cli --config "$PWD/config/runtime.json" research-industry-audit \
  --history-root "$PWD/data/market/history" \
  --required-sample-codes 300 \
  --tushare-access-points 120 \
  --output -
```

项目根目录 `.token_key` 同时保存两个独立字段：

```bash
DEEPSEEK_API_KEY=your-deepseek-key
TUSHARE_TOKEN=your-tushare-token
```

Token 优先从 `TUSHARE_TOKEN` 读取，其次读取 `TUSHARE_TOKEN_FILE`，最后读取
`config/market_data.json` 中 `market_data.tushare.token_file` 指向的赋值文件，默认即
`.token_key`。POSIX 系统必须限制该文件仅属主可读写，例如：

```bash
chmod 600 .token_key
./run.sh
```

Token、SDK、额度或网络不可用时，Tushare lane 会显式降级。服务从 `data/market/daily_window.sqlite3` 短窗口构造历史特征，
后台短窗口维护与 `./run.sh sync market` 共用 Tencent 有界并发用例；不依赖 Tushare 或 BaoStock 网络。
短窗口暂不可用时页面明确显示待下载/待更新，
已有最近有效视图则继续降级使用。东方财富/新浪全市场实时行情、腾讯
候选定向报价、AKShare 研究数据、本地推荐和只读 Web 继续运行。Token 不会写入配置、
日志、SQLite、快照或 API。

## Web API

- `GET /api/decisions/<tomorrow|d25|long>/current`
- `GET /api/decisions/<tomorrow|d25>/history?date=YYYY-MM-DD`
- `GET /api/decisions/<tomorrow|d25>/dates`
- `GET /api/status`
- `GET /api/events`

当前快照支持 ETag。SSE 使用单调事件 ID 和 `Last-Event-ID` 恢复；游标过旧时返回 `resync_required`。Web 请求只读取已发布快照，不抓行情、不评分、不调用 DeepSeek。
Long 的三个固定分类和股票身份由打包资源立即显示；当前决策 只增强实时行情，服务或行情暂时
不可用时不会把固定名单隐藏。

## 关键契约

Tomorrow/D25 的最终分使用已扣本地风险的 `local_score`，按 `ROUND_HALF_UP` 保留两位小数。模型只提供预测诊断和成本净效用门；DeepSeek 只提供经证据核验的结构化风险 veto，不参与数值评分。

- tomorrow 和 d25 于 15:00 first-wins 冻结，运行与新研究评价锚点同为 15:00。
- long 不冻结、不进入历史推荐。
- DeepSeek 每日物理请求全局硬上限为 168。

产品、架构、运行、API 与运维契约见
[工程设计](docs/2_工程设计.md)，候选、过滤、评分、DeepSeek、融合与
TopK 契约见[荐股策略文档](docs/1_评分逻辑.md)，协作与强制 review 流程见
[AGENTS.md](AGENTS.md)。历史数据下载、Tomorrow 训练和训练工件参与实时荐股的完整说明见
[下载训练文档](docs/4_下载训练.md)。

## 质量检查

```bash
make format-check
make lint
make type-check
make test
make package
```

`make package` 构建 sdist 和 wheel。发布前还必须在仓库外安装 wheel，验证 console scripts、模板、CSS、JavaScript 和图标资源。

## 目录

```text
config/          运行与策略配置
docs/               工程设计、评分逻辑、工程实施与下载训练文档
scripts/            工程辅助脚本
src/trader/         唯一活动产品包
tests/              单元、组件、契约和集成测试
data/                  业务持久数据，不进入 Git
.runtime/trader/        进程锁、日志和临时产物，不进入 Git
```
