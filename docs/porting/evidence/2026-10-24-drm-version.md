# DRM_IOCTL_VERSION 对齐 vendor（P5b-15，2026-10-24）

## 结论

- PowerVR/firmware 后端的 `DRM_IOCTL_VERSION` 现在返回 vendor
  `pvrsrvkm` 的身份：**24 / 2 / 6643903**，name `pvr`，
  desc `Imagination Technologies PVR DRM`，date `20170530`
  （`include/pvrversion.h`、`services/server/env/linux/pvr_drm.c`）；
  其它后端（virtio、非 pvr firmware）保持原值；
- ktest `gpu_drm_version_matches_vendor` 通过；
- 板级确认 libdrm 收到 `24 2 6643903`；
- **但 UMD 仍然没有发出任何 PVR bridge ioctl**：
  `gbm_create_device` 返回非 NULL，`eglGetPlatformDisplay` 返回 NULL，
  整份串口 0 条 `ASTERINAS_POWERVR_BRIDGE`，`pvr_dri`/`libsrv_um`
  仍是 init 后立即 fini 并回退 `kms_swrast`；
- 因此 DRM version **不是**（唯一的）前置门槛；需要继续找 UMD 在
  CONNECT 之前退出的原因。

## 实现（commit `0dfdd94ab`）

`kernel/src/device/dri.rs`：

- 新增 `drm_version_fields(driver_name)`：
  - `pvr` → `(24, 2, 6_643_903, "20170530",
    "Imagination Technologies PVR DRM")`；
  - 其它 → `(0, 1, 0, DRIVER_DATE, DRIVER_DESC)`；
- `GetVersion` 处理改用该函数填 major/minor/patchlevel/date/desc；
- 新增 ktest `gpu_drm_version_matches_vendor`，断言 pvr 三元组为
  24/2/6643903、desc/date 为 vendor 字符串，并断言非 pvr 仍为 0.1.0。

vendor 依据：

```text
include/pvrversion.h:      PVRVERSION_MAJ 24U, MIN 2U, BUILD 6643903
services/server/env/linux/pvr_drm.c:79-81:
  PVR_DRM_DRIVER_NAME "pvr" (config_kernel.h:175)
  PVR_DRM_DRIVER_DESC "Imagination Technologies PVR DRM"
  PVR_DRM_DRIVER_DATE "20170530"
pvr_drm.c:557-562 把上述值写入 drm_driver。
```

## ktest

`timeout 180`，内部不打印：

| 测试 | 结果 |
|---|---|
| `gpu_drm_version_matches_vendor`（新增） | `1 passed; 0 failed; 360 filtered out` |
| `gpu_drm_sysfs_nodes` | `1 passed; 0 failed; 360 filtered out` |
| `gpu_drm_driver_name` | `1 passed; 0 failed; 360 filtered out` |

原始串口：`target/kt-p5b15-*.serial.log`（未跟踪）。

## 镜像与板级

| 项 | 值 |
|---|---|
| 内核 | `/home/debian/kernel-pvr-drmver.Image`，16,744,208 B，CRC32 `22b65ef6`，SHA-256 `7fb05c7950a642593918c427cab791c10aec6362c8d058b11136e8e8effbea46` |
| initramfs | 原 `stage1-pvr-batch.cpio`（CRC32 `c8f825a7`），未修改 |
| DTB | `megrez_dtb-465cb129333cc334.dtb`（CRC32 `40ed4c65`），未修改 |
| 串口 | `target/board-p5b15-20261024/board_p5b15.serial.log`、`board_p5b15b.serial.log` |

guest 直接 gbm 探针（`LD_LIBRARY_PATH=/opt/pvr/mesa22/lib:/opt/pvr/lib`、
`LIBGL_DRIVERS_PATH=/opt/pvr/mesa22/lib/dri`、
`MESA_LOADER_DRIVER_OVERRIDE=pvr`）：

```text
FD 3
VER 24 2 6643903
BEFORE_GBM
AFTER_GBM True
BEFORE_DISP
AFTER_DISP False
```

说明：

- `VER 24 2 6643903` 来自 libdrm 的 `drmGetVersion`，证明内核版本对齐
  已到达用户态；
- 探针里按 ioctl `struct drm_version` 布局打印的额外字段（曾被标为
  `LEN`）实际落在 libdrm 自己的 `drmVersion` 上——其 name/date/desc
  是**指针**（name 在 offset 16），所以那些数值是指针而不是长度；
  P5b-11 的同一探针已确认 `NAME b'pvr'`；
