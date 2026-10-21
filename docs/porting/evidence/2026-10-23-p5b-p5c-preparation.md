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

## 下一步

1. 板子物理复位后先跑 `p5b-stage.sh` + guest 命令，记录
   `ASTERINAS_POWERVR_UMD`；
2. 若 `eglconfigs` 通过，P5c 尝试
   `Xorg -config /etc/X11/xorg.conf.d/10-pvr.conf`（或既有 modesetting
   配置）+ `MESA_LOADER_DRIVER_OVERRIDE=pvr`，观察 glamor 是否选择 EGL；
3. 若失败，按“缺库 → glibc → loader/驱动名 → EGL 平台 → DRM 节点”
   顺序记录，不修改内核。

## 边界

- 不修改 initramfs、内核或既有 marker；
- 二进制只放在 `/mnt/shared/xaj/vendor/`，仓库只提交脚本和本清单。
