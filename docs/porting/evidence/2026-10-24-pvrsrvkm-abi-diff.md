# pvrsrvkm.ko ABI 静态对齐（P5b-13，2026-10-24）

## 结论

- 取证对象：RockOS 的
  `/lib/modules/6.6.87-win2030/kernel/drivers/gpu/drm/img/img-volcanic/pvrsrvkm.ko`
  （ELF64 little-endian REL，RISC-V，RVC，soft-float ABI，vermagic
  `6.6.87-win2030`），以及 `eswin-src-20261022` 中的同一份 DDK 源码
  （`img/img-volcanic`）；
- DRM ioctl 面：vendor 共 8 条（`0x40`–`0x47`），Asterinas 现有实现
  **8 条全部覆盖**，编号、方向、参数大小均一致；
- 桥命令：vendor 的 dispatch 表有 **140 个 group 槽位**
  （`g_BridgeDispatchTableStartOffsets` = 560 B = 140×4）；
  `g_BridgeDispatchTable` = 4056 B，条目 24 B（函数指针 8 + 锁指针 8 +
  in/out size 各 4），即**本构建实际分配 169 个 dispatch 条目**；
  生成源码中 30 个 group 共 254 处 `SetDispatchTableEntry` 注册，
  说明部分 group 在本构建被裁掉；
- Asterinas 只实现了 **SRVCORE(1) 的 4/17 条**；
- 已实现的 4 条 SRVCORE 命令，in/out 结构体大小与 vendor packed 结构
  完全一致；`PVRSRV_OK=0`、`PVRSRV_DEVICE_STATUS_OK=1` 一致；
- **发现一处字段值不一致**：CONNECT OUT 的 `ui8KernelArch`，vendor
  对 64 位内核写 **64**（`srvcore.c:788`），Asterinas 写 **1**；
- **返回码语义不一致**：Asterinas 对未实现的 bridge 命令返回 ioctl
  `-EOPNOTSUPP`；vendor 会进入 dispatch，按情况返回
  `PVRSRV_ERROR_BRIDGE_EINVAL`（未知 group / func 越界 / 大小不匹配）
  或 `PVRSRV_ERROR_ENOTTY`（group 未编译），再映射为 native errno；
- 本轮只产出清单，未改内核。

## 取证方法

```text
scp rockos:/lib/modules/6.6.87-win2030/.../pvrsrvkm.ko  ->  /tmp/pvrsrvkm-abi/
readelf -h / -sW, strings -a, modinfo（vermagic）
源码：/mnt/shared/xaj/vendor/eswin-src-20261022/eswin/drivers/gpu/drm/img/img-volcanic
```

未在 RockOS 上 insmod、未修改 RockOS；`.ko` 只读复制。

## 二进制事实

`readelf -h`：

```text
类别: ELF64      数据: 2 补码，小端序      类型: REL (可重定位文件)
系统架构: RISC-V  标志: 0x1, RVC, soft-float ABI
```

`modinfo`（在 RockOS 上读取）：

```text
alias:    platform:pvrsrvkm
alias:    of:N*T*Cimg,gpuC*
vermagic: 6.6.87-win2030 SMP mod_unload riscv
```

关键符号：

| 符号 | 大小 | 含义 |
|---|---|---|
| `pvr_drm_ioctls` | 192 B | 8 条 DRM ioctl 描述符（每条约 24 B） |
| `g_BridgeDispatchTableStartOffsets` | 560 B | 140 个 group 槽位（每槽 2×u16） |
| `g_BridgeDispatchTable` | 4056 B | 169 个 dispatch 条目（24 B/条） |
| `PVRSRV_BridgeDispatchKM` | 418 B | `PVRSRV_BRIDGE_PACKAGE` 分发入口 |

与 ioctl 相关的 GLOBAL 符号（`readelf -sW`）：

