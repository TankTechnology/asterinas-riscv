# PowerVR DRM 驱动名改为 `pvr`（P5b-10，2026-10-24）

## 结论

- PowerVR/firmware 后端的 DRM driver name 已从 `simpledrm` 改为
  **`pvr`**；virtio 后端仍为 `virtio_gpu`，其它 firmware 场景仍为
  `simpledrm`；
- sysfs 补出了 `/sys/class/drm/card0/device` →
  `/sys/devices/platform/pvr`，以及
  `/sys/devices/platform/pvr/driver` →
  `/sys/bus/platform/drivers/pvr`；
- 板级确认：

```text
cat /sys/class/drm/card0/device/uevent
DRIVER=pvr
DEVTYPE=platform

ls -l /sys/class/drm/card0/device/driver
... -> ../../../bus/platform/drivers/pvr
```

- EGL 仍未通过：`ASTERINAS_POWERVR_EGL status=failed platform=gbm
  driver=pvr`；驱动名这一层已与 RockOS 对齐，下一层阻塞在
  pvr_dri/libsrv（或 EGL display）初始化。

## 实现

### 驱动名（`kernel/src/device/dri.rs`）

- 新增 `POWERVR_DRIVER_NAME = "pvr"`；
- `DisplaySource::driver_name()`：
  - virtio → `virtio_gpu`；
  - firmware 且 `is_powervr_platform()` → `pvr`；
  - 其它 firmware → `simpledrm`；
- `is_powervr_platform()` 在 riscv64 + `powervr_probe` 构建下检查
  device tree 是否有 `img,gpu`；非该构建恒为 false，不影响 QEMU
  virtio 与其它机器。

### sysfs（`kernel/src/device/sysfs.rs`）

- firmware DRM minor 节点新增 `device` 软链：
  `../../../devices/platform/pvr`；
- 新增 `/sys/devices/platform/pvr`：
  - `uevent`：`DRIVER=pvr`、`DEVTYPE=platform`；
  - `driver` → `../../../bus/platform/drivers/pvr`；
  - `subsystem` → `../../../bus/platform`；
- `/sys/bus/platform/drivers/pvr` 在
  `is_powervr_platform()` 为真时注册；
- 仅新增名字/链接，未改任何 DRM ioctl 语义，B1–B5/P4/P5a 的 marker
  与行为保持。

## ktest

| 测试 | 覆盖 | 结果 |
|---|---|---|
| `gpu_drm_sysfs_nodes` | `card0`/`renderD128` 的 `dev`、`uevent`、`device` 软链；`/sys/dev/char` 软链 | `1 passed; 0 failed; 358 filtered out` |
| `gpu_drm_driver_name` | `/sys/devices/platform/pvr/driver` 的 basename 为 `pvr` | `1 passed; 0 failed; 358 filtered out` |

原始日志（未跟踪）：`target/kt-gpu_drm_sysfs_nodes.serial.log`、
`target/kt-gpu_drm_driver_name.serial.log`。ktest 内无打印。

## 板级

| 项 | 值 |
|---|---|
| 内核 | `/tmp/tftp/kernel-pvr-name.Image`，16,742,560 B，CRC32 `bc9f82ef`，SHA-256 `2d8037bb5bbbf9aa2778d776143cd5d7ead9b6e87b71ee63f828a4c0fb04acc6` |
| initramfs | 原 `stage1-pvr-batch.cpio`（CRC32 `c8f825a7`），未修改 |
| guest 组件 | `/opt/pvr`（Mesa22 全套 + vendor DDK，沿用 P5b-5/9） |
| 串口 | `target/board-pvr-name-20261024/board_pvrname.serial.log` |

实测：

```text
DRIVER=pvr
/sys/class/drm/card0/device/driver -> ../../../bus/platform/drivers/pvr
```

EGL（gbm，60 s timeout）仍失败：

```text
ASTERINAS_POWERVR_EGL status=failed platform=gbm driver=pvr
```

本轮串口没有稳定抓到 `/tmp/egl_name.out` 的内容（前台 Firefox/Xorg
日志竞争），因此不把具体错误行写成结论；失败发生在驱动名已对齐之后。

## 下一步

1. 在 `driver=pvr` 已就位的前提下重跑
   `LIBGL_DEBUG=verbose`/`LD_DEBUG=libs`，确认 Mesa 是否终于打开
   `/opt/pvr/mesa22/lib/dri/pvr_dri.so`；
2. 若 `pvr_dri` 已加载但仍失败，抓 pvr_dri/libsrv 的第一条错误，
   进入 `PVR_SRVKM` bridge/初始化时序；
3. 若仍未打开驱动，检查 Mesa 22 的 DRI loader 是否按
   `drmGetVersion.name` 匹配（此时名字已是 `pvr`）以及
   glvnd vendor JSON 的实际加载路径。

## 边界

- 只改 driver name 与 sysfs 链接；不改 ioctl、initramfs 或既有 marker；
- EGL 未通过，不写成通过。