- `gbm_create_device` 成功，`eglGetPlatformDisplay` 仍为 NULL。

`LD_DEBUG=libs`（同一次运行）：

```text
calling init: /opt/pvr/mesa22/lib/dri/pvr_dri.so
calling init: /opt/pvr/lib/libpvr_dri_support.so
calling init: /opt/pvr/lib/libsrv_um.so
calling fini: /opt/pvr/lib/libpvr_dri_support.so
calling fini: /opt/pvr/lib/libsrv_um.so
calling fini: /opt/pvr/mesa22/lib/dri/kms_swrast_dri.so
calling fini: /opt/pvr/mesa22/lib/dri/pvr_dri.so
```

bridge marker：

```text
ASTERINAS_POWERVR_BRIDGE 出现次数：0
```

## 判定

- 版本字段对齐本身已生效，但 UMD 的退出点没有前移：仍然在
  `pvr_dri` 被接受之前（CONNECT 未发出）。
- 因此 P5b-14 的“版本是门槛”假设**未被证实**；至少它不是唯一门槛。

## 下一步候选（都不改内核模型，先做最小验证）

1. **改用 card0 节点**：当前探针用 `/dev/dri/renderD128`。vendor UMD
   可能在 `drmGetDevice2` 里要求 card0/主节点；把同一探针改为
   `/dev/dri/card0` 是最便宜的一次对照；
2. **`drmGetCap` 对照**：`pvr_dri.so` 导入 `drmGetCap`，我们只返回
   少数 capability；对比 vendor 设备期望的值；
3. **`drmGetDevice2`/`drmGetDevices2` 字段**：`pvr_dri.so` 与
   `libsrv_um.so` 都导入它们；检查我们的 sysfs/uevent 是否缺
   busid/subsystem/device 等字段；
4. **`libpvr_dri_support` 的 "Wrong version. Got: %d, Expected %d"**：
   它也可能指 `PVRDRIRegisterVersionedCallbacksV2` 的回调版本，而不是
   DRM version；下一步需要确认这条字符串的引用点（UMD 反汇编/调试输出）。

## 边界

- 未改 initramfs；ktest 均带 `timeout 180`；板级命令均带 `timeout`；
- 未执行 apt，未修改 RockOS 系统文件；
- EGL 未通过，不写成通过。

## P5b-16：四个假设的验证

本轮不改内核/initramfs，只做验证。板级复用
`kernel-pvr-drmver.Image`（CRC32 `22b65ef6`）与原 initramfs。

### H1：改用 `/dev/dri/card0` —— 排除

同一 gbm 探针把节点从 `renderD128` 换成 `card0`：

```text
FD 3
VER 24 2 6643903
BEFORE_GBM
AFTER_GBM True
BEFORE_DISP
AFTER_DISP False
```

`eglGetPlatformDisplay` 返回 NULL，`eglInitialize` 未执行（没有 display）；
整份串口 `ASTERINAS_POWERVR_BRIDGE` 仍为 **0** 条。card0 与
renderD128 无差别，H1 排除。

### H2：`drmGetCap` 对照 —— 差异成立，但不是直接门槛

RockOS（card0，`es_drm`）：

| cap | 值 |
|---|---|
| `DUMB_BUFFER`(1) | 1 |
| `DUMB_PREFERRED_DEPTH`(3) | 0 |
| `PRIME`(5) | 3 |
| `TIMESTAMP_MONOTONIC`(6) | 1 |
| `SYNCOBJ`(0x13) | 1 |
| `SYNCOBJ_TIMELINE`(0x14) | 0 |

Asterinas guest（card0 与 renderD128 相同）：

```text
DUMB 0 1 0
DEPTH 0 24 0
PRIME 0 3 0
TSMONO -1 0 22      # EINVAL
SYNCOBJ -1 0 22     # EINVAL
TIMELINE -1 0 22    # EINVAL
```

`pvr_dri.so` 的 `drmGetCap` 调用点（反汇编立即数）查询的是
**5 / 16 / 19 / 20**（PRIME、ADDFB2_MODIFIERS、SYNCOBJ、
SYNCOBJ_TIMELINE）；5 有值，16/19/20 在 Asterinas 上返回 EINVAL。

但 `pvr_dri.so` 对 cap 探测是**容错**的：