```text
PVRSRV_BridgeDispatchKM
pvr_sync_rename_ioctl, pvr_sync_force_sw_only_ioctl,
pvr_sw_sync_create_fence_ioctl, pvr_sw_sync_inc_ioctl,
pvr_export_fence_sync_create_fence_ioctl,
pvr_sync_ioctl_common_rename / _force_sw_only / _force_exp_only /
_create_export_fence / _sw_create_fence / _sw_inc,
pvr_sync_ioctl_init / pvr_sync_ioctl_deinit
```

`strings -a`：`PVRSRV_BRIDGE_*` 名字共 168 条；SRVCORE 的 17 条全部
在位（`CONNECT`、`DISCONNECT`、`ACQUIREGLOBALEVENTOBJECT`、
`RELEASEGLOBALEVENTOBJECT`、`EVENTOBJECTOPEN`、`EVENTOBJECTWAIT`、
`EVENTOBJECTCLOSE`、`DUMPDEBUGINFO`、`GETDEVCLOCKSPEED`、
`HWOPTIMEOUT`、`ALIGNMENTCHECK`、`GETDEVICESTATUS`、
`GETMULTICOREINFO`、`EVENTOBJECTWAITTIMEOUT`、`FINDPROCESSMEMSTATS`、
`ACQUIREINFOPAGE`、`RELEASEINFOPAGE`）。KCCB 命令是数值枚举，不以
字符串形式出现在 `.ko` 中。

## DRM ioctl 对照

vendor UAPI：`include/drm/pvr_drm.h`。

| 编号 | ioctl | 方向/大小 | Asterinas |
|---|---|---|---|
| 0x40 | `PVR_SRVKM_CMD` | `_IOWR`，`drm_pvr_srvkm_cmd` 32 B | ✅ `0xc0206440` |
| 0x41 | `PVR_SYNC_RENAME_CMD` | `_IOW`，`pvr_sync_rename_ioctl_data` 32 B | ✅ |
| 0x42 | `PVR_SYNC_FORCE_SW_ONLY_CMD` | `_IO`，0 B | ✅（stub） |
| 0x43 | `PVR_SW_SYNC_CREATE_FENCE_CMD` | `_IOWR`，`pvr_sw_sync_create_fence_data` 48 B | ✅ |
| 0x44 | `PVR_SW_SYNC_INC_CMD` | `_IOR`，`pvr_sw_timeline_advance_data` 8 B | ✅ |
| 0x45 | `PVR_SRVKM_INIT` | `_IOW`，`drm_pvr_srvkm_init_data` 4 B | ✅ `0x40046445` |
| 0x46 | `PVR_EXP_FENCE_SYNC_FORCE_CMD` | `_IOWR`，48 B | ✅ |
| 0x47 | `PVR_SYNC_CREATE_EXPORT_FENCE_CMD` | `_IOWR`，48 B | ✅ |

`drm_pvr_srvkm_cmd` 的字段（32 B）：

```text
u32 bridge_id; u32 bridge_func_id; u64 in_data_ptr; u64 out_data_ptr;
u32 in_data_size; u32 out_data_size;
```

与 Asterinas 的 `DrmPvrSrvkmCmd` 一致。

## 桥 group 规模

vendor `services/include/pvr_bridge.h` + `rgx_bridge.h`：group ID 从
0 到 139（`PVRSRV_BRIDGE_RGXRAY=139`），共 140 个槽位。生成源码中
有 30 个 group 带实际命令，共 254 处注册；`.ko` 实际只分配 169 个
dispatch 条目（4056 B ÷ 24 B），即本构建裁掉了部分 group。下表是
**源码侧每 group 的注册数**，不代表全部在本构建启用：

