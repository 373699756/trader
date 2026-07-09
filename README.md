# A 股荐股与策略验证看板

本项目是一个本地 Flask 看板，用公开行情数据生成 A 股推荐候选，并把推荐结果保存到验证库里做持续复盘。

结果只用于研究，不构成投资建议，也不保证盈利。

## 当前功能

- 三类荐股：今天推荐、明天推荐、2-5 天推荐
- 策略验证：历史批次、样本表现、股票明细、DeepSeek 复盘
- 个股预测：输入股票代码，返回本地预测和 DeepSeek 优化建议
- 自动保存与回填：交易日 14:30 后保存，15:00 后使用收盘锚点
- DeepSeek：候选复核、风险降权、验证复盘、影子调参建议

## 启动

```bash
chmod +x run.sh
./run.sh
```

默认地址：

```text
http://127.0.0.1:5000
```

常用环境变量：

```bash
PORT=5050 ./run.sh
ENABLE_HISTORY_FACTORS=1 ./run.sh
VALIDATION_AUTO_SNAPSHOT_TIME=15:00 ./run.sh
VALIDATION_AUTO_UPDATE_START_TIME=14:30 ./run.sh
VALIDATION_AUTO_UPDATE_INTERVAL_SECONDS=600 ./run.sh
ENABLE_DEEPSEEK_RUNTIME=1 ./run.sh
```

## 常用接口（A股口径）

- `GET /api/recommendations?top_n=18&market=all`
- `GET /api/tomorrow-picks?top_n=18&market=all`
- `GET /api/swing-picks?top_n=18&market=all`
- `GET /api/stock-prediction/<code>`
  - 其中 `market=all` 表示 A 股主板+创业板+科创板（对应沪深/创业/科创）。
- `GET /api/strategy-validation?strategy=tomorrow_picks`
- `GET /api/strategy-validation/tuning?strategy=tomorrow_picks`
- `POST /api/strategy-validation/tuning?strategy=tomorrow_picks`

## 数据与备份

- 验证数据库：`.runtime/strategy_validation.sqlite3`
- 自动备份目录：`.runtime/backups`
- 备份列表：`.venv/bin/python -m stock_analyzer.daily_job --list-validation-backups`
- 还原备份：`.venv/bin/python -m stock_analyzer.daily_job --restore-validation <backup-file>`

## 文档

- [`docs/strategy_and_prediction.md`](docs/strategy_and_prediction.md)：三类荐股策略、DeepSeek 结合方式、个股预测与优化建议。
- [`docs/software_design.md`](docs/software_design.md)：软件整体结构、页面设计、接口、异步刷新、验证保存和运行方式。
