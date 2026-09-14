# Version Profile-Owned Training Artifacts

## User Request

`data/train/v2` and `data/train/v3` 下的训练文件无法完整加入版本库；用户要求把两个目录中的全部固定工件加入并提交。

## Cause

根目录 `.gitignore` 以 `/data/train/**/*` 忽略训练运行产物，只对 V2 的空目录 `.gitkeep` 放行；V3 仅放行了目标目录中的四个文件。因此 V2 已生成的 `active-bundle.json`、`model.json`、`report.json` 和 `training-input.json` 会显示为 ignored，无法被普通 `git add` 纳入提交。

## Added

- 将 V2 Today、Tomorrow、D25 的 `active-bundle.json`、`model.json`、`report.json` 和
  `training-input.json` 纳入版本控制。

## Changed

- 为 V2 的 Today、Tomorrow、D25 目录放行四件固定可移植工件，并保留临时样本、staging、日志和运行归档的忽略规则。
- 将当前 V2 三头目录中的全部固定 JSON 工件纳入本次提交，并确认 V3 三头目录的同名固定工件继续由版本库管理；V2 工件保留独立 profile、feature manifest、model identity 和 hash。
- 更新打包布局契约，验证两档三头目录均包含 `.gitkeep`（历史占位）及四件非空固定工件。
- 更新 `docs/02_工程设计.md`、`docs/03_工程实施.md` 和 `docs/v1v2.md`，明确工件已提交但本批不执行运行 loader cutover。

## Fixed

- 修复 V2 固定训练工件因忽略规则而无法通过普通 `git add` 纳入交付的问题。

## Removed

- 未删除历史数据、训练工件或运行文件；临时样本、staging、日志和运行归档仍保持忽略。

## Verification

- `pytest -q tests/contract/test_packaging_layout.py tests/contract/test_entry_contract.py tests/unit/infra/scoring/test_profile_training_contracts.py` passed.
- 已逐项检查 V2/V3 18 个固定 JSON 文件存在且非空；V2 三头的 `profile_id` 为 `v2`，未使用 V3 文件冒充。
- `git diff --check`、最终 diff Review 和提交前暂存范围检查通过。

## Residual Risks

本批只版本控制训练工件，不改变运行时 loader、生产模型来源或自动 promotion。后续切换仍需单独完成档位感知 locator/codec、运行状态、性能门和完整启动验收；`docs/train.md` 为用户未跟踪文件，未纳入提交。

Regression-Key: profile-training-artifacts-versioning