| ID | group | 命令数 | ID | group | 命令数 |
|---|---|---|---|---|---|
| 1 | SRVCORE | 17 | 15 | PVRTL | 8 |
| 2 | SYNC | 10 | 16 | RI | 9 |
| 5 | PDUMPCTRL | 5 | 19 | DEVICEMEMHISTORY | 5 |
| 6 | MM | 36 | 20 | HTBUFFER | 1 |
| 8 | CMM | 3 | 22 | MMEXTMEM | 1 |
| 9 | PDUMPMM | 9 | 23 | SYNCTRACKING | 2 |
| 10 | PDUMP | 4 | 24 | SYNCFALLBACK | 16 |
| 11 | DMABUF | 4 | 25 | DI | 5 |
| 12 | DC | 26 | 26 | DMA | 3 |
| 13 | CACHE | 3 | 129 | RGXCMP | 12 |
| 14 | SMM | 3 | 130 | RGXTA3D | 16 |
| 132 | RGXFWDBG | 16 | 133 | RGXPDUMP | 6 |
| 134 | RGXHWPERF | 10 | 135 | RGXREGCONFIG | 5 |
| 136 | RGXKICKSYNC | 4 | 137 | RGXTQ2 | 9 |
| 138 | RGXTIMERQUERY | 3 | 139 | RGXRAY | 3 |

Asterinas 目前只注册了 group 1（SRVCORE）。

## SRVCORE 命令对照

vendor 命令 ID（`common_srvcore_bridge.h`）：

| ID | 命令 | Asterinas | 备注 |
|---|---|---|---|
| 0 | CONNECT | ✅ | 见下方字段差异 |
| 1 | DISCONNECT | ✅ | 4/4，一致 |
| 2 | ACQUIREGLOBALEVENTOBJECT | ❌ | 缺 |
| 3 | RELEASEGLOBALEVENTOBJECT | ❌ | 缺 |
| 4 | EVENTOBJECTOPEN | ❌ | 缺 |
| 5 | EVENTOBJECTWAIT | ❌ | 缺 |
| 6 | EVENTOBJECTCLOSE | ❌ | 缺 |
| 7 | DUMPDEBUGINFO | ❌ | 缺 |
| 8 | GETDEVCLOCKSPEED | ✅ | 4/8，一致（值硬编码 800 MHz） |
| 9 | HWOPTIMEOUT | ❌ | 缺 |
| 10 | ALIGNMENTCHECK | ❌ | 缺 |
| 11 | GETDEVICESTATUS | ✅ | 4/8，一致 |
| 12 | GETMULTICOREINFO | ❌ | 缺 |
| 13 | EVENTOBJECTWAITTIMEOUT | ❌ | 缺 |
| 14 | FINDPROCESSMEMSTATS | ❌ | 缺 |
| 15 | ACQUIREINFOPAGE | ❌ | 缺 |
| 16 | RELEASEINFOPAGE | ❌ | 缺 |

已实现命令的结构体大小（vendor `__packed`）：

| 命令 | vendor in/out | Asterinas in/out | 结论 |
|---|---|---|---|
| CONNECT | 16 / 17 | 16 / 17 | 一致 |
| DISCONNECT | 4 / 4 | 4 / 4 | 一致 |
| GETDEVCLOCKSPEED | 4 / 8 | 4 / 8 | 一致 |
| GETDEVICESTATUS | 4 / 8 | 4 / 8 | 一致 |

CONNECT OUT 的字段顺序也一致：

```text
u64 ui64PackedBvnc; u32 eError; u32 ui32CapabilityFlags; u8 ui8KernelArch;
```

BVNC 打包位移（`rgx_compat_bvnc.h`）：B=48、V=32、N=16、C=0；
Asterinas 的 `0x001e_0003_0198_0065` 对应 30.3.408.101，与 Megrez 一致。

## 差异清单

按影响排序：

1. **CONNECT `ui8KernelArch` 字段值不一致（确定）**
   - vendor：`*pui8KernelArch = 64`（64 位内核）或 `32`
     （`services/server/common/srvcore.c:784-792`）；
   - Asterinas：`out[16] = 1`（`kernel/src/device/dri/powervr_bridge.rs`）；
   - 影响：UM 依据该字段选择指针宽度。当前值不符合 vendor 写入语义，
     应改为 64。
