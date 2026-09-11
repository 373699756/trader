# A股策略看板

本项目是在个人 PC 上运行的 A 股推荐研究工具。它从公开行情构建候选，执行确定性本地评分，可选使用 DeepSeek 五维复核，并通过只读 Web 看板展示 today、tomorrow、d25 和 long 四类结果。

结果只用于研究，不构成投资建议，不提供真实下单能力，也不保证收益。

## 运行范围

- Python 3.10-3.14。
- 当前稳定版 Chrome、Edge 或 Firefox 桌面浏览器。
- 仅支持个人 PC；手机和平板浏览器不属于产品范围，也不纳入发布验收。
- 默认仅监听 `127.0.0.1`，不提供远程身份认证。
- SQLite 和 JSON 运行数据写入 `.runtime/trader`，不需要 Redis、Celery、Node 或外部数据库。

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
./run.sh check
./run.sh --profile v2
./run.sh download_history
./run.sh train-tomorrow
./run.sh install-history-automation
./run.sh uninstall-history-automation
./run.sh help
```

日常启动不需要参数，默认使用 Tomorrow V1；追加 `--profile v2` 才使用 V2，该覆盖不会写回配置。
`check` 依次执行配置校验、只读研究状态、持久化训练 due/提醒状态和所选档位的离线性能门禁；`download_history` 是唯一零参数历史维护
入口，`train-tomorrow` 负责统一的 Tomorrow 离线训练链。旧 H0 历史归档、回测和筛选入口已退役，
不再通过启动流程执行。离线研究不会随服务启动自动执行。底层
`trader-cli performance-check` 仍可用 `--output` 保存报告或用 `--baseline` 执行 5% 相对回归门禁；它
禁止外网并直接测量活动生产标准化、合并、三策略评分、overlay CAS、API/ETag/status、SSE 和 100 tick RSS。
BaoStock 下载是独立研究命令，必须先安装 `trader-research-dashboard[research]`。路径、滚动 2000 日和资源
参数不对用户开放；`--runtime-dir`、`--sessions`、`--mode`、`--profile` 等参数都会在环境创建前拒绝。零参数
重构控制库、稳定年月月分片读取面和零参数同步已经交付：命令自动执行初次最近 2000 日、日更缺口、最近 5 日
回读或返回 `already_current`，失败与取消不改变上一 active snapshot；它不会被启动、`check`、Web 或
`train-tomorrow` 隐式调用。
交互式下载进度固定在 stderr 显示紧凑单行：累计和调用耗时使用 `HH:MM:SS`，股票和月分片使用
`n/m (百分比)`，供应商重试使用“尝试 n/m”；日历、行业等子阶段只显示真实当前项，不再把内部 `0/1`
占位冒充整体进度。失败摘要保留最后一个具体供应商阶段、当前日期或股票和稳定错误码；最终 stdout JSON 保持不变，
供脚本消费。

历史自动化固定为“每日自动同步、到期只提醒”。`install-history-automation` 会先显示待写入的当前用户任务并要求
确认：Linux 安装带 `Persistent=true` 的 systemd user timer，Windows 安装带 `StartWhenAvailable` 的任务，
macOS 安装带日历触发与登录补跑的 LaunchAgent；每天按上海时间 15:10 和 20:30 调用同一零参数维护命令。
安装会验证既有虚拟环境和 BaoStock 依赖，计划任务自身不会安装或升级包；卸载只移除内容未被用户改写的托管文件。
任务日志写入 `.runtime/trader/logs/history-automation.log` 并按大小轮转。同一训练 due 身份在每个上海日期最多通知
一次；桌面通知不可用只记为 `notification_degraded`，不会把成功下载改成失败，也不会自动训练、切换档位或重启服务。

历史归档只接受 `data/history/baostock/control.sqlite3` 与 `partitions/YYYY/MM.sqlite3` 月分片；文件 hash 保存在
控制状态中用于校验，旧目录和兼容读取均不进入活动产品。
若仍有唯一旧归档，可显式运行 `.venv/bin/python scripts/convert_baostock_history.py` 一次性转换；该脚本不被启动、
Web、`check`、训练或零参数同步隐式调用。`research-status` 和统一诊断的 `research` profile 只读投影同一个
active snapshot 身份，不会触发下载、训练或文件写入。

启动脚本只读取 `TRADER_HOST` 和 `TRADER_PORT`；旧 `HOST`/`PORT` 不再映射到 当前进程。

## 荐股漏斗诊断

优先使用统一入口一次执行 Web 漏斗、沪深交易所基础资料、历史源、腾讯实时报价和 Tushare 能力检查；各专项能力由
`scripts/runtime_diagnostics/` 内部职责模块维护，不再保留多个顶层包装脚本：

```bash
.venv/bin/python scripts/diagnose_runtime.py \
  --profile live \
  --base-url http://127.0.0.1:5000 \
  --output -
```

`runtime` 只检查运行中的 Web，`sources` 只实测数据源，默认 `live` 合并两者；`full` 额外执行 Firefox
刷新链与离线生产性能门禁。命令会在单项失败后继续扫描，最终报告使用
`trader-runtime-diagnostics`，只保留聚合计数、延迟、状态和定位结论，不转发股票代码、价格、Token、
供应商原始载荷或子进程 stderr。需要留档时，`--output` 和 `--persistence-runtime-dir` 只能指向仓库外
绝对路径。
也可执行 `make diagnose-live`；耗时更长的浏览器与性能组合必须显式执行 `make diagnose-full`。

历史为空时可继续通过统一入口拆分生产组合路由和单一供应商；默认值为 `composite`，该参数只影响
只读诊断，不改变运行服务的来源优先级：

```bash
.venv/bin/python scripts/diagnose_runtime.py \
  --profile history \
  --history-source tencent \
  --codes 688981 \
  --output -
