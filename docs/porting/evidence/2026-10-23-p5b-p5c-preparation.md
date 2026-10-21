# P5b 上板就绪包 + P5c 依赖清单（2026-10-23）

## 结论

- 板子当前挂死（串口静默、ping down），本轮不碰板子、不改内核、不改
  initramfs；
- P5b 就绪包已整理为 rootfs 内目录 `opt/pvr/{bin,lib,dri}`，放在
  `/mnt/shared/xaj/vendor/p5b-rootfs-20261023/`（二进制不入库）；
- 新增宿主脚本 `tools/riscv/p5b/p5b-stage.sh`（scp 到板子
  `/home/debian/pvr`，并提供 HTTP 回退）和 guest 侧命令
  `tools/riscv/p5b/p5b-guest.sh`（从共享 rootfs 的
  `/home/debian/pvr` 或 `/tmp/pvr` 复制到 `/opt/pvr` 后跑 EGL）；
- P5c 的结论：guest 已有 Xorg/Mesa 基础栈，P5c 的增量主要是
  **与 RockOS Mesa 22 配套的 `pvr_dri.so` + vendor DDK 库 + EGL 厂商
  配置**，而不是重新安装整套 Xorg。

## P5b 就绪包

```
/mnt/shared/xaj/vendor/p5b-rootfs-20261023/
├── SHA256SUMS
└── opt/pvr/
    ├── bin/eglconfigs
    ├── lib/libpvr_dri_support.so
    ├── lib/libsrv_um.so
    ├── lib/libusc.so
    └── dri/pvr_dri.so
```

SHA-256：

| 文件 | SHA-256 |
|---|---|
| `bin/eglconfigs` | `bf2b7983ddc73a2bc97a60fc22297a8f3f4b9e56dfdbbf29ef18b037a25f7fb5` |
| `lib/libpvr_dri_support.so` | `c6ea11587b9a0b945179b803224d727730914ba6debfc3c35904b7475f456c86` |
| `lib/libsrv_um.so` | `fb2f574ea712955a0d97083da498368f839ae15fdc2c3ef8e869252f444c22ab` |
| `lib/libusc.so` | `f212ddca793473f6eec0ec1d03a080fb230e276a18eb8eaaab0db47ce372f7dd` |
| `dri/pvr_dri.so` | `c49db9691b40f05a44d05e79b8a574c07a523be06516af8e291afe4aa2990dbb` |

这些文件来自 RockOS 板子 rootfs 的 `/usr/bin/eglconfigs`、
`/usr/lib/lib{pvr_dri_support,srv_um,usc}.so`、
`/usr/lib/riscv64-linux-gnu/dri/pvr_dri.so`（RockOS GPU 包 24.2 和
RockOS Mesa 22.0.3）。

### 上板步骤（板子恢复后）

```bash
# 宿主：上传到板子 Debian rootfs（不经过 initramfs）
tools/riscv/p5b/p5b-stage.sh

# 或者 guest 网络可用时走 HTTP 回退
tools/riscv/p5b/p5b-stage.sh --http
```

guest 串口里执行 `tools/riscv/p5b/p5b-guest.sh` 里的命令：

```sh
SRC=/home/debian/pvr
mkdir -p /opt/pvr && cp -r "$SRC/." /opt/pvr/
ls -l /opt/pvr/bin/eglconfigs /opt/pvr/dri/pvr_dri.so /opt/pvr/lib/libsrv_um.so
export LD_LIBRARY_PATH=/opt/pvr/lib
export LIBGL_DRIVERS_PATH=/opt/pvr/dri
export MESA_LOADER_DRIVER_OVERRIDE=pvr
for p in surfaceless device gbm; do
  EGL_PLATFORM=$p timeout 30 /opt/pvr/bin/eglconfigs >/tmp/egl.out 2>&1
  echo "EGL_PLATFORM=$p EGL_RC=$?"
  head -60 /tmp/egl.out
done
```

成功时 guest 打印：

```text
ASTERINAS_POWERVR_UMD status=ok stage=egl platform=<...> libsrv=24.2.6643903
```

