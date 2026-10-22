./run.sh sync market
sync: 正在检查运行环境。
sync company | ST资格 | 进度 5225/5225 股（100.00%） | 待处理 0 股 | 成功页 0 | 变更行 0 | 累计复用 0 | 累计 00:00:08 | 状态 完成
00:00:00 | qfq 更新失败             | sqlite_schema_unsupported
{"failure": null, "schema_version": "data_sync", "status": "pending", "tasks": [{"changed_files": [], "changed_rows": 0, "completed_codes": 0, "failure_reason": "sqlite_schema_unsupported", "latest_missing_codes": 0, "no_trade_data_codes": 0, "pending_codes": 0, "pending_reasons": [], "reason": null, "skipped_codes": 0, "status": "pending", "target_date": null, "task": "market"}]}



./run.sh sync history
00:00:11 | 行业快照       | 开始 |     17/    18 ( 94.44%) | 日期 2026-10-21
00:00:11 | 行业快照       | 完成 |     18/    18 (100.00%) | 日期 2026-10-21 | 缓存复用 | 缺行业      0 股
00:00:12 | 同步流程       | 失败 | 错误 history_resume_codes_changed | 诊断 RuntimeError:history_sync_runner.py:_resume:204 | 阶段耗时 00:00:00
{"failure": null, "schema_version": "data_sync", "status": "pending", "tasks": [{"history": {"active_snapshot_hash": null, "archive_root": "/home/cp/linux/trader/data/market/history", "automatic_training": false, "data_cutoff": null, "efficient_daily_source": "tencent", "label_cutoff": null, "matured_label_days_since_training": 0, "price_gaps": null, "reason": "history_resume_codes_changed", "schema_version": "history_maintenance_status", "selected_baseline_source": "tencent_with_baostock_reference", "state": "failed", "training_due": false, "training_due_reason": "data_incomplete", "training_heads": [], "unresolved_price_cells": 0}, "reason": null, "status": "pending", "task": "history"}]}


00:02:18 | sync history 股票进度 | 3582/4401（81.39%） | 股票 300390 天华新能 | 处理 Tencent下载 | 结果 待重试 | raw 2000/2000 | qfq 1969/2000 | 完整配对 1969/2000 | 缺日 31 | 网络   3587/  4401 | 落库   3582/  4401 | 待写      0 | 下载 00:00:08 | 落库耗时 00:00:01 | 回压 00:00:00 | 总计 00:01:54 | 原因 tencent_qfq_invalid_price
00:02:18 | sync history 股票进度 | 3583/4401（81.41%） | 股票 000708 中信特钢 | 处理 Tencent下载 | 结果 待重试 | raw 1994/2000 | qfq 1980/2000 | 完整配对 1980/2000 | 缺日 14 | 推定停牌 6 | 网络   3587/  4401 | 落库   3583/  4401 | 待写      0 | 下载 00:00:08 | 落库耗时 00:00:01 | 回压 00:00:00 | 总计 00:01:54 | 原因 tencent_qfq_invalid_price
00:02:18 | sync history 股票进度 | 3584/4401（81.44%） | 股票 300274 阳光电源 | 处理 Tencent下载 | 结果 待重试 | raw 2000/2000 | qfq 1983/2000 | 完整配对 1983/2000 | 缺日 17 | 网络   3587/  4401 | 落库   3584/  4401 | 待写      0 | 下载 00:00:08 | 落库耗时 00:00:01 | 回压 00:00:00 | 总计 00:01:54 | 原因 tencent_qfq_invalid_price

