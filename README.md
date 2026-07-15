# A 股荐股与策略验证看板

本项目是一个本地 Flask 看板，用公开行情数据生成 A 股推荐候选，并把推荐结果保存到验证库里做持续复盘。

结果只用于研究，不构成投资建议，也不保证盈利。

## 当前功能

- 三类结果：今日延续推荐（信号至收盘观察）、明日优先、2-5 日持有；另有“长期”展示标签（独立观察池，偏长期持有观察，不参与执行策略收益口径）
- 策略验证：历史批次、样本表现、股票明细、DeepSeek 证据与 Meta 归因
- 个股预测：输入股票代码，返回本地量化诊断和三策略命中状态
- 自动保存与回填：交易日 14:30 生成三个策略快照，最迟 14:35 冻结；具体业务口径统一见 `docs/strategy_and_prediction.md`
- DeepSeek：盘中后台任务只预计算结构化证据特征，推荐和个股预测请求不等待外部 API
- 执行门控：真实 OOS、组合收益和置信区间未达门槛时允许输出空推荐或零仓位备选
- 历史因子默认启用；缓存缺失时后台分批预热，不阻塞推荐接口。盘后使用 `./run.sh after-close` 或 `.\run.ps1 after-close` 更新完整日线、回填 14:30 已冻结信号并刷新 IC

## 运行环境与安装

启动脚本会自动创建 `.venv`，并根据 `pyproject.toml` 和 `requirements/runtime.txt` 安装运行依赖。依赖声明、Python 小版本和平台没有变化时，后续启动会通过 `.runtime/runtime-dependencies.sha256` 校验后跳过联网安装；设置 `FORCE_INSTALL_DEPS=1` 可强制重装。开发工具通过根目录 `requirements.txt` 或 `pip install -e ".[dev]"` 单独安装。机器上不需要 Node、Redis、MySQL、PostgreSQL 或额外数据库服务，运行数据会写入本地 `.runtime/` 下的 SQLite/JSON 文件。

通用要求：

- Python 3.10-3.14，建议安装 64 位 CPython，并确保 `python`/`python3` 或 Windows 的 `py` 启动器可用。
- `pip` 和 `venv`。Windows 的 python.org 安装包通常自带；Ubuntu/Debian 需要安装 `python3-venv`。
- 能访问 PyPI 安装依赖，并能访问公开行情数据源。网络需要代理时，用 `PROXY_MODE`、`PROXY_HOST`、`PROXY_PORT` 配置。
- Git 只在需要 `git clone` 获取项目时必需；拿到源码目录后运行本项目不依赖 Git。

Windows 需要：

- Windows 10/11 或同等 Windows Server 环境。
- PowerShell 5.1+，系统自带即可；也可以从 CMD 运行 `run.bat`。
- Python 3.10-3.14。安装时建议勾选 `Add python.exe to PATH`，或者保留 Python Launcher `py.exe`。

Linux/macOS/WSL 需要：

