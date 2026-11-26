# A股策略看板

个人PC上的A股研究工具：读取行情和本地资料，过滤、评分并通过只读Web展示Tomorrow、D25及Long。
结果仅供研究，不提供真实下单或收益保证。

## 1. 运行环境

| 项目 | 要求 |
| --- | --- |
| Python | 3.10–3.14 |
| 浏览器 | 当前稳定版Chrome、Edge、Firefox桌面版 |
| 默认地址 | http://127.0.0.1:5000 |
| 持久数据 | data_dir，默认data/ |
| 服务锁、日志和临时产物 | runtime_dir，默认.runtime/trader/ |

不需要Redis、Celery、Node或外部数据库。依赖和公开入口以[pyproject.toml](pyproject.toml)为准。

## 2. 启动和准备数据

### 2.1 一键启动

~~~bash
# Linux、macOS或WSL
chmod +x run.sh
./run.sh
~~~

~~~powershell
# Windows PowerShell
.\run.ps1
~~~

Windows CMD执行run.bat。脚本按需创建.venv并安装项目，默认同进程启动V1、V2、V3、V5，Web默认显示V2。
启动可以显示缺数据状态，不会自动训练；坏模型与缺模型分别报错，不能伪造预测。

### 2.2 日常命令

| 任务 | 命令 |
| --- | --- |
| 查看帮助 | ./run.sh help |
| 依次更新参考、完整历史和公司资料 | ./run.sh sync |
| 只维护完整历史，不自动训练 | ./run.sh sync history |
| 只更新公司资料 | ./run.sh sync company |
| 训练四档各自的 Tomorrow 和 D25 | ./run.sh train |
| 训练 V1、V5 各自的 Tomorrow 和 D25 | ./run.sh train v1,v5 |
| 训练 Tomorrow 的四个档位 | ./run.sh train tomorrow |
| 训练 D25 的四个档位 | ./run.sh train d25 |
| 只训练 Tomorrow 的 V3 | ./run.sh train tomorrow v3 |
| 只训练 D25 的 V5 | ./run.sh train d25 v5 |
| 只启动V3 | ./run.sh v3 |
| 同进程启动任意组合 | ./run.sh v1,v2,v3 |

档位直接用逗号分隔，无需--profile；不再使用both。Web只显示已启动档位的按钮，一次查看一个档位；切换视图不会停止其它档位。

Shell与PowerShell使用相同的精简命令和参数范围，Windows可将上表的`./run.sh`替换为`.\run.ps1`。训练JSON／诊断参数仍由独立`trader-cli`提供，不经启动脚本转发。

