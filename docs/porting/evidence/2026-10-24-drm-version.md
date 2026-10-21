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
