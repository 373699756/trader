# A 股荐股与策略验证看板

本项目是一个本地 Flask 看板，用公开行情数据生成 A 股推荐候选，并把推荐结果保存到验证库里做持续复盘。

结果只用于研究，不构成投资建议，也不保证盈利。

## 当前功能

- 三类结果：盘中强势观察（零仓位）、明日优先、2-5 日持有
- 策略验证：历史批次、样本表现、股票明细、DeepSeek 复盘
- 个股预测：输入股票代码，返回本地预测和 DeepSeek 优化建议
- 自动保存与回填：交易日 14:30 后补齐已成熟收益，15:00 后保存一次收盘候选快照；可执行策略统一按次日开盘成交
- DeepSeek：候选复核、风险降权、验证复盘、影子调参建议
- 执行门控：至少 20 个真实交易日，平均净收益为正、净胜率不低于 50%，且主周期平均回撤优于硬限制；未通过时只展示零仓位备选
- 历史因子默认启用；缓存缺失时后台分批预热，不阻塞推荐接口。盘后使用 `./run.sh after-close` 或 `.\run.ps1 after-close` 更新完整日线、快照和 IC

## 运行环境与安装

启动脚本会自动创建 `.venv`，并从 `requirements.txt` 安装 Flask、akshare、pandas、numpy、requests、tushare、pytest 等 Python 依赖。机器上需要先准备的是 Python 和基础命令环境；不需要安装 Node、Redis、MySQL、PostgreSQL 或额外数据库服务，运行数据会写入本地 `.runtime/` 下的 SQLite/JSON 文件。

通用要求：

- Python 3.9-3.14，建议安装 64 位 CPython，并确保 `python`/`python3` 或 Windows 的 `py` 启动器可用。
- `pip` 和 `venv`。Windows 的 python.org 安装包通常自带；Ubuntu/Debian 需要安装 `python3-venv`。
- 能访问 PyPI 安装依赖，并能访问公开行情数据源。网络需要代理时，用 `PROXY_MODE`、`PROXY_HOST`、`PROXY_PORT` 配置。
- Git 只在需要 `git clone` 获取项目时必需；拿到源码目录后运行本项目不依赖 Git。

Windows 需要：

- Windows 10/11 或同等 Windows Server 环境。
- PowerShell 5.1+，系统自带即可；也可以从 CMD 运行 `run.bat`。
- Python 3.9-3.14。安装时建议勾选 `Add python.exe to PATH`，或者保留 Python Launcher `py.exe`。

Linux/macOS/WSL 需要：

- Bash。
- Python 3.9-3.14、`pip`、`venv`。
- Ubuntu/Debian 示例：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ca-certificates
```

- macOS 可使用系统 Python、python.org 安装包或 Homebrew Python，只要版本满足 3.9-3.14 即可。

可选配置：

- `DEEPSEEK_API_KEY`：启用 DeepSeek 复核和复盘；也可以写入项目根目录 `.env`。
- `TUSHARE_TOKEN`：启用 Tushare 作为备用行情/历史数据源；没有也可以运行。
- 如只想本地启动且跳过启动前外网检查，可显式设置 `SKIP_PROXY_CHECK=1`，但首次安装依赖仍需要能访问 PyPI。

## 启动

首次启动会自动创建 `.venv` 并安装依赖。

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
ENABLE_HISTORY_FACTORS=1 ./run.sh
VALIDATION_AUTO_SNAPSHOT_TIME=15:00 ./run.sh
VALIDATION_AUTO_UPDATE_START_TIME=14:30 ./run.sh
VALIDATION_AUTO_UPDATE_INTERVAL_SECONDS=600 ./run.sh
ENABLE_DEEPSEEK_RUNTIME=1 ./run.sh
PROXY_MODE=on PROXY_PORT=7890 ./run.sh
SKIP_PROXY_CHECK=1 ./run.sh
```

Windows PowerShell 常用环境变量：

```powershell
$env:PORT="5050"; .\run.ps1
$env:ENABLE_HISTORY_FACTORS="1"; .\run.ps1
$env:VALIDATION_AUTO_SNAPSHOT_TIME="15:00"; .\run.ps1
$env:VALIDATION_AUTO_UPDATE_START_TIME="14:30"; .\run.ps1
$env:VALIDATION_AUTO_UPDATE_INTERVAL_SECONDS="600"; .\run.ps1
$env:ENABLE_DEEPSEEK_RUNTIME="1"; .\run.ps1
$env:PROXY_MODE="on"; $env:PROXY_PORT="7890"; .\run.ps1
$env:SKIP_PROXY_CHECK="1"; .\run.ps1
```

盘后任务：

```powershell
.\run.ps1 after-close
.\run.ps1 after-close --strategy all
.\run.ps1 after-close --market-data-limit 500
```

## 常用接口（A股口径）

- `GET /api/recommendations?top_n=18&market=all`
- `GET /api/tomorrow-picks?top_n=18&market=all`
- `GET /api/swing-picks?top_n=18&market=all`
- `GET /api/stock-prediction/<code>`
  - 其中 `market=all` 表示 A 股主板+创业板+科创板（对应沪深/创业/科创）。
- `GET /api/strategy-validation?strategy=tomorrow_picks`
- `GET /api/strategy-validation/tuning?strategy=tomorrow_picks`
- `POST /api/strategy-validation/tuning?strategy=tomorrow_picks`

## 数据与备份

- 验证数据库：`.runtime/strategy_validation.sqlite3`
- 自动备份文件：`.runtime/strategy_validation.backup.sqlite3`
- 备份列表：
  - Linux/macOS/WSL：`.venv/bin/python -m stock_analyzer.daily_job --list-validation-backups`
  - Windows：`.venv\Scripts\python.exe -m stock_analyzer.daily_job --list-validation-backups`
- 还原备份：
  - Linux/macOS/WSL：`.venv/bin/python -m stock_analyzer.daily_job --restore-validation <backup-file>`
  - Windows：`.venv\Scripts\python.exe -m stock_analyzer.daily_job --restore-validation <backup-file>`

## 文档

- [`docs/strategy_and_prediction.md`](docs/strategy_and_prediction.md)：三类荐股策略、DeepSeek 结合方式、个股预测与优化建议。
- [`docs/software_design.md`](docs/software_design.md)：软件整体结构、页面设计、接口、异步刷新、验证保存和运行方式。