普通 `./run.sh sync company` 持续下载公告目录、正文和结构化公司资料，直到本轮工作处理完毕。默认无半小时、调度步数或累计请求次数限制。终端显示下载页数、正式字段覆盖、累计和未确认请求数。人工中断后重跑同一命令，程序沿原请求账本和游标续传。来源尚未披露、证据不足和无法解析的内容会标出原因。历史与行情沿用各自的缺价及停牌复核流程。[下载恢复说明](docs/d_下载设计.md#1-两个模块下载什么)

首次使用建议：

1. 运行`./run.sh sync`，准备参考后依次更新完整历史和公司资料。也可指定单项，待补时按原因补缺或续传。
2. 运行 `train`，用本地已发布历史分别训练四档，也可指定 `train v2,v3`。D25 使用本轮固定的最新有效行业映射。训练会检查工件复用与日期准入，资料不足时提示手动维护。真实质量和收益独立验收，旧 Shadow 记录保持原身份。V5 需要可选 CPU 依赖，使用 10 轮方案。
3. 正常启动或重启服务，加载所选档位模型。
4. 查看页面输入准备和诊断，确认是否真正达到评分条件。

train只使用本地已发布资料，不下载资料，也不是普通启动的前置脚本。价格截止、各头最新成熟训练日和预测特征截止各有含义，日期不足与进度口径见[训练设计](docs/e_训练设计.md#1-输入固定双头独立)。
日常 `./run.sh sync company` 更新每股最新可用的财报、质押和解禁观测，处理新披露或修订的公告，按各字段的证据发布。首次从当前来源取得可用值，沿用已保存的有效材料。后续从各市场已确认的公告日期续查。仅在主动执行 `./run.sh sync compnay fill` 时补采年度公告。

`sync`省略类型时依次执行history和company。指定类型时更新对应资料。`sync company`在stdout显示简短中文结果，进度和详细诊断写入stderr。`sync`、`sync all`和`sync history`的最终结果仍为JSON。
退出码0/1/2/130分别为完成、失败或待补、参数/配置错误、取消。

每个下载命令都会准备公共参考，只缺某类资料时使用对应命令。train不准备在线参考，不更新历史、在线短窗口或公司资料。
同一业务的写入受锁保护，冲突会拒绝执行。训练与 `sync history` 共享历史锁，公司资料使用独立的锁。

默认数据根目录 `data/` 下，完整历史位于 `history/`，证券、ST、日历和行业参考位于 `metadata/`。独立 QFQ 短窗文件 `daily_window.sqlite3` 与对应的 `daily_window.lock` 位于数据根目录。

跨日重跑会重新检查参考和已有断点。History 将旧批次的缺口与新增日期合并补齐。Company 保留日期片和页码，安排新增日期、近期复查及正文待补。完成条件和复查窗口见[下载设计](docs/d_下载设计.md)。

`config/selection.json` 的 `prediction_targets` 分别设置 V1/V2/V3/V5 的 `tomorrow_bps` 和 `d25_bps`。默认是 500 和 100 基点，分别表示次日收盘涨幅至少 5%，以及第 2 至第 5 个交易日相对信号日收盘的累计涨跌幅均值至少 1%。两项策略都按档独立训练和选股。改动后校验配置、训练对应档位并重启，正式历史仍按冻结时的阈值结算。

### 2.3 当前后台行为与目标

| 事项 | 当前行为 |
| --- | --- |
| 公司资料和参考 | 手工sync维护，启动时加载本地投影 |
| 完整历史 | 仅手工sync history维护，荐股启动从年度History预读；独立短窗由诊断入口维护 |
| 模型更新 | 手工train后正常重启，不热加载或自动切档 |
| 慢资料变更检查 | 预读线程按配置间隔观察正式发布身份，变更后提示重启，运行中保留启动时采用的资料与模型 |

普通run.sh只启动推荐服务，下载和训练由用户另行执行。当前共享逻辑缓存为512 MiB，推荐进程RSS配置预算为3 GiB，实际性能证据见[施工进度](docs/g_施工进度.md)及[配置与内存预算](docs/b_系统设计.md#73-完整配置清点与身份边界)。

## 3. 配置与手动安装

~~~bash
python3 -m venv .venv
.venv/bin/python3 -m pip install -e ".[dev]"
.venv/bin/trader-cli --config "$PWD/config/runtime.json" validate-config
.venv/bin/trader-server --config "$PWD/config/runtime.json"
~~~

DeepSeek开关位于[config/deepseek.json](config/deepseek.json)的deepseek.enabled，当前默认false。正常重启后关闭结构化复核，跳过DeepSeek的凭据加载、不发起请求、不消耗物理预算，合法本地推荐继续。Tushare的独立凭据校验仍生效。需要启用时显式改为true并配置密钥，再正常重启；JSON布尔不加引号。

修改配置后先执行上面的validate-config命令。配置hash随有效内容变化是正常行为，无需手工修改或保持旧hash。当前加载器会拒绝未知字段、错误类型，以及与固定合同不一致的调度、缓存、模型或预算值；这类字段不是任意可调参数，具体边界见[配置与身份边界](docs/b_系统设计.md#73-完整配置清点与身份边界)。配置不热加载，校验通过后正常重启生效。

| 变量 | 用途 |
| --- | --- |
| TRADER_CONFIG | 本次使用的绝对配置路径 |
| TRADER_HOST、TRADER_PORT | 本次服务监听地址、端口 |
| DEEPSEEK_API_KEY / DEEPSEEK_API_KEY_FILE | 可选结构化风险复核密钥 |
| TUSHARE_TOKEN / TUSHARE_TOKEN_FILE | 可选daily研究审计凭据 |

配置也可指定.token_key中的对应字段；环境变量优先。POSIX下用chmod 600 .token_key限制权限，不将密钥提交到Git。
Tushare不承担生产qfq或财务补缺；无密钥时按其职责降级，不阻塞合法本地结果。
旧HOST/PORT不映射到启动进程。

## 4. 诊断和常见问题

~~~bash
# 不需要运行服务的本地检查
.venv/bin/python3 scripts/tools.py --business recommendation --module pipeline --action snapshot --report -

# 运行中的服务和数据源检查
.venv/bin/python3 scripts/tools.py --business recommendation --module web --action health \
  --base-url http://127.0.0.1:5000 --report -
~~~

统一工具的专项参数见[scripts说明](scripts/README.md)。`--list`列出全部业务、模块和动作；报告通过`--report`保存到显式仓库外路径，业务原有`--output`仍表示工件。

| 现象 | 先检查什么 |
| --- | --- |
| 没有推荐 | 输入准备、主要阻断原因；区分数据不足和合法业务空集 |
| 模型未就绪 | 检查所选档位的 Tomorrow、D25 工件及服务重启后的加载状态 |
| 日线待更新 | sync history结果、发布截止和逐股缺失；不依靠反复重启补下载 |
| 公司风险覆盖不足 | sync company页/正文待补及来源失败，不能当作无风险 |
| local诊断model_unavailable | 缺模型，未完成性能测试；不是性能通过 |
| 旧进程持锁 | 在原终端正常关闭，不删除server.lock |
| 源或DeepSeek失败 | 保留最近有效结果，查看来源年龄与明确降级原因 |

缺少qfq不触发评分线程现场下载；只读Web请求不抓行情、训练或评分。

## 5. 关闭与页面行为

1. Ctrl+C开始共同最长30秒的安全关闭，再次中断可强制退出；关闭浏览器不停止服务。
2. SIGTERM和Windows SIGBREAK使用同一正常关闭流程。
3. 页面显示同轮评分前六及推荐/关注/回避状态；V1/V5按各自模型排序分选席，V2/V3按local_score选席；模型净效用仍作为准入门，DeepSeek只作结构化风险否决。V1/V5已有人工生产授权，收益验证另验，旧Shadow记录保持原身份。
4. Tomorrow/D25在15:00首次正式冻结后不被迟到结果覆盖；后续报价更新不改变名单和分数。
5. Long为固定研究名单当前行情，不评分、不冻结。
6. 所选各档短线结果和历史分别隔离；包含V2时由V2单一采集Long，否则由所选列表首档采集，供全部视图只读展示。

完整路由、ETag、SSE与恢复语义见[Web、API与SSE](docs/b_系统设计.md#8-web-与-api-详细设计)。

## 6. 检查与文档

~~~bash
make format-check
make lint
make type-check
make test-full
make package
~~~

make test是快速集，不代替全量。发布另需仓库外wheel安装及真实专项验收，不能只凭HTTP200或构建成功。

文档统一在docs根目录维护，设计中的目标、候选与已实现行为分别标明；目录迁移不表示候选已启用或业务、收益、性能及发布验收通过。完整导航见[文档目录](docs/README.md)。

| 需要了解 | 入口 |
| --- | --- |
| 过滤、因子、公式和收益验证 | [评分逻辑](docs/c_评分设计.md) |
| 架构、内存、调度、API及发布 | [工程设计](docs/b_系统设计.md) |
| 当前任务、证据和未完成项 | [施工进度](docs/g_施工进度.md) |
| 下载、数据布局和恢复 | [下载设计](docs/d_下载设计.md) |
| 训练输入、样本和模型工件 | [训练设计](docs/e_训练设计.md) |
| 人工审查问题和判断方法 | [人工审核](docs/f_人工审核.md) |
| 协作和Git规则 | [AGENTS.md](AGENTS.md) |

源码只在src/trader；config保存配置，tests保存测试，scripts保存既有诊断和维护工具。
业务数据、虚拟环境、缓存、构建产物和临时报告不进入提交。
