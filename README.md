# A 股荐股与策略验证看板

本项目是一个本地 Flask 看板，用公开行情数据生成 A 股推荐候选，并把推荐结果保存到验证库里做持续复盘。

结果只用于研究，不构成投资建议，也不保证盈利。

## 当前保留功能

- 三类荐股：今天推荐、明天推荐、2-5 天推荐。
- 每类最多展示 18 支；如果没有满足条件的股票，可以空推荐。
- 策略验证：按日期查看历史保存批次、样本表现、股票明细。
- 自动保存：交易日 14:30 后按配置间隔保存当天三类推荐，15:00 后使用收盘价作为锚点。
- DeepSeek：参与三类推荐的候选复核、风险降权、剔除理由、每日复盘和影子调参建议。
- 数据库：当前只保留三类策略的验证数据；启动时会清理无关策略验证记录。

## 三类荐股策略

| 页面 | 策略名 | 目标周期 | 用途 |
|---|---|---:|---|
| 今天推荐 | `short_term` | 盘中到次日 | 找盘中强势但不过热的短线候选 |
| 明天推荐 | `tomorrow_picks` | 次日 | 15:00 后筛次日可能延续且仍有买入安全的股票 |
| 2-5 天推荐 | `swing_picks` | 2-5 个交易日 | 找短周期趋势延续、温和放量、不过热的候选 |

完整策略和验证口径见 [`docs/strategies.md`](docs/strategies.md)。

## DeepSeek 如何参与

DeepSeek 不直接替代本地策略，也不直接改权重。当前流程是：

1. 本地策略先根据行情、量价、趋势、风险过滤生成候选。
2. DeepSeek 只复核这批候选，输出动作、风险、理由和二次排序分。
3. 命中明显风险的候选会被降权或剔除，例如 `veto`、高风险惩罚、`avoid` 或 DeepSeek 分过低。
4. 策略验证页每天 15:00 后生成一次 DeepSeek 复盘；也可以手动生成。
5. 复盘只保存为“影子调参建议”，不会自动应用到正式策略。

DeepSeek 不可用时，系统回退到本地策略排序。

DeepSeek 接口约定（兼容 OpenAI/Anthropic 风格）：

- `DEEPSEEK_BASE_URL`：默认 `https://api.deepseek.com/v1`（传 `https://api.deepseek.com` 也可自动补全）。
- `DEEPSEEK_MODEL`：建议 `deepseek-v4-flash`。
- `DEEPSEEK_PRO_MODEL`：建议 `deepseek-v4-pro`。
- 仅使用 DeepSeek v4 系列（`deepseek-v4-flash`、`deepseek-v4-pro`），不保留旧模型兼容映射。

## 策略验证

验证库只保存三类策略：

- `short_term`
- `tomorrow_picks`
- `swing_picks`

保存逻辑：

- 14:30 后自动按配置间隔保存今天、明天、2-5 天三类推荐。
- 同一天同策略只保留最后一次批次；0 支推荐也会保存为空批次。
- 15:00 后运行时，必须拿到收盘锚点价才保存为真实回溯锚点。
- 每次自动保存后会备份验证库。

验证逻辑：

- 今日/明天策略主要看次日方向和次日收益。
- 2-5 天策略看短周期持有表现。
- 页面里的胜率、涨跌个数、平均收益都来自保存样本和回填行情。
- 样本不足、未回填或空批次时，不把胜率当成可靠结论。

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

- [`docs/strategies.md`](docs/strategies.md)：三类荐股策略、DeepSeek 结合方式、验证口径。
- [`docs/strategy_optimization_plan.md`](docs/strategy_optimization_plan.md)：后续优化计划。
- [`docs/user_requirements_strategy_validation.md`](docs/user_requirements_strategy_validation.md)：策略验证当前需求口径。
