# P5b：厂商 UMD 首次上板（2026-10-23）

## 结论

- P5b 首次上板执行完成，但 **EGL 首次接触未通过**：
  `ASTERINAS_POWERVR_UMD status=failed stage=egl libsrv=24.2.6643903`；
- 三种 EGL 平台的结果：
  - `EGL_PLATFORM=surfaceless`：`EGL_RC=124`（30 s 超时，进程未返回）；
  - `EGL_PLATFORM=device`：`EGL_RC=1`，`eglGetDisplay failed`；
  - `EGL_PLATFORM=gbm`：`EGL_RC=1`，`eglInitialize failed`；
- gbm 的 verbose 错误是 **`DRI2: failed to create gbm device`**，
  说明失败点已落到 `gbm_create_device`/pvr DRI screen 初始化，而不是
  简单的库缺失或 `eglGetDisplay` 平台选择；
- 没有修改 initramfs：仍用原来的 1.3 MB `stage1-pvr-batch.cpio`
  （CRC32 `c8f825a7`），内核仍为 B2a 镜像（CRC32 `dce1d570`）。

## 板子 rootfs 关系（重要修正）

板级实测确认 RockOS 与 Asterinas guest **不共享 rootfs**：

```text
mmcblk1p1  500M  ext4  /boot (RockOS)
mmcblk1p2    4G  ext2  ASTER_BROWSERWEB  ← Asterinas guest 的 /
mmcblk1p3  114G  ext4  /                 ← RockOS 的 /
```

因此 `p5b-stage.sh` 把 bundle 放到 RockOS 的 `/home/debian/pvr` 后，
guest 里看不到它（本轮第一次启动已确认 `ls` 全部 No such file）。
最终采用的做法：

```sh
# RockOS 上（sudo）
mount /dev/mmcblk1p2 /mnt/aster_guest
mkdir -p /mnt/aster_guest/opt/pvr
cp -r /home/debian/pvr/. /mnt/aster_guest/opt/pvr/
sync
umount /mnt/aster_guest
```

guest 启动后 `/opt/pvr/{bin,lib,dri}` 全部存在，随后运行：

```sh
sh /opt/pvr/p5b-guest.sh /opt/pvr
```

脚本修正已经提交：`p5b-stage.sh` 的 `scp` 从 `"$BUNDLE/."` 改为
`"$BUNDLE"/*`（OpenSSH 新版拒绝 `.`），并在注释/输出中说明必须注入
guest 分区；`p5b-guest.sh` 在 `SRC=/opt/pvr` 时跳过自我复制。

## 环境

| 项 | 值 |
|---|---|
| 内核 | `/home/debian/kernel-pvr-b2a.Image`，CRC32 `dce1d570` |
| initramfs | `stage1-pvr-batch.cpio`，1,248,256 B，CRC32 `c8f825a7`，`initrd_size=0x131000` |
| bootargs | `asterinas.powervr=1 asterinas.powervr_dma_stage=1 asterinas.powervr_release=1 asterinas.powervr_job_noop=1` |
| guest UMD | `/opt/pvr/bin/eglconfigs`、`libsrv_um.so`、`libusc.so`、`libpvr_dri_support.so`、`dri/pvr_dri.so` |
| 环境变量 | `LD_LIBRARY_PATH=/opt/pvr/lib`、`LIBGL_DRIVERS_PATH=/opt/pvr/dri:/usr/lib/riscv64-linux-gnu/dri`、`MESA_LOADER_DRIVER_OVERRIDE=pvr` |

## 实测输出

```text
PVR_BUNDLE_PRESENT
-rwxr-xr-x 1 root root    31064 /opt/pvr/bin/eglconfigs
-rw-r--r-- 1 root root 17205256 /opt/pvr/dri/pvr_dri.so
-rw-r--r-- 1 root root   576400 /opt/pvr/lib/libsrv_um.so

EGL_PLATFORM=surfaceless EGL_RC=124
EGL_PLATFORM=device EGL_RC=1
eglGetDisplay failed
EGL_PLATFORM=gbm EGL_RC=1
eglInitialize failed
ASTERINAS_POWERVR_UMD status=failed stage=egl libsrv=24.2.6643903
```

额外诊断（`EGL_LOG_LEVEL=debug LIBGL_DEBUG=verbose`，gbm）：

```text
libEGL debug: EGL user error 0x3001 (EGL_NOT_INITIALIZED) in eglInitialize: DRI2: failed to create gbm device
libEGL debug: EGL user error 0x3001 (EGL_NOT_INITIALIZED) in eglInitialize
eglInitialize failed
```

`/opt/pvr/dri/pvr_dri.so` 在 guest 中可被加载，`/dev/dri/card0` 与
`renderD128` 都存在；`dri_gbm.so` 存在于
`/usr/lib/riscv64-linux-gnu/gbm/dri_gbm.so`。

## 失败点分析（已确认 vs 待查）

**已确认**：

1. 不是“库没放进去”：bundle 文件、guest 的 libEGL/libgbm/libdrm 都在；
2. 不是“文件路径”：`/opt/pvr` 下三类文件均存在；
3. 不是 `eglGetDisplay` 本身：device/gbm 都返回了 EGL 错误，
   gbm 的错误明确指向 `gbm_create_device`/DRI screen 初始化；
4. surfaceless 会挂起，说明 pvr 初始化路径里有阻塞（30 s 未返回），
   不是立即失败。

**待查（下一步，按代价排序）**：

1. `gbm_create_device(card0)` 在 **带 PVR 环境变量** 下的行为：
   本轮直接 ctypes 测试时没有设置 `MESA_LOADER_DRIVER_OVERRIDE=pvr`，
   测试被 SIGINT 中断，结果不作为根因；
2. pvr_dri/libpvr_dri_support 在 `gbm_create_device` 期间是否进入了
   未实现的 PVR bridge（用内核 `ASTERINAS_POWERVR_BRIDGE` 的
   `unsupported` 计数和 serial 日志确认）；
3. 是否需要 RockOS Mesa 22 的 `libEGL_mesa`/`libglapi`/`libgbm` 配套
   替换 guest 的 Mesa 24（ABI 风险在 P5c 清单中已记录）；
4. surfaceless 的阻塞点用 `strace -f -tt`（若 guest 有）或分段
   `eglGetDisplay`/`eglInitialize` 诊断拆开。

## 证据

- 串口日志：`/tmp/board_p5b_run2.serial.log`
  （归档 `target/board-p5b-20261023/board_p5b_run2.serial.log`）
- 失败 marker：`ASTERINAS_POWERVR_UMD status=failed stage=egl libsrv=24.2.6643903`
- 未修改 initramfs/内核；P5b 的下一步是 P5c 前置诊断，不是直接改内核。
