# Asterinas PowerVR fence / syncobj 最小闭环（Megrez P3/B4，2026-10-22）

## 结论

- B4 主机侧完成：新增设备级 fence 上下文（单调递增 seqno、signal、带超时
  的 wait），B2 的 no-op 作业在观察到 KCCB/IRQ 完成后 signal 自己的 fence
  并立即 wait，串起 **作业 → fence → wait** 链；
- 新增 DRM syncobj 最小面：`CREATE / DESTROY / WAIT / QUERY`，支持
  `CREATE_SIGNALED`、`WAIT_ALL` 和绝对 `CLOCK_MONOTONIC` 超时；timeline
  语义暂时是 0/1 两点的桩；
- 3 条 QEMU ktest 通过：
  `gpu_fence_signal_and_wait`、`gpu_syncobj_create_wait`、
  `gpu_fence_wait_all_requires_every_fence`（各 `1 passed; 0 failed;
  349 filtered out`）；
- **板级 marker 已定义但延后**：板子今晚在 U-Boot/autoboot 与串口 USB
  扫描之间反复不稳定，板级批量统一等物理复位后补；
- 不实现 vendor `PVR_SRVKM` 专有 sync bridge；B1–B3 既有 marker 语义不变。

期望的板级 marker（仅在 `asterinas.powervr_job_noop=1` 时出现）：

```text
ASTERINAS_POWERVR_FENCE status=ok signaled=1 waited=1 ctx=1
```

`signaled` 是上下文累计 signal 次数，`waited` 是成功返回的 wait 次数，
`ctx` 是上下文累计分配的 fence 数（当前设备只有一个 fence 上下文）。

## 实现

### fence 核心（`dri/fence.rs`）

- `FenceContext`：`next_seqno: AtomicU64`、`signaled/waited` 计数、
  一个 `WaitQueue`；
- `new_fence()` 分配 seqno 递增的 `FenceState`（初始 unsignalled）；
- `signal(fence)`：`AtomicBool::swap` 保证只记一次；随后计数并
  `wake_all()`，所有等待该上下文的线程被唤醒后重新检查条件；
- `wait_any(fences, all, timeout)`：
  - 先做无锁条件检查，已满足则立即返回；
  - `timeout = Some(0)` 直接返回 `ETIME`，不创建定时器；
  - 有限超时走 `WaitQueue::wait_until_or_timeout`，无限等待走
    `wait_until`；
  - 返回第一个满足的 fence 下标，供 syncobj 的 `first_signaled` 使用。

### syncobj（`dri.rs`）

| ioctl | 编号 | 参数 | 支持范围 |
|---|---|---|---|
| `SYNCOBJ_CREATE` | `0xc00864bf` | `DrmSyncobjCreate` 8 B | `CREATE_SIGNALED` |
| `SYNCOBJ_DESTROY` | `0xc00864c0` | `DrmSyncobjDestroy` 8 B | 未知 handle → `ENOENT` |
| `SYNCOBJ_WAIT` | `0xc02064c3` | `DrmSyncobjWait` 32 B | `WAIT_ALL`；`handles` 为 u32 数组 |
| `SYNCOBJ_QUERY` | `0xc01864cb` | `DrmSyncobjTimelineArray` 24 B | 每点返回 0/1，`LAST_SUBMITTED` 可接受 |

- handle 是 per-file 的（`SyncObjSpace` 在 `DriInner` 内），fence 状态是
  设备级 `Arc<FenceState>`；
- `WAIT` 的 `timeout_nsec` 按 uapi 解释为绝对单调时钟纳秒：负值与
  `0x7fff_ffff_ffff_ffff` 表示无限等待，已过期的 deadline 变成 0 超时；
- `WAIT_FOR_SUBMIT` / `WAIT_AVAILABLE` 等 timeline 专用 flag 明确返回
  `EOPNOTSUPP`，不静默降级；
- `QUERY` 读取 `handles` 数组后把每个 syncobj 的 timeline point（0 或 1）
  写入 `points` 数组；
- 这 4 个命令加入 `DRM_RENDER_ALLOW` allowlist，render node 可直接使用。

### 作业完成 signal

`powervr_probe/power.rs` 的 B2 作业：

1. 提交前 `fence::context().new_fence()`；
2. 保持既有 KCCB 完成判定（read offset + `CMD_EXECUTED` + IRQ 计数）；
3. 完成后 `signal(&job_fence)`，再以 1 ms 超时 `wait_any`；
4. 打印 `ASTERINAS_POWERVR_FENCE`，`status=timeout` 表示 signal 后等待
   仍未返回（不应发生；出现即说明 wake/条件检查有缺陷）。

用户态等待面目前是 **DRM fd 上的 `SYNCOBJ_WAIT` ioctl**（含超时）。
把 fence 导出成匿名 fd（`SYNCOBJ_HANDLE_TO_FD` / poll 等待）属于 vendor
sync bridge，B5 之后再做；本轮不宣称该 fd 面已实现。

## ktest

| 测试 | 覆盖 | 结果 |
|---|---|---|
| `gpu_fence_signal_and_wait` | 未 signal 的零超时 miss（`ETIME`）、signal 后立即 wait、幂等 signal、第二个 fence 不受影响 | `1 passed; 0 failed; 349 filtered out` |
| `gpu_syncobj_create_wait` | 创建 signaled/pending 两个 handle、signaled 立即 wait、pending 零超时 `ETIME`、destroy 后 `ENOENT` | `1 passed; 0 failed; 349 filtered out` |
| `gpu_fence_wait_all_requires_every_fence` | `WAIT_ALL`：只 signal 一个时 miss，两个都 signal 后返回 | `1 passed; 0 failed; 349 filtered out` |

原始日志（未跟踪）：`target/kt-b4-gpu_fence_signal_and_wait.serial.log`、
`target/kt-b4-gpu_syncobj_create_wait.serial.log`、
`target/kt-b4-gpu_fence_wait_all.serial.log`。

## 板级待补

1. 复位稳定后一次启动带
   `asterinas.powervr=1 asterinas.powervr_job_noop=1`
   的镜像，确认 `ASTERINAS_POWERVR_FENCE status=ok` 与
   `wake=irq` 同时出现；
2. 同批补 B1 `KCCB_PIPELINE`、B2 `JOB`、B3 `DRI_NODE`；
3. 若 fence wait 在板级超时，先检查 IRQ 是否真的唤醒（`wake=` 字段），
   再检查 `WaitQueue` 的唤醒条件，而不是放宽超时。

## 边界与下一步

- 本轮不实现 dma-fence fd 导出、PVR 专有 sync ioctl、timeline 多点语义；
- **B5**：vendor `PVR_SRVKM` bridge 的最小命令集、sync bridge、GLES
  后端路线；
- 板级批量证据汇总写到 `2026-10-22-asterinas-powervr-board-batch.md`。
