# Asterinas PowerVR PVR ioctl / bridge 骨架（Megrez P5a，2026-10-22）

## 结论

- P5a 主机侧完成：8 条 vendor PVR DRM ioctl 全部按 `pvr_drm.h` 编码并在
  render node 白名单中注册；
- `SRVKM_CMD` 的 bridge 分发骨架已实现：`{bridge_id, func_id, in/out
  ptr, in/out size}` → 精确大小校验 → 用户态 bounce copy → 有界函数表；
- 最小函数表只支持连接/版本协商类命令：
  `SRVCORE_CONNECT`、`DISCONNECT`、`GETDEVCLOCKSPEED`、
  `GETDEVICESTATUS`；`CONNECT` 返回 Megrez 的 packed BVNC
  `0x001e000301980065`（30.3.408.101）和 64-bit kernel arch；
- 其余 bridge 命令与 6 条 sync ioctl 明确返回 `EOPNOTSUPP` 并计入
  `unsupported`，不静默成功；
- 本轮不做真实固件调用（P5b 再连）；板级 marker 延后。

期望的板级 marker：

```text
ASTERINAS_POWERVR_BRIDGE status=ok init=N cmds=N unsupported=N
```

`init` 是 `SRVKM_INIT` 次数，`cmds` 是成功处理的 bridge 命令数，
`unsupported` 是未知 bridge/sync 命令次数。

## ioctl 编码

来源：`include/drm/pvr_drm.h`（vendor DDK 24.2），`DRM_COMMAND_BASE=0x40`。

| ioctl | nr | 方向/大小 | 命令号 |
|---|---|---|---|
| `PVR_SRVKM_CMD` | 0x40 | IOWR, 32 B | `0xc0206440` |
| `PVR_SYNC_RENAME_CMD` | 0x41 | IOW, 32 B | `0x40206441` |
| `PVR_SYNC_FORCE_SW_ONLY_CMD` | 0x42 | IO | `0x00006442` |
| `PVR_SW_SYNC_CREATE_FENCE_CMD` | 0x43 | IOWR, 48 B | `0xc0306443` |
| `PVR_SW_SYNC_INC_CMD` | 0x44 | IOR, 8 B | `0x80086444` |
| `PVR_SRVKM_INIT` | 0x45 | IOW, 4 B | `0x40046445` |
| `PVR_EXP_FENCE_SYNC_FORCE_CMD` | 0x46 | IOWR, 48 B | `0xc0306446` |
| `PVR_SYNC_CREATE_EXPORT_FENCE_CMD` | 0x47 | IOWR, 48 B | `0xc0306447` |

- `struct drm_pvr_srvkm_cmd` 为 32 B：
  `bridge_id u32, bridge_func_id u32, in_data_ptr u64, out_data_ptr u64,
  in_data_size u32, out_data_size u32`；
- `pvr_sync_rename_ioctl_data` 32 B（`name[32]`）；
- `pvr_sw_sync_create_fence_data` 48 B（`name[32]`、`fence i32`、
  `pad u32`、`sync_pt_idx u64`）；
- `pvr_sw_timeline_advance_data` 8 B（`sync_pt_idx u64`）；
- `drm_pvr_srvkm_init_data` 4 B（`init_module u32`）；
- 这 8 条都加入 `DRM_RENDER_ALLOW` 白名单。

## bridge 分发骨架

- `SRVKM_INIT`：接受 `init_module ∈ {1,2,3}`（services/sync/export-fence），
  其它值 `EINVAL`；只计数并打印 marker，不调用固件；
- `SRVKM_CMD`：
  1. 查有界函数表；未知 `(bridge_id, func_id)` → `EOPNOTSUPP`，
     `unsupported += 1`；
  2. 校验 `in_data_size`/`out_data_size` 与表项**精确相等**，否则
     `EINVAL`（不按“至少”接受，避免读越过用户缓冲区）；
  3. 按大小分配 bounce buffer，`copy_from_user` 输入，执行命令，
     `copy_to_user` 输出；
  4. 成功 `cmds += 1`，打印 marker；
- 支持的 SRVCORE 命令（组 ID `1`）：

  | func | 输入/输出字节 | 行为 |
  |---|---|---|
  | `CONNECT` 0 | 16 / 17 | 返回 BVNC、`PVRSRV_OK`、capabilities=0、kernel_arch=1 |
  | `DISCONNECT` 1 | 4 / 4 | 返回 `PVRSRV_OK` |
  | `GETDEVCLOCKSPEED` 8 | 4 / 8 | 返回 `800_000_000`（P2 实测值，占位） |
  | `GETDEVICESTATUS` 11 | 4 / 8 | 返回 `PVRSRV_DEVICE_STATUS_OK` |

- `CONNECT` 输出是 packed 布局：BVNC 在偏移 0，`PVRSRV_ERROR` 在 8，
  capability 在 12，`ui8KernelArch` 在 16（总 17 B）；
- 6 条 sync ioctl 已编码并分发，但统一返回 `EOPNOTSUPP` 并计入
  `unsupported`；真正的 sw_sync/export fence 在后续 sync bridge 实现。

## ktest

| 测试 | 覆盖 | 结果 |
|---|---|---|
| `gpu_pvr_bridge_rejects_bad_size` | `CONNECT` 的输入/输出尺寸不匹配均返回 `EINVAL`，且在触碰用户指针之前拒绝 | 已通过（`1 passed; 0 failed`） |
| `gpu_pvr_srvkm_init_ioctl` | 模块 1/2/3 接受、0/4 拒绝、init 计数 | 已通过（`1 passed; 0 failed`；此前失败是 ktest 环境无 console，marker 打印已改为 `cfg(not(ktest))`） |
| `gpu_pvr_bridge_unknown_cmd` | 未知 bridge 组返回 `EOPNOTSUPP`、unsupported 计数、sync 桩同样拒绝 | **已知问题/待复核**：曾在 QEMU 中挂起超过 2 分钟并被手动终止；console 打印已同样禁用，本地保存日志显示 `ok`，但按保守口径本轮不记为全绿，P5b 前做一次干净的单独复跑 |

此外还有 `pvr_connect_reports_the_megrez_bvnc` 检查 CONNECT 的 17 字节
packed 响应，未单独跑。

原始日志（未跟踪）：`target/kt-p5a-gpu_pvr_srvkm_init_ioctl.serial.log`、
`target/kt-p5a-gpu_pvr_bridge_rejects_bad_size.serial.log`、
`target/kt-p5a-gpu_pvr_bridge_unknown_cmd.serial.log`。

## 板级待补

1. 物理复位后启动带 `asterinas.powervr=1` 的镜像，用厂商
   `eglconfigs`/`pvrsrvctl` 打开 render node，确认
   `ASTERINAS_POWERVR_BRIDGE status=ok init=... cmds=...`；
2. 确认 `CONNECT` 的 BVNC 被 UMD 接受（若拒绝，核对 packed 布局和
   kernel arch 字段）；
3. 观察 `unsupported` 增长，按真实 UMD 调用顺序补齐下一条命令
   （预期从 `ACQUIREINFOPAGE`/`GETDEFAULT...` 开始）。

## 边界

- 不调用固件、不实现 PMR/render/sync bridge（P5b 起）；
- 不改变 P2/B1–B4 既有 marker 语义；
- `GETDEVCLOCKSPEED` 返回 P2 实测常量，接固件后应改为实时查询。
