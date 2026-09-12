# Tomorrow 训练 2 GiB 隔离与内存降峰

## User Request

用户反馈连续两次运行 `./run.sh train-tomorrow` 后，训练和桌面终端都在数分钟内被 kill；要求把 4 GiB
改为 2 GiB，并检查其它可优化的资源边界，改完提交。

## Current Evidence and Cause

- 2026-09-12 09:51:30 与 09:54:24 的系统日志均确认 `systemd-oomd` 因用户服务持续内存压力超过 50%
  而终止对应 VTE scope，分别连带终止 11 个和 4 个进程；不是 Python 受控异常。
- 公开 shell 入口此前把训练留在终端 VTE scope 内；4096 MiB 只是完成后检查的目标值，不是运行时硬限制，
  因而 oomd 选择该 scope 时会把终端内进程一起终止。
- 两次运行都没有创建新的样本 workspace，首个失效边界仍在 100 个、约 24 GB 月分片的完整校验阶段。
  顺序 SHA-256 与 SQLite 完整性检查没有主动释放文件页缓存，会放大桌面会话的持续回收压力。
- 后续模型阶段还存在 128 MiB SQLite 缓存、Ridge 全量截距设计矩阵和 LightGBM 未固定直方图内存池等
  可避免峰值。`Regression-Key: tomorrow-training-host-oom`。

## Added

- Linux `./run.sh train-tomorrow` 在可用的用户 systemd 中进入独立 `background.slice` scope，使用
  1792 MiB `MemoryHigh`、2048 MiB `MemoryMax`、2048 MiB `MemorySwapMax` 以及 CPU/IO 权重 20。
- 用户 systemd 不可用时输出明确警告，不把软件目标伪装成操作系统硬限制。

## Changed

- Tomorrow 训练峰值 RSS 合同由 4096 MiB 收紧为 2048 MiB，计算线程由 3 降为 2；进度提示改为“上限”。
- 月分片只读连接固定 8 MiB SQLite cache、文件临时区和禁用 mmap；顺序 hash 每 16 MiB 释放已读页，
  完整性检查结束再次释放该文件页缓存。
- 分片校验在单个大分片完成前持续显示当前分片 SHA-256 已读/总 MiB，并在 hash 后明确显示完整性与行数阶段，
  避免存活心跳连续重复无解释的 `0/100`。
- 一次性样本 SQLite cache 从 128 MiB 降为 32 MiB；LightGBM 固定列式直方图和 64 MiB 池；Ridge
  直接累计 7×7 正规方程，不再分配全量截距设计矩阵。

## Fixed

- 训练不再与桌面终端共享可被整体终止的同一 VTE scope；达到资源边界时故障被限制在训练 scope。
- 减少 24 GB 校验、样本缓存和模型拟合对主机内存回收压力的叠加。

## Removed

- 移除 4096 MiB/3 线程活动训练资源合同及其无硬隔离语义。

## Verification

- 定向回归通过，覆盖 Linux 独立 2 GiB scope、2 线程环境、分片内 MiB 进度、内存检查默认值、LightGBM
  确定性参数、分片页缓存释放和 SQLite cache 上限。
- 完整门禁 `make format-check`、`make lint`、`make type-check`、`make test`、`make package` 全部通过；
  最终进度增量另以定向 pytest、Ruff 和 mypy 复核通过。
- 本机真实用户 systemd scope 冒烟确认 `MemoryHigh`、`MemoryMax`、`MemorySwapMax`、CPUWeight 和 IOWeight
  参数组合可被接受；未启动真实训练、未写训练工件。

## Residual Risks

- 按用户要求不在本批自动重跑耗时的一至两小时、24 GB 全量训练；因此新路径的真实全程峰值和最终模型工件
  仍需下一次用户运行或显式内存门禁闭合。自动化测试不能替代该实证。
- 非 Linux 或没有用户 systemd 的 Linux 环境只能使用 2048 MiB 软件验收目标和低内存实现，不能提供
  Linux cgroup 硬隔离；入口会明确警告该差异。
- 用户已有未跟踪文件 `docs/v1v2.md` 不属于本批，保持原样且不进入提交。
