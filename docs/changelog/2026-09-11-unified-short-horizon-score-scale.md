# Delivery Record: Unified Short-Horizon Score Scale

## User Request

用户要求在继续后续计划前修复 Tomorrow 分数显著偏高的问题，并明确 Today、Tomorrow、D25 必须放在同一标准下
评分，不能继续一个天然很高、另一个很低。

## Cause

- 根因确认在评分所有权而非页面格式：Tomorrow 的生产模型先把同批预测超额收益按横截面名次映射到 0–100，
  再通过 `ScoredModelOverrides` 覆盖公共领域评分器的 `base_score`；任意非空批次因而天然产生接近 100 的最高分。
- Today 与 D25 使用固定因子含义和板块权重合成证据质量分。页面把这类分数与 Tomorrow 批内相对名次并列为最终分，
  所以相同数值没有相同含义，同一股票的 Tomorrow 分数还会受当批股票集合影响。
- 前一批只把页面改为“策略内评分”并解释成本门，没有消除评分内核的双标尺。用户明确拒绝该处理后，本批按
  `unified-short-horizon-score-scale` 缺陷族修复第一处错误所有者。

## Added

- 新决策在 `input_versions.score_scale` 绑定稳定的 `weighted_evidence_quality_0_100` 标尺身份；GET、SSE、冻结记录
  和未就绪草稿原样投影该身份。
- 增加模型相对信号与统一最终分分离、成本门不改分、新旧冻结解释、草稿标尺投影和三份相关文档一致性的回归合同。

## Changed

- Today、Tomorrow、D25 统一由公共领域评分器按已登记组件与板块权重生成 `base_score`：0 表示证据明显不利，
  50 表示中性，100 表示证据明显有利。各持有期仍使用适合自身目标的因子与权重，但同一数值共享质量强弱含义。
- Tomorrow V1/V2/V3 继续输出预测超额收益、模型分歧和批内相对信号，并继续执行 20–40bp 成本净效用门；模型
  相对名次移入类型化诊断，不再覆盖 `base_score`、`local_score` 或 `final_score`，也不单独决定 73/78 动作线。
- Web 卡片改为“统一评分最高”，逐股抽屉把模型项明确标为“模型相对信号分”。新标尺 Tomorrow 即使被成本门
  拦截也显示统一最终分与独立成本状态；缺少标尺身份的旧冻结记录继续按原相对分只读解释。
- `01_评分逻辑.md`、`02_工程设计.md` 和 `04_策略回溯.md` 已同步评分所有权、模型职责、API 草稿身份和页面解释。

## Fixed

- 修复 Tomorrow 每批最高预测名次天然接近 100、却与 Today/D25 规则分并列比较的双标尺问题。
- 修复未就绪页面虽然使用新决策草稿分数，却因草稿未公开评分标尺身份而仍显示“策略内最终评分”的遗漏。

## Removed

- 删除 `ModelScoreBatch.scores`、`ScoredModelOverrides` 及模型覆盖公共本地分的分支；不保留双实现或隐藏回退。
- 未删除模型预测、成本门、风险、DeepSeek、冻结记录或历史数据，也未新增评分档位或调整 V1/V2/V3 身份。

## Verification

- 已补文档消费者合同并先复现失败；实现同步后通过。草稿 `score_scale` API 与页面身份测试同样先分别因字段缺失
  和误标失败，实现后通过。
- 评分模型、选择、融合、冻结、持久化、查询、SSE、Web 与配置权重定向回归共 167 项通过；JavaScript 页面状态
  合同通过；受影响 Ruff 与全包 mypy 通过；`git diff --check` 通过。
- 生产性能门禁通过：1080 个三策略候选、5 轮测量、`network_calls=0`、内存增长 0%，绝对/相对/分配预算及
  cache cold/hot、冻结边界、latest-wins、部分来源失败、随机输入顺序等价性全部通过。
- Firefox 刷新门第一次可执行测量的 patch-to-paint P95 为 125ms，超过 100ms 预算；确认无残留浏览器进程后
  同一固化门禁复跑通过，DOM 刷新 P95 1.083 秒、patch-to-paint P95 12ms，决策 patch 与 35 秒保留窗口通过。
- 参数化桌面浏览器验收通过：1280x720、1440x900、1920x1080 均无白屏、浏览器错误、重叠或页面级横向溢出，
  页面可切换 Tomorrow/D25 草稿并保持分数降序；无外部网络请求。
- 完整高风险门禁通过：`make format-check`、`make lint`、`make type-check`、`make test`、`make package`。
  首轮格式检查发现并格式化一个新增合同断言；首轮全量测试发现两项旧双标尺文档断言，更新合同后完整复跑通过。
  首轮隔离打包仅因沙箱禁止连接构建依赖索引失败，获授权后同一命令在主机网络成功完成。
- 仓库外 wheel 安装验证通过：包从仓库外导入，公开 CLI 可执行，10 个模板、CSS、JavaScript、图标、模型和
  自动化资源可读。全量测试同时覆盖固定融合 `83.40`、架构 AST、`create_app()` 无副作用、预算并发、SSE 游标、
  冻结恢复和内容 hash 合同。

## Residual Risks

- 当前没有运行中的 `trader-server`/`run.sh` 进程；统一 runtime 诊断在沙箱内三次返回 `connection_failed`，主机
  进程检查也确认没有服务，因此无法提供“重启后的真实供应商进程已加载新代码”证据。该结果只表示 live gate
  不可用，不归因于评分、行情或 Web 缺陷。
- 已冻结旧 Tomorrow 记录不可改写且没有新 `score_scale` 身份，继续显示原批内相对分；下一次新评分决策才使用
  统一标尺。统一分数表达证据质量强弱，不等价于收益率，当前 73/78 等阈值仍是固定参数而非已证明的收益最优值。
- 固定 68/32 融合、风险只扣一次、DeepSeek 168 次物理请求上限、Top6、成本净效用门和冻结 first-wins 均保持不变。
- `Regression-Key: unified-short-horizon-score-scale`。
