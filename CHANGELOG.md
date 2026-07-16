# Changelog

All notable changes to this project are documented here.

## Unreleased

### Added

- 新建单一 `src/trader` 安装包，按 `domain`、`application`、`infrastructure`、`web`、`entrypoints` 和唯一组合根分层。
- 新增 today、tomorrow、d25、long 四策略的确定性评分、硬过滤、风险事实去重、TopK 和动作判定。
- 新增东方财富/新浪/腾讯行情适配、AKShare 研究边界、交易日历、历史特征缓存和多源降级。
- 新增 DeepSeek 五维 schema、证据子集校验、共享代际缓存、逐物理请求原子预算及 188 次六桶上限。
- 新增 SQLite/不可变 JSON staged-committed 冻结协议、哈希校验、隔离恢复、优先事件重放和跨平台单进程锁。
- 新增只读推荐 API、ETag、有界审计查询、SSE 游标恢复/慢客户端隔离及包内桌面工作台资源。
- 新增分层单元、组件、契约和集成测试，以及根级 `AGENTS.md`、迁移清单和 v2 运行手册。

### Changed

- 项目入口统一为 `trader-server` 和 `trader-cli`；Linux/macOS/WSL、PowerShell 和 CMD 启动脚本只调用安装后的入口。
- 依赖、构建、包发现、console scripts、Ruff、mypy 和 coverage 统一由 `pyproject.toml` 管理。
- 运行配置迁移到 `config/v2`，运行数据隔离到 `.runtime/v2`，配置路径必须显式且为绝对路径。
- 最终分固定为 `clamp(local_score * 0.68 + deepseek_score * 0.32 - deepseek_risk_penalty, 0, 100)`，并以 `ROUND_HALF_UP` 保留两位。
- Web 产品范围固定为个人 PC 桌面浏览器；发布验收分辨率为 1280x720、1440x900 和 1920x1080，手机和平板不在范围内。
- v1 需求、设计、研究登记和配置移入 `docs/archive/v1`，`docs/need.md` 成为唯一活动业务契约。

### Fixed

- 防止本地风险在 68/32 融合中重复扣除；固定向量 `82 - 2 / 100 - 3` 得到 `83.40`。
- 融合保留未舍入本地分精度到最终计算，修正临界值被提前舍入抬高 0.01 的问题。
- 修正 d25 在 20 日涨幅恰好 30% 时应使用 0.85、仅高于 30% 才使用 0.75 的边界。
- 定向报价刷新后再次执行硬过滤，并沿用同版本全市场横截面分位，避免过热/过期股票继续评分和候选内重排漂移。
- DeepSeek 每次重试前重新检查 14:48 截止，完成时间等于截止也按 late 处理；429、超时和成功逐物理请求独立记账。
- 冻结事件改用保留优先级、入队前持久化并支持重启重放；冻结事务同步提交当前发布指针，消除 commit 后 publish 前退出的旧草稿窗口。
- 过期交易日历刷新失败时严格 fail-closed，不再使用超过有效期的日期猜测交易日。
- 配置拒绝 NaN/Infinity，启动时锁定五维键、预算桶、阈值键和 0.68/0.32 融合契约。
- SSE 对超前或过期游标统一要求 resync；慢客户端不会阻塞发布线程。
- 修正桌面表头覆盖首行以及 Tab/SSE 在途请求竞态，迟到响应不再覆盖用户当前策略。

### Removed

- 删除活动 `stock_analyzer` 包、根 `app.py`、旧 static/templates、旧配置和重复 requirements。
- 删除验证、回测、自动调参、预测、paper trading、OOS/实验功能及其 Web 路由、资源和旧测试。
- 删除根 `analysis`、`experiments` 活动产物和旧依赖指纹脚本；有保留价值的资料仅归档，不进入 wheel。

### Verification

- `make quality`：Ruff format/lint、58 个源文件 mypy 和 106 个 pytest 测试全部通过。
- `make package`：从干净生成目录成功构建 sdist 和 `py3-none-any` wheel；sdist 不包含旧包或旧测试。
- 仓库外 `/tmp` 虚拟环境覆盖安装 wheel 后，`trader.__file__` 位于 site-packages，CLI 配置校验、首页和进程锁导入通过。
- wheel 内模板、CSS、两个 JavaScript 和 SVG 均可通过包资源读取，`create_app().test_client().get('/')` 返回 200。
- 无界面 Chrome 在 1280x720、1440x900、1920x1080 下均渲染 3 行 fixture，页面无横向溢出，抽屉在视口内且无脚本异常。
- 浏览器竞态测试通过：延迟 today 响应后立即切换 tomorrow，迟到响应未覆盖当前 Tab。
- `./run.sh validate-config`、架构 AST、无副作用 app factory、冻结恢复、预算并发和 SSE 慢客户端契约均已纳入门禁。

### Residual Risks

- 尚未完成一个真实 A 股完整交易日的 v2 影子运行，因此 TopK 报价 P95、冻结点实时时延和阈值分布仍需在生产发布前留证。
- 当前环境未提供真实 `DEEPSEEK_API_KEY`，已完成 mock 的 429/超时/schema/截止/预算测试，但真实 API 冒烟尚未执行。
- 当前 Linux 环境没有 PowerShell，`run.ps1`/`run.bat` 已静态审查，仍需在 Windows PC 实机验证创建虚拟环境、单进程锁和 Ctrl+C 停止。
- 外部行情提供方可能发生字段或限流变化；组件测试使用脱敏固定响应，首次真实运行应观察来源覆盖、熔断和降级状态。
