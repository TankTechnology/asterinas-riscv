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

## P5b-11：LD_DEBUG 判别

### 运行条件

- 内核/initramfs 与 P5b-10 相同（`kernel-pvr-name.Image` CRC32
  `bc9f82ef`，`stage1-pvr-batch.cpio` CRC32 `c8f825a7`），bootargs 相同；
- guest 的 `/opt/pvr` 未改动；
- 全量串口：`target/board-p5b11-20261024/board_p5b11.serial.log`。

### 三次运行

| 运行 | 环境 | 结果 |
|---|---|---|
| A | 用户原文命令（无 `LD_LIBRARY_PATH`、无 `EGL_PLATFORM`） | `A_RC=124`（30 s 超时）；`LD_DEBUG` 显示加载 guest 的 `/lib/riscv64-linux-gnu/libEGL.so.1` 与 `libgallium-25.0.7-2+deb13u1.so`，即 guest Mesa 25，不是 `/opt/pvr` 的 Mesa 22 |
| B | P5b-10 基线（`LD_LIBRARY_PATH`，未设 `EGL_PLATFORM`） | `B_RC=1` 立即退出；`ldd eglconfigs` → `libEGL.so.1 => /opt/pvr/mesa22/lib/libEGL.so.1`；335 行 `LD_DEBUG` 中 `pvr_dri` 0 命中、`MESA-LOADER` 0 命中；`calling init` 末尾只有 `libEGL_mesa`/`libgbm`/`libglapi`/`libxcb-dri2` |
| C | B + `EGL_PLATFORM=gbm` | `C_RC=124`（挂到超时）；374 行 `LD_DEBUG` 中 `pvr_dri` 仍 0 命中；`calling init` 同上；stdout 为空 |

在 `eglconfigs` 的 EGL 路径上，Mesa 22 的 DRI loader 根本没有被走到：
A 选了 guest Mesa 25 栈；B/C 在 EGL 平台初始化阶段就失败或挂起。

### 直接调用链探针（ctypes）

guest 内 `/tmp/probe.py`（一次性文件，未入库）依次调用：

| 调用 | 结果 |
|---|---|
| `drmGetVersion(open("/dev/dri/renderD128"))` | name = `pvr` |
| `gbm_create_device(fd)` | 返回非 NULL |
| `eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, dev, NULL)` | 返回 NULL |

`gbm_create_device` 内部先报：

```text
MESA-LOADER: failed to open zink: /opt/pvr/mesa22/lib/dri/zink_dri.so: cannot open shared object file
failed to load driver: zink
```

### 打开/初始化判别

1. 直接 `ctypes.CDLL("/opt/pvr/mesa22/lib/dri/pvr_dri.so")` **成功**，
   `__driDriverGetExtensions_pvr` 可解析（`SYM True`）；
2. 进程环境里 `MESA_LOADER_DRIVER_OVERRIDE=pvr`、
   `LIBGL_DRIVERS_PATH=/opt/pvr/mesa22/lib/dri` 确实生效（probe 内打印
   `E1 pvr`、`E2 /opt/pvr/mesa22/lib/dri`）；
3. 在 `gbm_create_device` 内部用 `LD_DEBUG=libs` 看到：

```text
calling init: /opt/pvr/mesa22/lib/dri/pvr_dri.so
find library=libpvr_dri_support.so
calling init: /opt/pvr/lib/libpvr_dri_support.so
find library=libsrv_um.so
calling init: /opt/pvr/lib/libsrv_um.so
calling fini: /opt/pvr/lib/libpvr_dri_support.so
calling fini: /opt/pvr/lib/libsrv_um.so
```

4. `MESA_DEBUG=1 LIBGL_DEBUG=verbose`：

```text
LibGL: Loaded libpvr_dri_support.so
LibGL: Unloaded libpvr_dri_support.so
（随后 zink 回退）
```

即 **loader 打开了 `pvr_dri.so`，`libpvr_dri_support`/`libsrv_um` 也
初始化了，但随后立即 fini 并被卸载**；Mesa 判定该驱动不可用后回退
`zink`（不存在），`gbm_create_device` 仍返回一个 fallback device。
没有缺库的 dlopen 错误；`libusc` 未在该路径加载。

### 判定（对应用户 a/b）

- **不是 (a)**：`pvr_dri.so` 存在、override 与驱动路径生效、直接
  dlopen 与符号解析都成功；在 gbm 路径中它确实被打开；
- **接近 (b)，但失败点不是依赖 dlopen**：`pvr_dri` 与 `libsrv_um`
  初始化后立刻被 Mesa 拒绝并卸载，拒绝原因没有出现在
  `LIBGL_DEBUG=verbose` 输出里；
- 在 `eglconfigs` 的 EGL 路径中失败更早：Mesa 22 的 EGL 平台初始化
  在调用 DRI loader 之前就挂起（C）或失败（B），所以那两份日志里
  看不到 `pvr_dri`。

附加事实：`/dev/dri/card0` 与 `renderD128` 都能正常 open（card0
`os.open` 返回 fd 3，不阻塞），排除设备节点本身阻塞。

### 下一条最小改动（不重编优先）

1. 先确认 RockOS 自身 EGL 是否可用（同一板、同一 `pvr_dri`/Mesa 22、
   真实 `pvrsrvkm`）：正常启动 RockOS，跑 `eglinfo`/`eglconfigs`。
   若 RockOS 也失败，问题在厂商栈或硬件，不在 Asterinas 的 DRM 桥；
2. 若 RockOS 成功，再抓 `pvr_dri` 初始化失败的具体点：打开 DDK 侧调试
   输出，并对比 RockOS 上同一二进制的 `PVR_SRVKM_CMD` ioctl 序列；
   优先补 SRVCORE 之外被初始化路径实际使用的命令；
3. 已排除：缺 `libglapi`/`libLLVM-16`（dlopen 成功）、`ld.so.conf`
   路径问题（`LD_LIBRARY_PATH` 已生效）、把 `pvr_dri.so` 软链到
   guest Mesa 25 的 dri 目录（Mesa 25 DRI ABI 与 Mesa 22 驱动不匹配，
   不建议）；
4. 不再混用 guest Mesa 25 EGL 与 Mesa 22 `pvr_dri`（运行 A 已证明会
   选错栈）。

### 边界（P5b-11）

- 未改内核/initramfs，未跑 MMU ktests；
- EGL 仍未通过，不写成通过；串口输出按轮次 bounded，全量日志在
  `target/`（未跟踪）。