失败时保留 `EGL_RC` 和 `/tmp/egl.out` 前 60 行；P5b 的后续排查顺序：
缺失库 → glibc 版本 → Mesa loader/驱动名 → EGL 平台/DRM 节点。

**关键约束**：本轮没有修改 `stage1-pvr-batch.cpio`（1.3 MB）及其
`initrd_size`；pvr 目录通过 Debian rootfs 传输，guest 内只做
`/home/debian/pvr → /opt/pvr` 的复制。

## P5c 依赖清单（Xorg glamor / EGL）

### 已在 guest 中确认存在的部分

在 Asterinas guest 的 Debian rootfs 中已看到：

| 组件 | 路径/版本 |
|---|---|
| EGL loader | `/usr/lib/riscv64-linux-gnu/libEGL.so.1 → libEGL.so.1.1.0` |
| Mesa EGL | `libEGL_mesa.so.0 → .0.0.0` |
| libdrm | `libdrm.so.2.124.0` |
| gbm | `libgbm.so.1.0.0` |
| Mesa DRI 目录 | `/usr/lib/riscv64-linux-gnu/dri/`（含大量 DRM DRI 驱动） |
| Xorg | 既有 E3 Firefox/Xorg 栈，`modesetting_drv.so` 已存在 |

因此 P5c **不需要**重新安装 Xorg 全家桶；需要补齐的是 PVR 专用件。

### 需要加入 `/opt/pvr`（P5c 增量）

| 文件 | 来源 | 用途 |
|---|---|---|
| `dri/pvr_dri.so` | RockOS Mesa 22.0.3 | Mesa DRI 驱动，内部 dlopen `libpvr_dri_support.so` |
| `lib/libpvr_dri_support.so` | RockOS DDK 24.2 | PowerVR DRI/EGL 支撑，使用 libdrm PRIME |
| `lib/libsrv_um.so` | RockOS DDK 24.2 | PVR Services 用户态，走 `PVR_SRVKM_CMD` |
| `lib/libusc.so` | RockOS DDK 24.2 | 着色器编译器（pvr 驱动按需加载） |
| `bin/eglconfigs` | RockOS DDK 24.2 | 最小 EGL 验收程序 |

`pvr_dri.so` 的直接依赖（RockOS 上 `ldd` 观察）：`libglapi.so.0`、
`libdrm.so.2`、`libLLVM-16.so.1`、`libexpat.so.1`、`libz.so.1`、
`libzstd.so.1`、`libsensors.so.5`、`libdrm_{radeon,amdgpu,nouveau}`、
`libelf.so.1`、`libxcb-dri3.so.0`、`libstdc++.so.6`、`libgcc_s.so.1`、
`libm.so.6`、`libc.so.6`。其中大部分应已由 guest 的 Mesa/Xorg 栈提供；
板子恢复后应用 `ldd /opt/pvr/dri/pvr_dri.so` 逐项核对，缺什么补什么。

### Xorg glamor 运行要求

1. **X server**：`Xorg`/`xserver-xorg-core` 带 glamor 和 EGL 支持；
   `modesetting_drv.so` 以 `Option "kmsdev" "/dev/dri/card0"` 工作。
   guest 已有该栈（E3 已能启动 Xorg）。
2. **glamor 依赖**：`libepoxy.so.0`、`libEGL.so.1`、`libgbm.so.1`、
   `libdrm.so.2`、`libpixman-1.so.0`、`libxshmfence.so.1`；
   这些随现有 Xorg/Mesa 栈提供。
3. **EGL 平台**：glamor 的 modesetting 路径使用
   `EGL_PLATFORM_GBM_KHR`（`eglGetPlatformDisplayEXT` +
   `gbm_create_device(card0)`），因此需要可用的 `/dev/dri/card0` 和
   gbm 后端；`pvr_dri.so` 必须能被 Mesa 按 `MESA_LOADER_DRIVER_OVERRIDE=pvr`
   加载。
4. **PRIME/dmabuf**：glamor 共享 pixmap 走 `gbm_bo_get_fd` +
   `EGL_EXT_image_dma_buf_import`；需要 DRM PRIME 和
   `DMA_BUF_IOCTL_SYNC`（P4 已实现）。
