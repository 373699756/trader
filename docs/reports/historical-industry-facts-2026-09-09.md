# 历史行业事实资格报告（2026-09-09）

## 结论

- 状态：`historical_data_insufficient`
- 合格股票：0
- `training_authority=false`
- `production_authority=false`
- 日线父 manifest hash：`a3bd8aa73b42b99c7a646d3a67a4fce8451401e67ac45596c20e17812d93acff`
- 行业数据集 hash：`a7b5691e1b1f648896260177ed08a2510182f3673a4e6228b726271c3e511d54`
- 最终报告 schema 复跑：`pending`（加入跨来源冲突字段后的复跑被中断，未生成输出文件）

本报告记录初次完整审计证据和当前 checkpoint；最终代码的聚合计数预计不变，但仍须复跑后确认新的报告内容
hash。本报告不表示行业永远不可取得，也不授权点时数据集、V3 训练、终端留出或生产切换。

## 来源结果

| 来源 | 样本/合格 | 股票分层 | 交易日覆盖 | 事实/冲突/时间穿越 | 失败原因 | 来源数据集 hash |
| --- | --- | --- | --- | --- | --- | --- |
| `baostock_archived_industry` `00.9.30` | 5453 / 0 | 主板 3392、创业板 1442、科创板 619；老股票 5127、新上市 84、退市 242 | 8,931,889 / 9,085,235，98.3121%；3521 只完整、1932 只缺 153,346 日 | 6326 / 0 / 0 | `industry_query_time_unavailable`、`industry_eligible_codes_below_300` | `21aedcb219929b387b5ee8237da367a075138b60787572a7739742bb79bc818f` |
| `tushare_index_member_all` `document_335` | 0 / 0 | 未采样 | 0 / 0 | 0 / 0 / 0 | `source_access_below_required_points`、`industry_sample_below_300`、`industry_fact_evidence_missing`、`industry_eligible_codes_below_300` | `3c9441894b6b54d7a42107447699e5a635c415589e062d82da3ed7c1d379c216` |

BaoStock 封存事实具有代码、行业、分类体系、生效边界、失效/下一变更边界、来源身份和逐事实 hash，但旧证据
没有独立查询时间，且 1932 只股票存在日期缺口，不能把请求日期或当前行业补成历史事实。Tushare 官方
[`index_member_all`](https://tushare.pro/document/2?doc_id=335) 合同提供分级指数、成分代码、纳入和剔除日期，
接口权限要求 2000 积分；当前配置为 120，本次没有发送无权限请求，也没有把字段合同冒充真实样本。

## 审计边界

审计逐一验证 92 个封存 SQLite 分片及父 manifest，按证券实际上市/退市区间与交易所开市日构造交易日期；
行业事实只有在 `effective_from <= trade_date < effective_to` 时才覆盖该日。多来源同分类可合并证据，不同分类
记为冲突并失败关闭。逐股票明细和 6326 个逐事实 hash 已由参数化脚本生成到仓库外报告，不提交运行数据或
股票清单。

```bash
.venv/bin/python scripts/audit_historical_industry_facts.py \
  --history-root "$PWD/data/history" \
  --required-sample-codes 300 \
  --tushare-access-points 120 \
  --include-details \
  --output /tmp/trader-historical-industry-report.json
```

命令以预期退出码 1 完成，耗时约 318.67 秒、峰值 RSS 约 120.0 MiB；执行期间无网络请求、无下载、无训练、
无 SQLite/manifest 写入。该结果来自跨来源整体冲突字段加入前的完整审计；最终 schema 的复跑在约 90 秒时
被中断，未产生部分报告。下次从同一命令重新运行并更新内容 hash 后，再执行完整门禁。只有未来来源同时满足
至少 300 只分层样本、全部必需字段、逐交易日覆盖、查询时间、
无冲突和无时间穿越时，对应股票才可进入行业合格集合。
