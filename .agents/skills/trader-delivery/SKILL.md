---
name: trader-delivery
description: "规划、实施或审查 Trader 仓库改动，按风险核对业务影响、消费者和交付证据。仅解释日志且无需仓库检查时不使用。"
---

# Trader Delivery

## 先确定任务模式

遵守用户要求和仓库 AGENTS.md。本 skill 只负责路由、业务影响和交付证据；Python 设计与编码使用 [Python engineering guide](../python-engineering-guide/SKILL.md)，不重复执行两份流程。

| 用户当前任务 | 本轮产物与边界 |
| --- | --- |
| 规划、讨论方案，或宿主处于 Plan Mode | 只读调查与方案；不登记实施、不修改文件、不提交或推送 |
| 只读审查、诊断 | 有位置和证据的发现、未验证项；不自动转为修复或发布 |
| 已授权实施 | 完成实现、适用门禁与 AGENTS.md 要求的单批交付；已有授权不重复询问 |

只在进入实施时登记阶段；小范围修改沿用轻量路线。读取过 skill 不是完成证据，检查未运行不能写成通过。

## 开始与资料选择

先确认分支、上游、已暂存与未暂存修改及本批范围，保留其它任务的修改。按实际影响读取权威章节：docs/1_评分逻辑.md 管评分和收益准入；docs/2_工程设计.md 管产品、架构、运行、API/Web和验收；docs/4_下载训练.md 管下载、历史、训练和回溯。导航见 docs/README.md，进度与证据只维护 docs/3_工程实施.md，不恢复旧 Changelog 所有者。

| 触发情形 | 此时读取 | 要获得的证据 |
| --- | --- | --- |
| 文档、skill、元数据且无运行合同变化 | [delivery evidence 的轻量路线](references/delivery-evidence.md) | 精确范围、完整 diff、引用或示例有效性 |
| 行为、缺陷或跨边界变更 | [change-impact matrix](references/change-impact-matrix.md) 和 [delivery evidence](references/delivery-evidence.md) | 首个变化或故障边界、owner、全部受影响消费者与验收条件 |
| 候选、评分、模型诊断、成本门、风险、排名或决策身份 | [scoring-chain guide](references/scoring-chain.md) | 语义从 owner 到冻结、查询及外部投影的完整传递 |
| 运行、供应商、历史、性能或浏览器交付 | [runtime diagnostics](references/runtime-diagnostics.md) | 运行身份、真实探测范围、失败类别和未验证门 |
| 推荐为空、collecting停滞、漏斗或Web状态不一致 | [funnel incident playbook](references/recommendation-funnel-incidents.md) | 分配根因前完成六个检查点，不因首个探测看似明确而省略 |
| qfq供应商成功但pending或数据库与检查点不一致 | [qfq publication playbook](references/qfq-publication-incidents.md) | 下载、配对校验、双档发布、检查点确认及锁竞争分别核实 |

文档中提及运行行为本身不要求启动真实探测；只加载当前任务命中的资料。

## 实施时保留可检查事实

- 缺陷先区分已证实事实与假设；记录用户症状、首个故障边界、根因是否确认。只有复发缺陷族使用已有稳定 Regression-Key。
- owner、表示、资源编排或时间跨模块时，比较局部修复与整体修复；按证据选择。迁移同步新 owner、全部消费者并删除旧活动路径。
- 完成实现切片后再补实际风险需要的最小回归。命名与原子改名只遵守 [Python 命名权威](../python-engineering-guide/references/coding-standard.md#3-命名格式与可读性)。
- 调度、冻结、current/history或Web可见性变更使用工程设计的热/冷五时段矩阵；一个时间点或fixture不够。状态/JSON变更追踪类型到最终serializer和浏览器消费者，不加字典fallback或第二状态源。
- 并发/deadline分别核对排队、供应商尝试、校验、持久化、发布、取消和停止。Python检查种类由Python guide确定，具体门禁按 AGENTS.md 和工程设计选择。
- 运行诊断只通过 scripts/diagnose_runtime.py，实现放在所属 scripts/runtime_diagnostics/ 模块；不重建独立wrapper或在编排器复制探测。mock、HTTP 200、旧进程和源码阅读不能代替真实运行证据。

## 结束前核对

按 [delivery evidence 的交付核对](references/delivery-evidence.md#review-and-handoff) 查看实际 diff、检查输出与Git身份。发现漏项先补做；条件不可用时明确未验证的门，不把口头确认、测试fixture或先前批次结果当作本批通过。

修改 skill 时核对链接及锚点、命名的源码/测试路径、命令、API路由、配置和稳定身份。元数据validator只证明结构有效；需要核验执行行为时，显式运行 scripts.skill_evaluation，命令和限制见 scripts/README.md。普通文案调整不自动触发模型调用，不以少量合成案例承诺稳定成功率。

本 skill 由代理在仓库任务中读取，不是Web、行情或调度器运行hook。符合条件却漏加载时补齐流程，不新增产品触发逻辑。
