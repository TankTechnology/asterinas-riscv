# RockOS EGL 对照实验（P5b-12，2026-10-24）

## 结论

- 按 P5b-11 的判据做对照：**RockOS 本机也没有可用的 pvr EGL** —
  X11 GLX 无 visual，X11 EGL 落到 `swrast`，GBM EGL 落到
  `kms_swrast`/softpipe；
- 强制 `MESA_LOADER_DRIVER_OVERRIDE=pvr` 时，RockOS 上出现与
  Asterinas **完全同构**的“加载后立即卸载”：
  `pvr_dri.so` → `libpvr_dri_support` → `libsrv_um` → `libusc` 依次
  `calling init`，随后全部 `calling fini`；
- 根因在 RockOS 镜像自身：GPU 设备树节点存在
  （`/soc/gpu@51400000`，`img,gpu`，status `okay`），但
  `pvrsrvkm.ko` 的 vermagic 是 `6.6.87-win2030`，运行内核是
  `6.6.87`，`insmod` 直接 `Invalid module format`；系统里没有任何
  驱动绑定该 GPU 节点，只有显示子系统 `es_drm`；
- 因此 RockOS 不能作为“pvr 可用”的成功参照，**问题在厂商栈/驱动
  本身**；P5b 需要回到 vendor UMD/srvkm 侧，而不是继续在 Asterinas
  的 bridge 上猜补丁；
- 本轮没有可抓的成功 ioctl 序列（RockOS 无成功样本，且 RockOS 未
  安装 `strace`），ioctl 对比不适用。

## RockOS 环境事实

板子经 Asterinas `reboot_after` 自动重启回 RockOS，无需 U-Boot 菜单
干预。SSH `debian@10.100.19.200`，内核：

```text
Linux rockos-eswin 6.6.87 #1 SMP Mon Sep  1 21:20:09 CST 2025 riscv64 GNU/Linux
```

| 项 | 值 |
|---|---|
| `/dev/dri` | 只有 `card0`（226:0），**无 renderD128** |
| `card0` 驱动 | `DRIVER=es_drm`（`eswin,display-subsystem`） |
| dmesg | `[drm] Initialized es_drm 1.0.0 20191101 ... on minor 0`、`fb0: es_drmdrmfb` |
| GPU DT | `/soc/gpu@51400000`，compatible `img,gpu`，status `okay` |
| GPU 平台驱动 | 无（`/sys/bus/platform/drivers` 中无 pvr/gpu 驱动） |
| `pvrsrvkm.ko` | `/lib/modules/6.6.87-win2030/kernel/drivers/gpu/drm/img/img-volcanic/pvrsrvkm.ko`，alias `of:N*T*Cimg,gpu` |
| 模块树 | 只有 `6.6.87-win2030` 与 `asterinas-kvm` |
| `pvr_dri.so` | `/usr/lib/riscv64-linux-gnu/dri/pvr_dri.so`，SHA-256 `c49db9691b40f05a44d05e79b8a574c07a523be06516af8e291afe4aa2990dbb`（与 `/opt/pvr` 注入版本完全一致） |

`modprobe` / `insmod` 结果：

```text
modprobe: FATAL: Module pvrsrvkm not found in directory /lib/modules/6.6.87
pvrsrvkm: version magic '6.6.87-win2030 SMP mod_unload riscv' should be '6.6.87 SMP mod_unload riscv'
insmod: ERROR: could not insert module ...: Invalid module format
```

extlinux 默认项是 `RockOS GNU/Linux 6.6.87-win2030`，但运行内核
`uname -r` 为 `6.6.87`，镜像自带的模块树与运行内核不匹配。

## eglinfo（RockOS，默认环境）

前 35 行（GBM 平台）：

```text
EGL client extensions string:
    EGL_EXT_client_extensions, EGL_EXT_device_base,
    EGL_EXT_device_enumeration, EGL_EXT_device_query, EGL_EXT_platform_base,
    EGL_EXT_platform_device, EGL_EXT_platform_wayland, EGL_EXT_platform_x11,
    EGL_EXT_platform_xcb, EGL_KHR_client_get_all_proc_addresses,
    EGL_KHR_debug, EGL_KHR_platform_gbm, EGL_KHR_platform_wayland,
    EGL_KHR_platform_x11, EGL_MESA_platform_gbm,
    EGL_MESA_platform_surfaceless

GBM platform:
EGL API version: 1.4
EGL vendor string: Mesa Project
EGL version string: 1.4
EGL client APIs: OpenGL OpenGL_ES
EGL driver name: kms_swrast
EGL extensions string:
    EGL_ANDROID_blob_cache, EGL_EXT_buffer_age, EGL_EXT_image_dma_buf_import,
    EGL_EXT_image_dma_buf_import_modifiers, EGL_EXT_image_gl_colorspace,
    EGL_EXT_pixel_format_float, EGL_EXT_yuv_surface, EGL_KHR_cl_event2,
    EGL_KHR_config_attribs, EGL_KHR_context_flush_control,
    EGL_KHR_create_context, EGL_KHR_create_context_no_error,
    EGL_KHR_fence_sync, EGL_KHR_get_all_proc_addresses, EGL_KHR_gl_colorspace,
    EGL_KHR_gl_renderbuffer_image, EGL_KHR_gl_texture_2D_image,
    EGL_KHR_gl_texture_3D_image, EGL_KHR_gl_texture_cubemap_image,
    EGL_KHR_image, EGL_KHR_image_base, EGL_KHR_image_pixmap,
    EGL_KHR_no_config_context, EGL_KHR_reusable_sync,
    EGL_KHR_surfaceless_context, EGL_KHR_wait_sync,
    EGL_MESA_configless_context, EGL_MESA_drm_image,
    EGL_MESA_image_dma_buf_export, EGL_MESA_query_driver,
    EGL_WL_bind_wayland_display
OpenGL core profile vendor: Mesa/X.org
OpenGL core profile renderer: softpipe
OpenGL core profile version: 3.3 (Core Profile) Mesa 22.3.5
```

