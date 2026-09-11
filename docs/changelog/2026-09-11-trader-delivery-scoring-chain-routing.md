# Delivery Record: Trader Delivery Scoring-Chain Routing

## User Request

用户确认按计划优化仓库级 `trader-delivery` Skill 的主规则与引用指南，并完成定向验证、最终 Review、记录、
独立提交和推送。当前评分链批次已经先以独立提交闭合，本批不修改产品运行代码、评分参数或用户数据。

## Cause

- 评分链已经把 Tomorrow 模型相对分从生产 `base_score` 中分离，并新增三策略统一证据质量标尺、独立成本执行门、
  `score_scale` 身份及新旧冻结展示边界；既有 Skill 的单行评分影响矩阵没有强制追踪这些不同语义所有者。
- Skill 仍引用已删除的英文权威文档、旧调度模块和测试、退役状态 API、旧配置路径、旧诊断 schema 及旧类型名，
  会把后续交付路由到不存在或不再活动的边界。
- 原 Skill 契约只验证文件可发现和少量关键词，没有验证 Markdown 引用、现行路径或评分语义；既有稳定命名合同也
  没有扫描仓库 Skill，因此这些漂移仍能通过测试。

## Added

- 新增按需读取的 `scoring-chain.md`，固定从点时资格、统一证据质量分、风险、模型诊断、成本门、DeepSeek、融合、
  动作与排名，一直追踪到决策身份、冻结、持久化、GET/SSE/Web 和离线收益证据。
- 新增 Skill 引用解析、现行权威文档/运行模块/调度测试/配置路径存在性、退役名称禁止及评分关键不变量契约；稳定
  命名扫描范围扩展到 `.agents/skills`。

## Changed

- 主 Skill 现在读取 `docs/01_评分逻辑.md` 与 `docs/02_工程设计.md`，按需读取工程实施和策略回溯文档，并从根
  Changelog 继续检索 `docs/changelog/` 的当前交付记录。
- 影响矩阵把评分拆为候选与证据质量、模型诊断与成本执行门、风险/融合/动作/排名、决策身份/冻结/持久化、
  API/SSE/Web 和离线研究边界，明确语义或身份变化会传播到全部下游消费者。
- 运行诊断指南改用 `/api/status`、`config/runtime.json` 和稳定 `trader-runtime-diagnostics` 身份；推荐漏斗事故
  手册改用当前 `RefreshOutcome` 与稳定运行术语。

## Fixed

- 修复 Skill 虽能被加载却会读取不存在文档、调用退役路径并遗漏统一评分语义、旧冻结兼容和收益授权门的问题。
- 修复契约测试只能证明 Skill “存在”，不能证明其引用和路由仍有效的门禁盲区。

## Removed

- 移除 Skill 中旧英文文档路径、非评分项目版本命名及退役 API、配置、测试和类型引用；未删除任何产品代码、运行
  数据、评分工件或历史记录。

## Verification

- 契约先行首次运行产生 4 个失败：缺少评分链指南、现行契约路由缺失、评分不变量未覆盖，以及稳定命名扫描发现
  5 处 Skill/测试旧命名；实现后相关契约共 11 项通过。
- 受影响两个 Python 契约文件 Ruff 检查通过且格式已符合要求；`git diff --check` 通过。
- Skill Creator 的 `quick_validate.py` 使用系统 Python 返回 `Skill is valid!`。项目虚拟环境首次执行仅因验证器自身
  依赖的 `PyYAML` 未安装而无法启动，本批没有为仓库增加无关依赖。
- 统一运行诊断 `scripts/diagnose_runtime.py --help` 成功，现行 profiles、`--runtime-config` 与其它公开参数可见。
- 完整 `make test`、打包、wheel 安装、真实供应商、运行服务和三档浏览器门禁不适用：本批只修改代理工作流
  Markdown 及其静态契约，不改变产品运行、构建、资源或页面行为。

## Residual Risks

- 自动化合同可以阻止引用和关键语义再次静默漂移，但 Skill 的实际效果仍取决于后续代理按任务类型加载相应引用并
  取得真实运行或收益证据；语法验证和关键词合同不能替代独立业务 Review。
- 本批不调整评分公式、权重、动作线、V1/V2/V3、DeepSeek、冻结或生产权限，也不宣称提高投资收益。
- `Regression-Key: trader-delivery-scoring-chain-routing`。