5. **KMS**：`CREATE_DUMB/MAP_DUMB/ADDFB/SETCRTC/PAGE_FLIP`（已有）；
   glamor vsync 需要 page-flip 完成事件，当前仍是同步 flip（P5c 风险）。
6. **版本配套**：`pvr_dri.so` 来自 Mesa 22.0.3，最好与 RockOS 的
   `libEGL_mesa.so.0`/`libglapi.so.0`/`libgbm.so.1` 配套使用；
   若直接用 guest 的 Mesa 24 栈，DRI/glapi ABI 不匹配是首要风险。
   P5c 可先试 guest 栈 + `MESA_LOADER_DRIVER_OVERRIDE=pvr`，失败则
   把 RockOS Mesa 22 的 `libEGL_mesa`/`libglapi`/`libgbm` 一并放入
   `/opt/pvr/lib`，并用 `__EGL_VENDOR_LIBRARY_FILENAMES` 指向
   `/opt/pvr/lib/50_pvr.json`。

## P5b-2：上板复核 `DRI2: failed to create gbm device`

板子恢复后按“先确认文件、再查节点、再逐层 EGL”的顺序复核。

### 文件与节点

guest 采用 rootfs 注入（RockOS 在 `/dev/mmcblk1p3`、guest 在
`/dev/mmcblk1p2`，见 `2026-10-23-p5b-umd-first-contact.md`）：把 bundle
复制到 guest `/opt/pvr`，未改 initramfs。sha256 与宿主 manifest 对比：

| 文件 | sha256 |
|---|---|
| `bin/eglconfigs` | `bf2b7983ddc73a2bc97a60fc22297a8f3f4b9e56dfdbbf29ef18b037a25f7fb5` |
| `lib/libsrv_um.so` | `fb2f574ea712955a0d97083da498368f839ae15fdc2c3ef8e869252f444c22ab` |
| `lib/libpvr_dri_support.so` | `c6ea11587b9a0b945179b803224d727730914ba6debfc3c35904b7475f456c86` |
| `lib/libusc.so` | `f212ddca793473f6eec0ec1d03a080fb230e276a18eb8eaaab0db47ce372f7dd` |
| `dri/pvr_dri.so` | `c49db9691b40f05a44d05e79b8a574c07a523be06516af8e291afe4aa2990dbb` |

全部匹配，排除“注入路径/文件缺失”。`/dev/dri` 同时存在：

```text
crw------- 1 root root 226,   0 /dev/dri/card0
crw------- 1 root root 226, 128 /dev/dri/renderD128
```

因此不是“只有 renderD128、没有 card0”的情况。

### EGL 结果（重复确认）

```text
EGL_PLATFORM=surfaceless EGL_RC=124   # 30 s 超时，未返回
EGL_PLATFORM=device     EGL_RC=1      # eglGetDisplay failed
EGL_PLATFORM=gbm        EGL_RC=1      # eglInitialize failed
```

`EGL_LOG_LEVEL=debug LIBGL_DEBUG=verbose` 的 gbm 输出仍为：

```text
libEGL debug: EGL user error 0x3001 (EGL_NOT_INITIALIZED) in eglInitialize: DRI2: failed to create gbm device
libEGL debug: EGL user error 0x3001 (EGL_NOT_INITIALIZED) in eglInitialize
eglInitialize failed
```

### 本轮新增观察

- `ldd /opt/pvr/bin/eglconfigs` 在 guest 里会阻塞（未能在限定时间内返回），
  因此本轮不能把 `ldd` 作为依赖完整性证据；文件哈希已足以排除缺文件。
- 直接 `gbm_create_device` 的串口探针不稳定（长命令行/前台任务竞争），
  结果不足以判定根因；不把它写成结论。
- 失败点可以确定在 `gbm_create_device`/pvr DRI screen 初始化，而不是
  `eglGetDisplay` 或文件缺失；surfaceless 的 30 s 阻塞说明 pvr 初始化
  路径里有等待（可能是 PVR bridge/event，尚未确认）。