即：默认 GBM 选择的是 `kms_swrast`，不是 `pvr_dri`。

## 强制 pvr 的对照（关键）

在 RockOS 上执行：

```sh
MESA_LOADER_DRIVER_OVERRIDE=pvr LIBGL_DEBUG=verbose timeout 30 eglinfo
```

输出（只保留相关行）：

```text
LibGL: Loaded libpvr_dri_support.so
LibGL: Unloaded libpvr_dri_support.so
EGL driver name: kms_swrast
OpenGL core profile renderer: softpipe
```

`LD_DEBUG=libs` 的完整加载/卸载链：

```text
calling init: /usr/lib/riscv64-linux-gnu/dri/pvr_dri.so
find library=libpvr_dri_support.so [0]; searching
  trying file=/lib/libpvr_dri_support.so
calling init: /lib/libpvr_dri_support.so
find library=libsrv_um.so [0]; searching
  trying file=/lib/libsrv_um.so
calling init: /lib/libsrv_um.so
find library=libusc.so [0]; searching
  trying file=/lib/libusc.so
calling init: /lib/libusc.so
calling fini: /lib/libusc.so [0]
calling fini: /lib/libpvr_dri_support.so [0]
calling fini: /lib/libsrv_um.so [0]
calling fini: /usr/lib/riscv64-linux-gnu/dri/pvr_dri.so [0]
```

最终 EGL 仍是 `kms_swrast`，OpenGL 仍是 `softpipe`，Mesa 版本 22.3.5。
这与 P5b-11 在 Asterinas 上看到的“`pvr_dri` + `libsrv_um` 初始化后
立即 `fini`”签名一致（Asterinas 侧未走到 `libusc`）。

## Xorg / X11 侧

- Xorg 进程：`/usr/lib/xorg/Xorg :0 ... -auth /var/run/lightdm/root/:0`
  （LightDM）；
- `glxinfo -B`（root + LightDM Xauthority）：
  `Error: couldn't find RGB GLX visual or fbconfig`；
- `eglinfo -p x11`：`libEGL warning: DRI2: failed to authenticate`，
  随后 `EGL driver name: swrast`、renderer `softpipe`；
- `/var/log/Xorg.0.log`：

```text
(II) LoadModule: "modesetting"
(II) Loading /usr/lib/xorg/modules/drivers/modesetting_drv.so
(II) Loading sub module "glamoregl"
(II) modeset(0): Refusing to try glamor on softpipe
(II) modeset(0): glamor initialization failed
```

Xorg 只有 modesetting/fbdev/amdgpu/radeon/vesa 驱动，没有 pvr/armsoc
厂商 DDX；glamor 因 GL 只有 softpipe 而拒绝启用。

## 判定

按 P5b-12 的二选一：

- **RockOS 也失败** → 问题是**厂商栈/驱动本身**（具体：本镜像的
  `pvrsrvkm.ko` 与运行内核 vermagic 不匹配，GPU 驱动无法加载，
  DT 节点没有驱动绑定，Mesa 只能走软件路径）；
- 因此“RockOS 成功 → 抓 ioctl 对比”的分支**不适用**：没有成功样本；
  RockOS 未安装 `strace`，本轮也没有做 ioctl 抓取。

对 P5b 的含义：

1. Asterinas 侧的 driver name=`pvr`、sysfs、DRM/PRIME/bridge 工作仍然
   有效，但**不能**用 RockOS 作为“pvr_dri 应该能工作”的参照；
2. 下一步应回到 vendor srvkm 侧：
   - 先让 RockOS 真正加载 `pvrsrvkm`（需要与运行内核匹配的镜像/模块），
     或在厂商支持下拿到可用的 RockOS + GPU 组合；
   - 成功后再抓 `PVR_SRVKM_CMD` 的 ioctl 序列，作为 Asterinas bridge
     的对照；
   - 或者直接分析 `pvrsrvkm.ko` 的 ioctl ABI，与 Asterinas 现有
     SRVCORE/bridge 实现逐条比对；
3. 在拿到可用的参照之前，不建议继续在 Asterinas 侧补 PVR bridge 命令。

## 边界

- 未改 Asterinas 内核/initramfs；未执行任何 `apt` 操作；
- RockOS 上尝试了 `modprobe pvrsrvkm` 与 `insmod <路径>`，均失败，
  未产生持久变更；
- 所有远端命令带 `timeout`；sudo 密码经 env 传入、未落盘，
  X11 命令通过 LightDM root Xauthority 以 root 运行；
- 串口/X11 输出只保留关键行，未刷屏。