```

只复测单一边界时仍使用同一个入口，profile 可选 `web`、`security-master`、`history`、`tencent`、`tushare`、`browser`
和 `performance`。例如只检查荐股漏斗：

```bash
.venv/bin/python scripts/diagnose_runtime.py \
  --profile web \
  --base-url http://127.0.0.1:5000 \
  --web-samples 6 \
  --web-interval-seconds 5 \
  --output -
```

脚本同时读取 status 与 today/tomorrow/d25 current，检测候选、特征、证券身份、历史和完整评分
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
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/trader-cli --config "$PWD/config/runtime.json" validate-config
.venv/bin/trader-server --config "$PWD/config/runtime.json"
```

配置路径必须为绝对路径。`TRADER_CONFIG` 可代替 `--config`。DeepSeek 密钥优先从
`DEEPSEEK_API_KEY` 读取，也可使用 `DEEPSEEK_API_KEY_FILE` 或项目根目录
`.token_key` 的 `DEEPSEEK_API_KEY` 字段；密钥不写入配置、快照或日志。

## Tushare 慢数据

当前 120 积分档按官方 50 次/分钟、8000 次/日权限，只用 Tushare `daily` 单证券未复权日线做
低频能力审计和来源健康观测。生产参考 lane 以固定审计代码复用 6 小时缓存，评分历史仍直接使用
腾讯/东方财富前复权日线；证券主数据、交易日历、复权因子、日度估值和财务指标等 2000 积分能力
不调用。SDK 已由 `pyproject.toml` 作为默认运行依赖安装；Token 缺失时显式降级。
`scripts/diagnose_runtime.py --profile tushare --output -` 可只读实测当前 Token，统一报告输出延迟、
能力和进程内配额计数，不会输出 Token、价格、逐股载荷或完整供应商响应。

已有完整日线归档时，可用下列工程脚本只读审计历史行业事实；它不请求供应商、不修改归档，数据不足时以
退出码 1 和 `historical_data_insufficient` 失败关闭。默认输出只有聚合结果；逐股票与逐事实 hash 必须配合
`--include-details` 写到仓库外绝对路径。

```bash
.venv/bin/python scripts/audit_historical_industry_facts.py \
  --history-root "$PWD/data/history" \
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
`config/runtime.json` 中 `market_data.tushare.token_file` 指向的赋值文件，默认即
`.token_key`。POSIX 系统必须限制该文件仅属主可读写，例如：

```bash
chmod 600 .token_key
./run.sh
```

Token、SDK、额度或网络不可用时，Tushare lane 会显式降级。历史特征由腾讯前复权日线
主源和东方财富回退源重新预热，不读取或写入旧历史 SQLite；每只临时计算最多 61 根，
进程内只保留最近 20 根原始日线及紧凑长周期摘要。东方财富/新浪全市场实时行情、腾讯
候选定向报价、AKShare 研究数据、本地推荐和只读 Web 继续运行。Token 不会写入配置、
日志、SQLite、快照或 API。

## Web API

- `GET /api/decisions/<today|tomorrow|d25|long>/current`
- `GET /api/decisions/<today|tomorrow|d25>/history?date=YYYY-MM-DD`
- `GET /api/decisions/<today|tomorrow|d25>/dates`
- `GET /api/status`
- `GET /api/events`

当前快照支持 ETag。SSE 使用单调事件 ID 和 `Last-Event-ID` 恢复；游标过旧时返回 `resync_required`。Web 请求只读取已发布快照，不抓行情、不评分、不调用 DeepSeek。
Long 的三个固定分类和股票身份由打包资源立即显示；当前决策 只增强实时行情，服务或行情暂时
不可用时不会把固定名单隐藏。

## 关键契约

融合公式固定为：

```text
final_score = clamp(local_score * 0.68
                    + deepseek_score * 0.32
                    - deepseek_risk_penalty, 0, 100)
```

`local_score` 已扣本地风险。DeepSeek 风险扣分由本地规则根据已验证风险事实映射，不能采用模型自由生成的数值。

- today 于 11:20 冻结。
- tomorrow 和 d25 于 14:50 冻结。
- long 不冻结、不进入历史推荐。
- DeepSeek 每日物理请求全局硬上限为 168。

产品、架构、运行、API 与运维契约见
[软件业务设计文档](docs/02_工程设计.md)，候选、过滤、评分、DeepSeek、融合与
TopK 契约见[荐股策略文档](docs/01_评分逻辑.md)，协作与强制 review 流程见
[AGENTS.md](AGENTS.md)。历史数据下载、Tomorrow 训练和训练工件参与实时荐股的完整说明见
[策略回溯文档](docs/04_策略回溯.md)。

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
docs/               工程设计、评分逻辑、工程实施与策略回溯文档
scripts/            工程辅助脚本
src/trader/         唯一活动产品包
tests/              单元、组件、契约和集成测试
.runtime/trader/        本地运行数据，不进入 Git
```