### P5b-3 建议顺序

1. 用 `strace -f -tt`（若 guest 有）或 `LD_DEBUG=libs` 抓
   `gbm_create_device` 的系统调用/库加载，确认阻塞在哪个 ioctl；
2. 同时观察内核串口 `ASTERINAS_POWERVR_BRIDGE ... unsupported=` 是否增长，
   判断是否走进了未实现的 bridge 命令；
3. 用 guest 自带 Mesa 的 `LIBGL_ALWAYS_SOFTWARE=1` + surfaceless 做基线，
   区分“EGL 栈本身”与“pvr 驱动初始化”；
4. 若确认是 Mesa 22/guest Mesa 24 ABI 问题，再把 RockOS 的
   `libEGL_mesa`/`libglapi`/`libgbm` 放入 `/opt/pvr/lib` 重试。

## P5b-3：strace / loader 定位

### 工具与启动

- guest **没有 `strace`**：`/usr/bin/strace` 不存在，无法直接抓 ioctl；
- 改为 `LD_DEBUG=libs,files` + `LIBGL_DEBUG=verbose`，并重新启动一次带
  `asterinas.dri_trace=1 asterinas.dri_trace_fail=1` 的 guest；
- 已确认 U-Boot 的 `setenv bootargs` 中确实包含这两个 trace 标志。

### 观察

1. `LD_DEBUG=libs,files` 日志（522 行）中：
   - `eglconfigs` 先加载 `libEGL.so.1`、`libGLdispatch.so.0`，
     `LD_LIBRARY_PATH=/opt/pvr/lib` 的搜索路径生效；
   - 搜 `pvr_dri` 结果为 **0 行**：`pvr_dri.so` 没有被 loader 打开；
   - 搜 `libsrv`/`libusc` 也没有出现（未进入 PVR 驱动/服务加载）。
2. 开 `asterinas.dri_trace=1` 后跑 gbm EGL，串口
   `DRI_IOCTL` 计数为 **0**；即在整个 20 s 内没有任何 DRM ioctl
   到达驱动。
3. 这次 gbm 运行本身以 `timeout` 结束（`EGL_TRACE_RC=124`，
   `/tmp/egl_trace.out` 为 0 字节）；前一轮同一路径曾快速返回
   `DRI2: failed to create gbm device`。两次行为不完全一致，说明
   gbm/pvr 初始化路径存在“有时快速失败、有时阻塞”的状态差异，
   不能只用单一返回值下结论。

### 分类结论

- **不是 ②（缺 DRM ioctl）** 的有力证据：trace 开着却没有任何
  `DRI_IOCTL`；目前不能指认缺哪条 DRM 命令。
- 证据更偏向 **①/③**：`pvr_dri.so` 没有被 loader 打开，或它在任何
  DRM ioctl 之前就阻塞在 `libsrv_um`/`libpvr_dri_support` 的初始化里；
  但 exact 阻塞调用点仍 **TODO**（缺 strace）。
- 需要先验证 trace 本身有效（用一个已知 DRM ioctl，如
  `DRM_IOCTL_VERSION`，确认串口出现 `DRI_IOCTL`），再重跑 EGL，
  否则“0 条 DRI_IOCTL”可能只是 trace 未触发。
- 下一轮 P5b-4 建议：给 guest 塞入 `strace`（或 `ltrace`）二进制，
  抓 `openat("pvr_dri.so")`、`openat("libsrv_um.so")` 与首个阻塞
  syscall；若 pvr_dri 确实未被打开，优先检查
  `MESA_LOADER_DRIVER_OVERRIDE` 与 Mesa `libEGL_mesa` 的 DRI loader
  路径/ABI，而不是继续补 DRM ioctl。

## P5b-4：Mesa 版本 / 驱动 ABI 匹配

### guest 侧事实

从 guest 镜像 `/usr/lib/riscv64-linux-gnu/` 直接读取（不需要再启动板子）：

