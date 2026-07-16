# A股策略看板

本项目是在个人 PC 上运行的 A 股推荐研究工具。它从公开行情构建候选，执行确定性本地评分，可选使用 DeepSeek 五维复核，并通过只读 Web 看板展示 today、tomorrow、d25 和 long 四类结果。

结果只用于研究，不构成投资建议，不提供真实下单能力，也不保证收益。

## 运行范围

- Python 3.10-3.14。
- 当前稳定版 Chrome、Edge 或 Firefox 桌面浏览器。
- 仅支持个人 PC；手机和平板浏览器不属于产品范围，也不纳入发布验收。
- 默认仅监听 `127.0.0.1`，不提供远程身份认证。
- SQLite 和 JSON 运行数据写入 `.runtime/v2`，不需要 Redis、Celery、Node 或外部数据库。

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

常用配置：

```bash
TRADER_PORT=5050 ./run.sh
DEEPSEEK_API_KEY=your-key ./run.sh
TRADER_CONFIG=/absolute/path/runtime.json ./run.sh
./run.sh validate-config
```

兼容旧用法的 `HOST` 和 `PORT` 会在启动脚本边界映射为 `TRADER_HOST` 和 `TRADER_PORT`。应用内部只读取 v2 环境变量。

## 手动安装

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/trader-cli --config "$PWD/config/v2/runtime.json" validate-config
.venv/bin/trader-server --config "$PWD/config/v2/runtime.json"
```

配置路径必须为绝对路径。`TRADER_CONFIG` 可代替 `--config`。DeepSeek 密钥只从 `DEEPSEEK_API_KEY` 读取，不写入配置、快照或日志。

## Web API

- `GET /api/status`
- `GET /api/recommendations/<today|tomorrow|d25|long>?date=YYYY-MM-DD&top_n=10`
- `GET /api/recommendation-dates?strategy=<today|tomorrow|d25>`
- `GET /api/events?cursor=0&limit=100`
- `GET /api/events/stream`

当前快照支持 ETag。SSE 使用单调事件 ID 和 `Last-Event-ID` 恢复；游标过旧时返回 `resync_required`。Web 请求只读取已发布快照，不抓行情、不评分、不调用 DeepSeek。

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
- DeepSeek 每日物理请求全局硬上限为 188。

完整业务契约见 [docs/need.md](docs/need.md)，运行维护见 [docs/operations/runbook.md](docs/operations/runbook.md)，协作与强制 review 流程见 [AGENTS.md](AGENTS.md)。

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
config/v2/          运行与策略配置
docs/               需求、架构、运维和 v1 归档
scripts/            工程辅助脚本
src/trader/         唯一活动产品包
tests/              单元、组件、契约和集成测试
.runtime/v2/        本地运行数据，不进入 Git
```