```text
6cd00a:  jalr  ... drmGetCap (cap=19)
6cd00e:  li    a5,0
6cd010:  bnez  a0, 6cd018     # 出错则保留 0
6cd012:  ld    a5,152(sp)
6cd014:  snez  a5,a5
6cd018:  sb    a5,770(s0)     # 存为 feature flag
（cap=20 同样）
```

即 EINVAL 被当作“能力不存在”，初始化继续。H2 的**差异成立**
（应补 16/19/20），但它不是 CONNECT 之前的直接失败点。

### H3：`uevent`/`busid`/`subsystem` —— 差异成立，机制未证实

Asterinas guest：

```text
DRIVER=pvr
DEVTYPE=platform
/sys/class/drm/card0/device/subsystem -> /sys/bus/platform
/sys/class/drm/card0/device/busid: 不存在
```

RockOS（对照）：

```text
DRIVER=es_drm
OF_NAME=display-subsystem
OF_FULLNAME=/soc/display-subsystem@0
OF_COMPATIBLE_0=eswin,display-subsystem
OF_COMPATIBLE_N=1
MODALIAS=of:Ndisplay-subsystemT(null)Ceswin,display-subsystem
```

差异确认：我们的 uevent 没有 `OF_COMPATIBLE_*`/`MODALIAS`。
但 UMD 侧没有找到按 compatible 过滤的证据：

- `libsrv_um.so` 的动态导入只有 `drmGetDevices2`，**没有**
  `drmGetVersion`；
- 整个 `/opt/pvr` 包里没有 `img,gpu` / `eswin,display` 字面字符串；
- 枚举调用是 `drmGetDevices2(flags=0, ...)`（反汇编
  `li a1,0; li a2,0; jal drmGetDevices2`）。

因此 `OF_*` 缺失是真实的平台设备字段差异，但“它导致 UMD 找不到设备”
尚无证据，暂列**未证实**。

### H4：回调版本 —— 基本排除

- `PVRDRIRegisterVersionedCallbacksV2` 由 `libpvr_dri_support.so`
  导出；`pvr_dri.so` 里只有字符串
  `libpvr_dri_support.so` 和 `PVRDRIRegisterVersionedCallbacksV2`
  （dlsym 路径），没有静态导入；
- 反汇编该函数：

```text
li   a4,3
bltu a4,a1, <wrong version 分支>   # 版本必须 <= 3
```

它是**回调表选择器**（索引 0..3），不是 DRM version 的比较；
调用方与被调方来自同一 vendor 包，版本应当一致；
`Wrong version. Got/Expected` 字符串的引用点本轮未解析，不能把它与
这条检查绑定。

### 判定汇总

| 假设 | 判定 |
|---|---|
| H1 card0 vs renderD128 | **排除**（结果完全一致） |
| H2 `drmGetCap` 缺失 | **差异成立**（16/19/20 返回 EINVAL），但反汇编显示被容错，不是直接门槛 |
| H3 uevent 缺 OF 字段 | **差异成立**，但 UMD 无 compatible 过滤证据，机制未证实 |
| H4 回调版本 | **基本排除**（<=3 的 table 选择器，同包构建） |

四条都不能单独解释“CONNECT 未发出”。下一轮需要找到 UMD 在
CONNECT 之前的真实退出点。

### 能推进 CONNECT 的最小改动（下轮实施）

1. **补 caps**：`ADDFB2_MODIFIERS`(0x10)、`SYNCOBJ`(0x13)、
   `SYNCOBJ_TIMELINE`(0x14)，以及 `TIMESTAMP_MONOTONIC`(6)
   （RockOS 上为 1；0x10 的 RockOS 值本轮未测到，先按 0 处理）；
   只对 pvr 后端生效；
2. **补平台 uevent 字段**：`OF_NAME`、`OF_FULLNAME`、
   `OF_COMPATIBLE_0=img,gpu`、`OF_COMPATIBLE_N=1`、`MODALIAS`
   （与 vendor `img,gpu` 平台设备一致）；
3. 若 1+2 后仍无 CONNECT：用 LD_PRELOAD 拦截
   `drmGetDevices2`/`open`/`ioctl`，或打开 DDK 调试输出，定位
   libsrv 在 CONNECT 前的最后一步；这是诊断，不改内核语义。

### 边界（P5b-16）

- 未改内核/initramfs；未执行 apt；未修改 RockOS 系统文件；
- 板级命令带 `timeout`；串口输出按轮次限量；
- 串口全量：`target/board-p5b16-20261024/board_p5b16.serial.log`
  （未跟踪）。
