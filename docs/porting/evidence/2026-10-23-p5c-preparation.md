# P5c 就绪包：Xorg glamor + PVR DRI（2026-10-23）

## 结论

- 板子仍挂（串口静默、ping down），本轮不碰板子、不改内核、不改
  initramfs；
- P5c 增量包已整理到 `/mnt/shared/xaj/vendor/p5c-rootfs-20261023/`：
  PVR 专用的 `pvr_dri.so`、`libpvr_dri_support.so`、`libsrv_um.so`、
  `libusc.so`、`eglconfigs`，加上 vendor 的 `10-pvr.conf`、
  `powervr.ini` 和一份显式 glamor 的 Xorg 配置；
- `libEGL_mesa.so.0`、`libgbm.so.1`、`libdrm.so.2`、`libepoxy.so.0`、
  `libpixman-1.so.0` **在 Asterinas guest 的 Debian rootfs 中已存在**
  （前一轮串口核对），本轮**不打包、不替换**，在 bundle 里用
  `opt/pvr/lib/README.present-in-guest` 标注；
- 新增宿主脚本 `tools/riscv/p5c/p5c-stage.sh` 和 guest 侧
  `tools/riscv/p5c/p5c-guest.sh`：后者先验证库/DRM 节点，再启动
  Xorg（modesetting + `kmsdev=/dev/dri/card0` + glamor），成功打印
  `ASTERINAS_POWERVR_XORG status=ok backend=glamor`。

## 包布局

```
/mnt/shared/xaj/vendor/p5c-rootfs-20261023/
├── SHA256SUMS
├── etc/
│   ├── powervr.ini                     # vendor 原文件
│   └── X11/xorg.conf.d/
│       ├── 10-pvr.conf                 # vendor 原文件（modesetting/card0）
│       └── 11-pvr-glamor.conf          # 显式 AccelMethod=glamor
└── opt/pvr/
    ├── bin/eglconfigs
    ├── dri/pvr_dri.so
    └── lib/
        ├── libpvr_dri_support.so
        ├── libsrv_um.so
        ├── libusc.so
        └── README.present-in-guest
```

SHA-256：

| 文件 | SHA-256 |
|---|---|
| `etc/powervr.ini` | `4dcb09b7d91f92f7a3000b2f04fa7313c19766c22b403764d2e0ccab9babbc2b` |
| `etc/X11/xorg.conf.d/10-pvr.conf` | `ae66380e48b433fc11c9f996ff8165019ec2fc6d8588a03a401ca8f1cbbad382` |
| `etc/X11/xorg.conf.d/11-pvr-glamor.conf` | `f30a9c11aa282a93a5e628addeff2e96505542d59a6f6e45b198c734ab6e1017` |
| `opt/pvr/bin/eglconfigs` | `bf2b7983ddc73a2bc97a60fc22297a8f3f4b9e56dfdbbf29ef18b037a25f7fb5` |
| `opt/pvr/dri/pvr_dri.so` | `c49db9691b40f05a44d05e79b8a574c07a523be06516af8e291afe4aa2990dbb` |
| `opt/pvr/lib/libpvr_dri_support.so` | `c6ea11587b9a0b945179b803224d727730914ba6debfc3c35904b7475f456c86` |
| `opt/pvr/lib/libsrv_um.so` | `fb2f574ea712955a0d97083da498368f839ae15fdc2c3ef8e869252f444c22ab` |
| `opt/pvr/lib/libusc.so` | `f212ddca793473f6eec0ec1d03a080fb230e276a18eb8eaaab0db47ce372f7dd` |

来源：RockOS GPU 包 `eswin-eic7x-gpu` 24.2 与 RockOS Mesa 22.0.3 的
`pvr_dri.so`（前一轮从板子 rootfs 取出的同一批文件）。

## guest 已有、无需替换

前一轮在 Asterinas guest 中实际看到：