| 项 | guest 实际值 |
|---|---|
| Mesa 版本 | **25.0.7**（`libgallium-25.0.7-2+deb13u1.so`） |
| DRI 目录 | `/usr/lib/riscv64-linux-gnu/dri/`，含 `swrast_dri.so`、`kms_swrast_dri.so` 等；**没有 `pvr_dri.so`** |
| EGL | `libEGL.so.1.1.0` + `libEGL_mesa.so.0.0.0`（Mesa 25） |
| libgbm / libdrm | `libgbm.so.1.0.0` / `libdrm.so.2.124.0` |
| libglapi | **不存在**（guest Mesa 25 不再提供独立的 `libglapi.so.0`） |
| LLVM | 只有 `libLLVM.so.19.1` / `libLLVM-19.so`，**没有 16** |

### pvr_dri 的 ABI/依赖事实

RockOS 的 `pvr_dri.so` 来自 **Mesa 22.0.3**（RockOS `rockos-mesa22.0.3`），
`readelf -d` 的 NEEDED 包括：

```text
libglapi.so.0
libdrm.so.2
libLLVM-16.so.1
libexpat.so.1
libz.so.1
libzstd.so.1
libsensors.so.5
libdrm_radeon.so.1
libelf.so.1
libdrm_amdgpu.so.1
libdrm_nouveau.so.2
libxcb-dri3.so.0
libstdc++.so.6
...
```

它导出 `__driDriverGetExtensions_pvr`（Mesa DRI 入口），但：

- guest **没有 `libglapi.so.0`**（NEEDED 直接缺失）；
- guest 只有 LLVM 19，**没有 `libLLVM-16.so.1`**（NEEDED 直接缺失）。

RockOS 上对应文件存在：`libglapi.so.0.0.0`（313,528 B）、
`libLLVM-16.so.1`（103,743,080 B）、`libEGL_mesa.so.0.0.0`（Mesa 22）。

### 结论

- **不是 a) 驱动目录/文件名**：`/opt/pvr/dri/pvr_dri.so` 存在，
  `MESA_LOADER_DRIVER_OVERRIDE=pvr` 指向的名字也匹配；
- **主要是 b) Mesa 版本/依赖 ABI 不匹配**：guest Mesa 25.0.7 与
  RockOS Mesa 22.0.3 的 `pvr_dri` 不配套；即使 loader 打开了
  `pvr_dri.so`，`libglapi.so.0` 与 `libLLVM-16.so.1` 缺失也会让
  `dlopen` 失败。这是目前已确认的硬阻塞；
- **c) loader/ICD 不是首要问题**：`LD_DEBUG` 中 `pvr_dri` 0 次出现，
  但 gbm 在更早的 `gbm_create_device` 就失败，尚不能把“未打开”
  单独归因于 ICD；在补齐 ABI 依赖之前讨论 `libEGL_pvr` 意义有限。

### 推荐下一步（P5b-5，按优先级）

1. **首选：用 guest Mesa 25 源码重编 pvr_dri**（RockOS Mesa 22 的
   `src/gallium/drivers/pvr` 移植到 Mesa 25），得到与 guest
   glapi/gallium/LLVM 匹配的 `.so`；这是根治 ABI 的方案；
2. 次选：把 RockOS Mesa 22 的 `libglapi.so.0`、`libLLVM-16.so.1`、
   `libEGL_mesa.so.0`、`libgbm.so.1` 一并放入 `/opt/pvr/lib` 并用
   `LD_LIBRARY_PATH` 优先加载，做一次“整套 Mesa 22”验证；体积约
   100 MB（LLVM 16），且要防止与 guest Mesa 25 混用；
3. 不推荐继续补 DRM ioctl：当前没有证据表明缺少 DRM 命令。

## P5b-5：整套 RockOS Mesa 22 栈验证

### 新增到 bundle 的文件

按“快、可逆、不重编”的方案，从在线 RockOS 取整套 Mesa 22 依赖，放进
`/mnt/shared/xaj/vendor/p5b-rootfs-20261023/opt/pvr/mesa22/`：