- Bash。
- Python 3.10-3.14、`pip`、`venv`。
- Ubuntu/Debian 示例：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ca-certificates
```

- macOS 可使用系统 Python、python.org 安装包或 Homebrew Python，只要版本满足 3.10-3.14 即可。

可选配置：

- `DEEPSEEK_API_KEY`：启用 DeepSeek 后台证据预计算和盘后研究；也可以写入项目根目录 `.deepseek_key`。
- `TUSHARE_TOKEN`：启用 Tushare 作为备用行情/历史数据源；也可以写入同一个 `.deepseek_key` 文件，没有也可以运行。
- 如只想本地启动且跳过启动前外网检查，可显式设置 `SKIP_PROXY_CHECK=1`，但首次安装依赖仍需要能访问 PyPI。

## 启动

首次启动会自动创建 `.venv` 并安装依赖；依赖未变化的后续启动可以离线完成。

### Linux/macOS/WSL

```bash
chmod +x run.sh
./run.sh
```

### Windows

在项目目录打开 PowerShell：

```powershell
.\run.ps1
```

如果 PowerShell 执行策略拦截脚本，可用 CMD 运行：

```bat
run.bat
```

默认地址：

```text
http://127.0.0.1:5000
```

Linux/macOS/WSL 常用环境变量：

```bash
PORT=5050 ./run.sh
VALIDATION_AUTO_UPDATE_START_TIME=14:30 ./run.sh
VALIDATION_AUTO_UPDATE_INTERVAL_SECONDS=600 ./run.sh
DEEPSEEK_PRECOMPUTE_TIMES='["09:40","10:00"]' ./run.sh
PROXY_MODE=on PROXY_PORT=7890 ./run.sh
SKIP_PROXY_CHECK=1 ./run.sh
```

Windows PowerShell 常用环境变量：

```powershell
$env:PORT="5050"; .\run.ps1
$env:VALIDATION_AUTO_UPDATE_START_TIME="14:30"; .\run.ps1
$env:VALIDATION_AUTO_UPDATE_INTERVAL_SECONDS="600"; .\run.ps1
$env:DEEPSEEK_PRECOMPUTE_TIMES='["09:40","10:00"]'; .\run.ps1
$env:PROXY_MODE="on"; $env:PROXY_PORT="7890"; .\run.ps1
$env:SKIP_PROXY_CHECK="1"; .\run.ps1
```

可覆盖的运行参数由 `config/runtime.json` 的 `env_overrides` 白名单定义，并按原配置值类型解析。生产基线中的策略开关和锁定参数不可通过环境变量修改；若设置为不同值，程序会在启动时明确报错，避免产生未记录的策略漂移。

看板没有远程身份认证，默认只允许监听回环地址。若确实要绑定 `0.0.0.0`、局域网地址或主机名，必须显式设置 `SERVER_ALLOW_INSECURE_NON_LOOPBACK=1`；这只表示接受风险，不会自动增加认证，建议仍通过带访问控制的反向代理隔离。

盘后任务：

```powershell
.\run.ps1 after-close
.\run.ps1 after-close --strategy all
.\run.ps1 after-close --market-data-limit 500
```

离线任务统一改为：

```bash
.venv/bin/python -m stock_analyzer.jobs deepseek-precompute --strategy all --market all
.venv/bin/python -m stock_analyzer.jobs snapshot
.venv/bin/python -m stock_analyzer.jobs update-outcomes
.venv/bin/python -m stock_analyzer.jobs build-portfolios
.venv/bin/python -m stock_analyzer.jobs deepseek-meta-build --strategy all
.venv/bin/python -m stock_analyzer.jobs tune
.venv/bin/python -m stock_analyzer.jobs validate
.venv/bin/python -m stock_analyzer.jobs backup
.venv/bin/python -m stock_analyzer.jobs stats
```

直接运行 `app.py` 时，进程内 supervisor 会统一启动和停止实时行情、推荐池行情、DeepSeek、自动快照与结果回填线程；`create_app()` 本身不创建后台线程。使用其他 WSGI 入口时，需要显式调用 `create_app(start_runtime=True)`，或由外部任务平台执行上述命令，但不要同时启用两套调度。周期线程使用可中断的停止信号，一次性刷新线程由各自所有者拒绝新任务并在退出时等待回收。任务依赖 SQLite 租约防止相同槽位并发重入。`deepseek-precompute` 在交易日 09:40–14:20 约每 20 分钟做一次变化检查，午休不调用；14:30–14:48 仅按需调用，14:48 停止 API，14:50 冻结最终推荐。缓存命中和无证据弃权不计次，所有策略与模型合计最多 50 次/交易日。

## 开发质量检查

```bash
.venv/bin/python -m pip install -e ".[dev]"
make quality
make test-fast
make test-integration
make coverage-fast
make package
```

`make lint` 对全部产品代码执行正确性规则；完整 Ruff、格式和 mypy 门禁先覆盖新增或已迁移模块，再随模块重构逐步扩大。快速测试分支覆盖率当前以 50% 为最低基线，后续只能逐步提高。CI 在 Python 3.10–3.14 上执行快速测试，Python 3.11 单独执行覆盖率门禁和集成测试，慢速测试由 nightly 工作流执行。

日级组合基线随 `after-close` 自动刷新；也可单独重放：

```bash
.venv/bin/python -m stock_analyzer.daily_job --portfolio-baseline --strategy tomorrow_picks
.venv/bin/python -m stock_analyzer.daily_job --portfolio-baseline --strategy tomorrow_picks --portfolio-baseline-date 2026-07-10
.venv/bin/python -m stock_analyzer.daily_job --portfolio-baseline --strategy tomorrow_picks --portfolio-ranking-field rank_score --portfolio-model-id challenger_v1
```

冻结基线始终按 `score` 生成等权 Top-5；`--portfolio-ranking-field` 只生成同日期候选池上的挑战模型组。随机候选池基准使用固定种子且至少重复 1,000 次。

## 常用接口（A股口径）

- `GET /api/recommendations?top_n=18&market=all`
- `GET /api/tomorrow-picks?top_n=18&market=all`
- `GET /api/swing-picks?top_n=18&market=all`
- `GET /api/stock-prediction/<code>`
  - 其中 `market=all` 表示 A 股主板+创业板+科创板（对应沪深/创业/科创）。
- `GET /api/strategy-validation?strategy=tomorrow_picks`
- `GET /api/strategy-validation/portfolio-baseline?strategy=tomorrow_picks&days=120`
- `POST /api/strategy-validation/portfolio-baseline?strategy=tomorrow_picks&days=120`
- `GET /api/strategy-validation/tuning?strategy=tomorrow_picks`
- `POST /api/strategy-validation/tuning?strategy=tomorrow_picks`

## 数据与备份

- 验证数据库：`.runtime/strategy_validation.sqlite3`
- 日级组合审计表：同库的 `daily_portfolio_baselines`；保存完整候选哈希、排名、执行状态、随机路径和五类对照。
- P1 数据与标签验收：`.venv/bin/python -m stock_analyzer.validation_audit_cli --strategy tomorrow_picks --sample-size 30`
- 自动备份文件：`.runtime/strategy_validation.backup.sqlite3`
- 备份列表：
  - Linux/macOS/WSL：`.venv/bin/python -m stock_analyzer.daily_job --list-validation-backups`
  - Windows：`.venv\Scripts\python.exe -m stock_analyzer.daily_job --list-validation-backups`
- 还原备份：
  - Linux/macOS/WSL：`.venv/bin/python -m stock_analyzer.daily_job --restore-validation <backup-file>`
  - Windows：`.venv\Scripts\python.exe -m stock_analyzer.daily_job --restore-validation <backup-file>`

## 文档

- [`docs/strategy_and_prediction.md`](docs/strategy_and_prediction.md)：荐股策略、评分取舍、收益口径、DeepSeek 业务边界和 OOS 晋级标准。
- [`docs/software_design.md`](docs/software_design.md)：软件结构、接口、数据库、生产冻结、试验登记、readiness 审计、任务门控和运行方式。
