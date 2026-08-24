# V2 本地启动与安全关闭

本文只说明当前 V2-only release 的本地运行方式。产品、架构、时间线、API 与验收契约以
`docs/software-business-design.md` 为准。

## 1. 启动前提

- 使用 Python 3.10 至 3.14，并在仓库根目录创建 `.venv`。
- 运行配置固定为绝对路径 `config/v2/runtime.json`；该配置把运行目录设为 `.runtime/v2`。
- 可选的 DeepSeek 密钥从环境变量、密钥文件或本地 `.token_key` 读取，不写入仓库。
- 浏览器关闭不会停止服务；服务是本机独立进程。

首次安装与配置校验：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/trader-cli --config "$PWD/config/v2/runtime.json" validate-config
```

## 2. 启动服务

推荐使用仓库脚本：

```bash
./run.sh
```

无参数运行就是日常启动；`./run.sh serve` 是完全等价的显式写法。执行 `./run.sh help` 可查看分组
帮助。方括号中曾经平铺展示的名称不是需要同时填写的启动参数，而是互斥的独立子命令：

| 类别 | 子命令 | 用途 |
| --- | --- | --- |
| 日常 | 无参数或 `serve` | 启动本地 Web 看板和生产 V2 调度 |
| 日常 | `validate-config` | 校验运行与策略配置后退出，不启动服务 |
| 日常 | `research-status` | 只读查看研究数据覆盖和阻塞状态，不抓网络、不写研究工件 |
| 离线研究 | `research-history-download` | 下载并断点续传独立的历史日线归档 |
| 离线研究 | `research-backtest` | 只读历史归档并运行固定训练/验证回测 |
| 离线研究 | `research-r6-screen` | 运行并不可变封存 R6 历史参数筛选 |
| 离线研究 | `research-r6-daily-screen` | 运行并不可变封存 R6 日线趋势筛选 |
| 离线研究 | `research-r6-stability-screen` | 运行并不可变封存 R6 排名稳定性诊断 |
| 离线研究 | `research-r7-dossier --research-identity <ID>` | 为已具备资格的 R6 前向身份生成待人工审查档案 |

普通看盘只需运行 `./run.sh`，不需要使用任何 `research-*` 命令。未知命令会在创建虚拟环境或安装
依赖之前快速退出，并提示 `./run.sh help`，避免拼写错误触发无关环境变更。

也可以直接运行安装后的入口：

```bash
.venv/bin/trader-server --config "$PWD/config/v2/runtime.json"
```

启动后访问 `http://127.0.0.1:5000/`。同一运行目录只允许一个服务进程；重复启动会由
`.runtime/v2/server.lock` 拒绝。

源码更新后，先在原服务终端按一次 `Ctrl+C` 并等待正常退出，再运行
`./run.sh validate-config` 和 `./run.sh serve`。直接再次执行 `./run.sh serve` 不会热替换旧进程；
如果页面显示“服务版本不一致”，说明浏览器资源与常驻 Python 进程并非同一 release，继续等待
观察草稿没有意义。重启后 `/api/v2/status` 必须返回 `v2_status_v2`，且
`release.decision_view_schema=v2_decision_view_v2` 与页面资源 revision 一致。

服务启动采用失败开放：外部行情、交易日历、Tushare 或 DeepSeek 暂不可用时，Web 仍可读取
最近有效 V2 快照并显示降级。Long 页的卡脖子、高成长、低价潜力三个固定分类随 wheel 打包，
即使实时接口暂不可用也会显示股票身份；价格等行情字段显示 `--`，不会伪造实时数据。

## 3. 状态检查

```bash
curl -fsS http://127.0.0.1:5000/api/v2/status
curl -fsS http://127.0.0.1:5000/api/v2/decisions/long/current
```

只读接口包括：

- `GET /api/v2/decisions/<today|tomorrow|d25|long>/current`
- `GET /api/v2/decisions/<today|tomorrow|d25>/history?date=YYYY-MM-DD`
- `GET /api/v2/decisions/<today|tomorrow|d25>/dates`
- `GET /api/v2/status`
- `GET /api/v2/events`

HTTP 请求只读取应用层快照，不抓行情、不评分、不调用 DeepSeek。

## 4. 安全关闭

在启动服务的终端按一次 `Ctrl+C`，或向进程发送正常 `SIGTERM`。第一次信号启动全进程共享的
30 秒安全关闭期限：停止 Web 接收、调度、数据源和工作池，并等待已接纳任务在期限内收尾。

关闭过程中再次发送关闭信号会立即强制退出；任务管理器强制结束和断电也属于异常终止。
Linux/macOS 的正常 `SIGTERM` 与 Windows `SIGBREAK` 使用同一关闭语义。

## 5. 持久化与恢复边界

当前 release 只读写 `.runtime/v2`，其中 V2 数据平面、正式决策和 DeepSeek 预算使用彼此独立的
持久化文件；预算文件名为 `deepseek-budget.sqlite3`。新 release 不读取、迁移或回放旧运行目录、
旧数据库、旧快照或旧 schema。

正式冻结记录按不可变身份、恢复载荷和 SHA-256 校验恢复；损坏或身份冲突时 fail closed。
行情预热、候选、观察池、研究/review、backoff 与 breaker 等纯内存状态在重启后重新建立。

回退只能整体启动对应的旧 release，并让它使用自己的旧运行目录。禁止让旧 release 写入
`.runtime/v2`，也禁止把 V2 文件复制回旧目录。

## 6. 发布验证

代码交付运行：

```bash
make format-check
make lint
make type-check
make test
make package
```

最终 release 还需在仓库外安装生成的 wheel，验证 `trader-cli`、包导入和 Web 静态资源；使用真实
浏览器在 1280x720、1440x900、1920x1080 三档桌面视口检查无白屏、重叠和页面级横向溢出。