```text
mesa22/lib/libEGL_mesa.so.0 -> .0.0.0 (255,224 B)
mesa22/lib/libglapi.so.0    -> .0.0.0 (313,528 B)
mesa22/lib/libgbm.so.1      -> .1.0.0 (60,320 B)
mesa22/lib/libLLVM-16.so.1  (103,743,080 B)
mesa22/lib/libdrm_radeon.so.1 (43,160 B)
mesa22/lib/libdrm_nouveau.so.2 (26,912 B)
mesa22/lib/dri/{pvr,swrast,kms_swrast}_dri.so
```

guest 端环境改为：

```sh
LD_LIBRARY_PATH=/opt/pvr/mesa22/lib:/opt/pvr/lib
LIBGL_DRIVERS_PATH=/opt/pvr/mesa22/lib/dri
MESA_LOADER_DRIVER_OVERRIDE=pvr
```

`p5b-guest.sh` 已更新；bundle 的 `SHA256SUMS` 已重新生成（14 项）。

### 实测结果（guest Mesa 22 栈）

- 首次发现 `pvr_dri` 还缺 `libdrm_radeon.so.1` 和
  `libdrm_nouveau.so.2`（`LD_PRELOAD` 直接报
  `libdrm_radeon.so.1: cannot open shared object file`）；补入后该错误消失。
- `LD_PRELOAD=libLLVM-16.so.1 timeout 10 /bin/true` 超时（RC=124），
  改 `timeout 60` 后 RC=0：**LLVM 16 加载很慢，但能加载**。
- EGL 仍然失败：

```text
EGL_PLATFORM=surfaceless EGL_RC=124   # p5b 脚本 30 s 内未返回
EGL_PLATFORM=device     EGL_RC=1      # eglGetDisplay failed
EGL_PLATFORM=gbm        EGL_RC=1      # eglInitialize failed
```

单独把 surfaceless 的 timeout 提到 120 s 后，输出变为快速失败：

```text
eglGetDisplay failed
```

说明 Mesa 22 栈下 surfaceless 不再是“纯等待”，而是 EGL 显示/驱动
初始化失败；gbm 仍在 `eglInitialize` 失败（前一轮 verbose 为
`DRI2: failed to create gbm device`）。

最终 marker：

```text
ASTERINAS_POWERVR_UMD status=failed stage=egl reason=egl-init libsrv=24.2.6643903
```

### 结论

- 补齐 Mesa 22 依赖**排除了“缺 libglapi/libLLVM16”这一层**，但没有让
  EGL 通过；`pvr_dri` 依赖链现在可以被 loader 解析（LLVM16 加载慢），
  EGL 仍卡在平台/GBM 初始化。
- 这支持 P5b-4 的判断：问题不只是缺库，而是 **Mesa 22 pvr_dri 与
  guest Mesa 25/平台栈不配套**；继续拼库的收益递减。
- 下一步首选仍是 **用 Mesa 25 源码重编 pvr_dri**；次选是补一个
  PVR 专用 EGL ICD 或在 guest 里整套替换 Mesa（风险高，易与系统
  Xorg/Firefox 冲突）。
- 另一个需并行确认的点：`gbm_create_device`/`drmGetNodeTypeFromFd`
  是否依赖 Asterinas 未提供的 sysfs 节点；这与 Mesa 版本无关，但会
  让 gbm 在任何 Mesa 下都失败。

## P5b-6：EGL 初始化失败的归因（sysfs / 源码可行性）

### sysfs / dev 节点

guest 实测：

```text
ls /sys/class          -> graphics  tty
ls /sys/class/drm      -> No such file or directory
ls /sys/class/graphics -> 只有 fb0
```

- **没有 `/sys/class/drm`**（没有 `card0`/`renderD128` 的 class 项）；
- `/sys/dev/char` 也不可用（`ls /sys/dev` 只看到 fb 相关项）；
- `/dev/dri/card0` 与 `renderD128` 在 devtmpfs 中存在，但 sysfs 元数据缺失。

这与 libdrm/Mesa 的实际用法直接冲突：

- `drmGetNodeTypeFromFd(fd)` 依赖
  `/sys/dev/char/<major>:<minor>` 的 readlink 判断 render/card；
