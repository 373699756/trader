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
./run.sh performance-check
./run.sh help
```

日常启动不需要参数。`help` 会把只读检查与离线研究命令分组说明；离线研究命令不会随服务启动自动
执行。`performance-check` 禁止外网并直接测量活动生产标准化、合并、三策略评分、overlay CAS、
API/ETag/status、SSE 和 100 tick RSS；可用 `--output` 保存报告，或用 `--baseline` 执行 5% 相对回归门禁。

启动脚本只读取 `TRADER_HOST` 和 `TRADER_PORT`；旧 `HOST`/`PORT` 不再映射到 V2 进程。

## 荐股漏斗诊断

服务运行期间，可连续采样只读 Web API，检查荐股漏斗是否真的在上游阶段停滞：

```bash
.venv/bin/python scripts/check_web_recommendation_health.py \
  --base-url http://127.0.0.1:5000 \
  --samples 6 \
  --interval-seconds 5 \
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

正常重启会重新预热行情、候选、观察池和研究/review 等纯内存状态；正式推荐、合法检查点、
预算、V2 数据平面和收盘 overlay 按各自持久化契约恢复。强制结束进程或断电属于异常终止，
正式冻结会在下次启动时按恢复载荷和 SHA-256 校验恢复或 fail closed。

## 手动安装

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/trader-cli --config "$PWD/config/v2/runtime.json" validate-config
.venv/bin/trader-server --config "$PWD/config/v2/runtime.json"
```

配置路径必须为绝对路径。`TRADER_CONFIG` 可代替 `--config`。DeepSeek 密钥优先从
`DEEPSEEK_API_KEY` 读取，也可使用 `DEEPSEEK_API_KEY_FILE` 或项目根目录
`.token_key` 的 `DEEPSEEK_API_KEY` 字段；密钥不写入配置、快照或日志。

## Tushare 慢数据

当前 120 积分档只用 Tushare Pro SDK `daily` 批量未复权日线做能力审计和来源健康观测，
不进入活动历史特征链，也不承担高频实时报价；证券主数据、交易日历、复权因子、日度估值和
财务指标等更高积分能力不调用。SDK 已由 `pyproject.toml` 作为默认运行依赖安装；Token 缺失时显式降级。
项目根目录 `.token_key` 同时保存两个独立字段：

```bash
DEEPSEEK_API_KEY=your-deepseek-key
TUSHARE_TOKEN=your-tushare-token
```

Token 优先从 `TUSHARE_TOKEN` 读取，其次读取 `TUSHARE_TOKEN_FILE`，最后读取
`config/v2/runtime.json` 中 `market_data.tushare.token_file` 指向的赋值文件，默认即
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

- `GET /api/v2/decisions/<today|tomorrow|d25|long>/current`
- `GET /api/v2/decisions/<today|tomorrow|d25>/history?date=YYYY-MM-DD`
- `GET /api/v2/decisions/<today|tomorrow|d25>/dates`
- `GET /api/v2/status`
- `GET /api/v2/events`

当前快照支持 ETag。SSE 使用单调事件 ID 和 `Last-Event-ID` 恢复；游标过旧时返回 `resync_required`。Web 请求只读取已发布快照，不抓行情、不评分、不调用 DeepSeek。
Long 的三个固定分类和股票身份由打包资源立即显示；V2 current 只增强实时行情，服务或行情暂时
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
[软件业务设计文档](docs/software-business-design.md)，候选、过滤、评分、DeepSeek、融合与
TopK 契约见[荐股策略文档](docs/recommendation-strategy.md)，协作与强制 review 流程见
[AGENTS.md](AGENTS.md)。

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
docs/               软件业务设计与荐股策略两份权威文档
scripts/            工程辅助脚本
src/trader/         唯一活动产品包
tests/              单元、组件、契约和集成测试
.runtime/v2/        本地运行数据，不进入 Git
```