2. **未实现的 bridge 命令返回码语义不同（确定）**
   - Asterinas：任何不认识的 `(bridge_id, func_id)` 一律
     `-EOPNOTSUPP`；大小不匹配 `-EINVAL`；
   - vendor：未知 group / func 越界 / 大小不匹配 →
     `PVRSRV_ERROR_BRIDGE_EINVAL`；group 未编译 →
     `PVRSRV_ERROR_ENOTTY`（DummyBW）；然后经
     `OSPVRSRVToNativeError` 映射；
   - 影响：UM 看到的 errno 不同，可能影响其错误分支。
3. **CONNECT 未校验客户端版本/构建选项（确定）**
   - vendor 比较 `ui32ClientBuildOptions`（严格掩码）与
     `ui32ClientDDKVersion`，不匹配返回
     `PVRSRV_ERROR_BUILD_OPTIONS_MISMATCH` 等；
   - Asterinas 忽略输入，始终返回 `PVRSRV_OK`；
   - 影响：掩盖 UM/KM 版本不匹配，后续初始化可能在更深处失败。
4. **能力标志与时钟值是固定值（已知简化）**
   - `ui32CapabilityFlags`：vendor 按设备计算
     （cache-coherent 等，`srvcore.c:427-452`）；Asterinas 为 0；
   - `GETDEVCLOCKSPEED`：vendor 查询实际时钟；Asterinas 固定
     800 MHz。
5. **其余 13 条 SRVCORE 命令缺失（确定）**
   - 见上表；其中与 UM 初始化最可能相关的是
     `ACQUIREGLOBALEVENTOBJECT`、`EVENTOBJECTOPEN/WAIT/CLOSE`、
     `ACQUIREINFOPAGE/RELEASEINFOPAGE`、`GETMULTICOREINFO`。
6. **其余 29 个 group 全部缺失（确定）**
   - 其中 MM(6, 36 条)、SYNC(2, 10 条)、DMABUF(11, 4 条)、
     CACHE(13, 3 条)、RGXTA3D(130, 16 条) 是后续渲染/内存路径的
     主体；本轮不展开。

## KCCB 常量交叉确认

vendor `include/volcanic/rgx_fwif_km.h`：

```text
RGXFWIF_KCCB_CMD_MMUCACHE       = 102 | magic
RGXFWIF_KCCB_CMD_SLCFLUSHINVAL  = 104 | magic
RGXFWIF_KCCB_CMD_HEALTH_CHECK   = 111 | magic
RGXFWIF_KCCB_CMD_STATEFLAGS_CTRL= 206 | magic
```

与 Asterinas 的 B2a 修复一致（`SLCFLUSHINVAL=104`，不是 102）；
`RGX_CMD_MAGIC_DWORD_SHIFTED = 0x2abc << 16` 与现有实现一致。

## 下一步（下轮实施，本轮不做）

1. 把 CONNECT 的 `ui8KernelArch` 改为 64（一行）；
2. 按 UM 初始化顺序补 SRVCORE：先 event object 四件套 +
   info page 两件套 + `GETMULTICOREINFO`，再视日志补
   `DUMPDEBUGINFO`/`HWOPTIMEOUT`/`ALIGNMENTCHECK`；
3. 调整未实现命令的返回码语义（区分 EINVAL/ENOTTY/EOPNOTSUPP，
   或改为写回 bridge eError）；
4. 之后再按清单补 MM/SYNC/DMABUF/CACHE 组。

## 边界

- 只读复制 RockOS 的 `.ko`，未 insmod、未改 RockOS；
- 未改 Asterinas 内核/initramfs；本轮只产出清单与差异；
- 所有板级命令带 `timeout`。