- `drmGetDevices2()` 扫描 `/sys/class/drm` 发现设备；
- Mesa gbm/dri loader 用这些结果选择驱动和节点。

因此 `DRI2: failed to create gbm device` **很可能首先是 sysfs 缺失**，
而不是（或不仅是）Mesa 22/25 的 ABI。pvr_dri 内部
`libpvr_dri_support`/`libsrv_um` 也导入 `drmGetNodeTypeFromFd`
与 `drmGetDevices2`，同样会被这个缺口挡住。

### 第一条 EGL 错误

带 `LIBGL_DEBUG=verbose EGL_LOG_LEVEL=debug EGL_PLATFORM=gbm` 的运行
（Mesa 22 栈）第一条错误仍是：

```text
libEGL debug: EGL user error 0x3001 (EGL_NOT_INITIALIZED) in eglInitialize: DRI2: failed to create gbm device
```

之后才是 `eglInitialize failed`。这与“gbm 在打开/识别 DRM 节点阶段
失败”一致。

### 重编 pvr_dri 的可行性（只调研）

- RockOS Mesa fork：`https://github.com/rockos-riscv/mesa`，分支
  `rockos-mesa22.0.3`；树里有
  `src/gallium/drivers/pvr`、`src/gallium/frontends/pvr`、
  `dri_support.h`、`pvrcompat.c` 等，说明驱动源码可得；
- 本地（当前宿主）没有该 fork 的完整树，需要 clone/sparse checkout；
- 重编到 Mesa 25 的主要成本是 gallium 接口差异（`pipe_screen`/
  `pipe_context`、资源创建、fence/query 等）以及 Mesa 25 的构建系统
  （meson 选项、`libgallium`/`libglapi` 变化）。工作步骤建议：

1. 取 RockOS Mesa 22 的 `src/gallium/drivers/pvr` + `frontends/pvr`
   和对应 meson 片段；
2. 用 Debian 13 / guest Mesa 25.0.7 源码树做 riscv64 构建环境，
   先让 `pvr_dri.so` 编译通过（不追求功能）；
3. 对齐 Mesa 25 的 gallium/`pipe_loader` 接口与 `libglapi` 导出；
4. 用 `dlopen`/`LD_PRELOAD` 验证加载，再跑 `eglconfigs`；
5. 若接口差异过大，退化为“在 guest 内整套 Mesa 22 rootfs”方案，
   而不是逐库拼装。

粗估：不是一天内能完成的工作；应在 sysfs 修复后重新评估是否仍有
必要。

### 结论与优先级

1. **先补 sysfs/DRM class + `/sys/dev/char` 元数据**（内核/sysfs，
   本轮不做）：这是当前最直接、最可能一次解锁 gbm + libdrm 设备
   发现的缺口；
2. 修好后用现有 Mesa 22/25 栈复跑 `eglconfigs`，再判断 Mesa ABI
   是否仍是第二层阻塞；
3. 只有在 sysfs 修复后仍失败时，才进入 **Mesa 25 重编 pvr_dri**；
4. srvkm 时序/OS-online 不是当前第一优先级：目前 EGL 还没走到
   pvr_dri 的 bridge 初始化。

## 下一步

1. 板子物理复位后先跑 `p5b-stage.sh` + rootfs 注入 + guest 命令，记录
   `ASTERINAS_POWERVR_UMD`；
2. 若 `eglconfigs` 通过，P5c 尝试
   `Xorg -config /etc/X11/xorg.conf.d/10-pvr.conf`（或既有 modesetting
   配置）+ `MESA_LOADER_DRIVER_OVERRIDE=pvr`，观察 glamor 是否选择 EGL；
3. 若失败，按“缺库 → glibc → loader/驱动名 → EGL 平台 → DRM 节点”
   顺序记录，并优先做 P5b-3 的 strace/LD_DEBUG 定位，不修改内核。

## 边界

- 不修改 initramfs、内核或既有 marker；
- 二进制只放在 `/mnt/shared/xaj/vendor/`，仓库只提交脚本和本清单。
