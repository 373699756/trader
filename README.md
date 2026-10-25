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
| 锁、日志和临时产物 | runtime_dir，默认.runtime/trader/ |

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
| 更新参考、完整历史、在线日线和公司资料 | ./run.sh sync |
| 只维护完整历史，不自动训练 | ./run.sh sync history |
| 只更新在线日线 | ./run.sh sync market |
| 只更新公司资料 | ./run.sh sync company |
| 维护一次完整历史后训练全部档位 | ./run.sh train |
| 只训练指定档位 | ./run.sh train v1,v5 |
| 只启动V3 | ./run.sh v3 |
| 同进程启动任意组合 | ./run.sh v1,v2,v3 |

档位直接用逗号分隔，无需--profile；不再使用both。Web只显示已启动档位的按钮，一次查看一个档位；切换视图不会停止其它档位。

首次使用建议：

1. 运行sync，依次准备参考、完整历史、在线短窗口和公司资料；待补时按原因修复或续传。
2. 运行train，检查或补齐历史后处理全部档位；也可指定train v2,v3。各档按既有规则训练或复用，来源或样本不足时不会发布合格模型；V1已获人工授权正式接入，V5保持工程Shadow并需要可选CPU依赖。
3. 正常启动或重启服务，加载所选档位模型。
4. 查看页面输入准备和诊断，确认是否真正达到评分条件。

train可能下载大量历史，不是普通启动的前置脚本。
sync的某一分项失败或待补仍执行后续分项，取消或公共准备失败停止；stdout为最终JSON，进度走stderr。
退出码0/1/2/130分别为完成、失败或待补、参数/配置错误、取消。

单项命令也会准备公共参考，只限制其后的业务范围；只缺某类资料时使用对应单项，不必先运行sync再把所有单项重复运行。train不更新在线短窗口或公司资料。
同一业务的写入受锁保护，冲突会拒绝执行，不会自动排队；不要在sync运行时再启动其分项，也不要同时运行train与sync history。训练期间持有历史锁，market和company则使用各自的数据锁。

跨日重跑会重新检查参考并消费已有断点：history先完成旧截止批次再追至新截止；market校验已提交的两档窗口后补缺；company保留日期片和页码，并安排新增日期、近期复查及正文待补。完成条件和复查窗口见[下载训练](docs/4_下载训练.md)，有断点不等于重跑没有网络请求或能立即完成。

### 2.3 当前后台行为与目标

| 事项 | 当前行为 |
| --- | --- |
| 公司资料、参考和在线短窗口 | 手工sync维护，启动时加载本地投影 |
| 完整历史 | 手工sync history或train前置维护；旧系统任务使用显式卸载命令移除 |
| 模型更新 | 手工train后正常重启，不热加载或自动切档 |
| 慢资料变更检查 | 启动预读完成后线程退出，运行中不轮询数据库或模型文件变化 |

普通run.sh只启动推荐服务，下载和训练由用户另行执行。当前共享逻辑缓存为512 MiB，推荐进程RSS验收预算为2 GiB，实际性能证据见[工程实施](docs/3_工程实施.md)及[工程设计第7节](docs/2_工程设计.md#7-当前运行内存缓存与性能契约)。

## 3. 配置与手动安装

~~~bash
python3 -m venv .venv
.venv/bin/python3 -m pip install -e ".[dev]"
.venv/bin/trader-cli --config "$PWD/config/runtime.json" validate-config
.venv/bin/trader-server --config "$PWD/config/runtime.json"
~~~

DeepSeek开关位于[config/deepseek.json](config/deepseek.json)的deepseek.enabled，当前默认false。正常重启后关闭结构化复核，跳过DeepSeek的凭据加载、不发起请求、不消耗物理预算，合法本地推荐继续。Tushare的独立凭据校验仍生效。需要启用时显式改为true并配置密钥，再正常重启；JSON布尔不加引号。

修改配置后先执行上面的validate-config命令。配置hash随有效内容变化是正常行为，无需手工修改或保持旧hash。当前加载器会拒绝未知字段、错误类型，以及与固定合同不一致的调度、缓存、模型或预算值；这类字段不是任意可调参数，具体边界见[工程设计第3.3节](docs/2_工程设计.md#33-配置修改与启动校验)。配置不热加载，校验通过后正常重启生效。

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
.venv/bin/python3 scripts/diagnose_runtime.py --profile local --output -

# 运行中的服务和数据源检查
.venv/bin/python3 scripts/diagnose_runtime.py --profile live \
  --base-url http://127.0.0.1:5000 --output -
~~~

统一脚本的专项参数见[scripts说明](scripts/README.md)。报告需要保存时使用显式仓库外路径。

| 现象 | 先检查什么 |
| --- | --- |
| 没有推荐 | 输入准备、主要阻断原因；区分数据不足和合法业务空集 |
| 模型未就绪 | 所选档位双头是否完成训练、是否重启；文件存在不等于身份合格 |
| 日线待更新 | sync market结果、窗口截止和逐股缺失；不依靠反复重启补下载 |
| 公司风险覆盖不足 | sync company页/正文待补及来源失败，不能当作无风险 |
| local诊断model_unavailable | 缺模型，未完成性能测试；不是性能通过 |
| 旧进程持锁 | 在原终端正常关闭，不删除server.lock |
| 源或DeepSeek失败 | 保留最近有效结果，查看来源年龄与明确降级原因 |

缺少qfq不触发评分线程现场下载；只读Web请求不抓行情、训练或评分。

## 5. 关闭与页面行为

1. Ctrl+C开始共同最长30秒的安全关闭，再次中断可强制退出；关闭浏览器不停止服务。
2. SIGTERM和Windows SIGBREAK使用同一正常关闭流程。
3. 页面显示同轮评分前六及推荐/关注/回避状态；模型只作诊断和成本门，DeepSeek只作结构化风险否决。
4. Tomorrow/D25在15:00首次正式冻结后不被迟到结果覆盖；后续报价更新不改变名单和分数。
5. Long为固定研究名单当前行情，不评分、不冻结。
6. 双档短线结果和历史分别隔离，Long由V2单一采集供只读展示。

完整路由、ETag、SSE与恢复语义见[工程设计第9节](docs/2_工程设计.md#9-唯一-web-api-与-sse)。

## 6. 检查与文档

~~~bash
make format-check
make lint
make type-check
make test-full
make package
~~~

make test是快速集，不代替全量。发布另需仓库外wheel安装及真实专项验收，不能只凭HTTP200或构建成功。

| 需要了解 | 入口 |
| --- | --- |
| 过滤、因子、公式和收益验证 | [评分逻辑](docs/1_评分逻辑.md) |
| 架构、内存、调度、API及发布 | [工程设计](docs/2_工程设计.md) |
| 当前任务、证据和未完成项 | [工程实施](docs/3_工程实施.md) |
| 下载、训练、数据布局和恢复 | [下载训练](docs/4_下载训练.md) |
| 人工审查问题和判断方法 | [人工审查](docs/5_人工审查.md) |
| 协作和Git规则 | [AGENTS.md](AGENTS.md) |

源码只在src/trader；config保存配置，tests保存测试，scripts保存既有诊断和维护工具。
业务数据、虚拟环境、缓存、构建产物和临时报告不进入提交。