| 库 | 观察到的版本/路径 |
|---|---|
| `libEGL.so.1` | `libEGL.so.1.1.0`（glvnd loader） |
| `libEGL_mesa.so.0` | `libEGL_mesa.so.0.0.0` |
| `libdrm.so.2` | `libdrm.so.2.124.0` |
| `libgbm.so.1` | `libgbm.so.1.0.0` |
| `libglapi.so.0` | 随 Mesa 栈 |
| `libepoxy.so.0` | 随 Xorg/glamor 栈（E3 Xorg 已运行） |
| `libpixman-1.so.0` | 随 Xorg 栈（E3 Xorg 已运行） |

因此 P5c 不覆盖这些库；若某个精简 guest 缺失，再从 RockOS rootfs 的
对应路径复制到 `opt/pvr/lib/`，并更新 `SHA256SUMS`。

**ABI 风险**：`pvr_dri.so` 来自 Mesa 22.0.3，guest 的 Mesa 栈来自
Debian（较新）。若 DRI/glapi ABI 不匹配，下一步是把 RockOS 的
`libEGL_mesa.so.0`/`libglapi.so.0`/`libgbm.so.1` 一并放入
`/opt/pvr/lib`，用 `LD_LIBRARY_PATH` 和 `__EGL_VENDOR_LIBRARY_FILENAMES`
指向 PVR 厂商 JSON；这属于 P5c 的备选路径，不在本包预置。

## 上板步骤（板子恢复后）

宿主：

```bash
tools/riscv/p5c/p5c-stage.sh
# 或 guest 网络可用时
tools/riscv/p5c/p5c-stage.sh --http
```

guest 串口：

```sh
sh /path/to/p5c-guest.sh /home/debian/pvr-p5c
# 或手工执行脚本中的命令
```

脚本行为：

1. 把 `opt/pvr` 复制到 `/opt/pvr`，把 `etc` 合并到 `/etc`；
2. 验证 `/opt/pvr/bin/eglconfigs`、`pvr_dri.so`、`libsrv_um.so` 存在，
   缺少则打印 `ASTERINAS_POWERVR_XORG status=failed reason=missing-lib`；
3. 验证 `/dev/dri/card0`、`/dev/dri/renderD128`，缺失打印
   `reason=no-drm-node`；
4. 先跑一次 `EGL_PLATFORM=gbm eglconfigs`（非致命，日志留在
   `/tmp/p5c-egl.out`）；
5. 用临时配置启动 `Xorg :99`（modesetting + card0 + glamor），等待
   X socket，最多 10 s；
6. 检查 `/tmp/xorg-pvr.log` 的 glamor/EGL 行，打印
   `ASTERINAS_POWERVR_XORG status=ok backend=glamor display=:99` 或
   `status=failed reason=xorg-start|glamor-not-egl`。

## 已知风险 / 下一步

- **Xorg 启动**：guest 已有 E3 Xorg 栈，但 headless 串口下启动 `:99`
  可能受 VT/DRM master 或已有显示管理器影响；失败时用 `xorg-start`
  记录，不改内核。
- **glamor EGL 平台**：modesetting 走 `EGL_PLATFORM_GBM_KHR`；guest 的
  `libgbm` 必须能打开 `/dev/dri/card0`。若 gbm 失败，先试
  `LIBGL_ALWAYS_SOFTWARE=1` 确认 Xorg 本身可起，再定位 PVR。
- **fence/page-flip**：glamor vsync 依赖 page-flip 完成事件，当前内核
  仍为同步 flip；P5c 若要观察 DRI3/Present 可能暴露缺口。
- 板子恢复后按 `p5c-stage.sh` → guest 脚本 → 记录 marker；失败时保留
  `/tmp/xorg-pvr.log`、`/tmp/p5c-egl.out` 和 `/var/log/Xorg.99.log`。

## 边界

- 不修改 initramfs、内核或既有 marker；
- 不在仓库提交 vendor 二进制；本文件只记录路径与校验和。
