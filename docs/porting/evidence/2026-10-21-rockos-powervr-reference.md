# RockOS PowerVR 参考栈验证（Megrez，2026-10-21）

## 结论

- 用与厂商模块**同源**的 `-origin` 内核（`6.6.87-win2030`）引导后，
  PowerVR AXM-8-256 的厂商 KMD 自动加载，固件加载成功，出现
  `card1` / `renderD128`；
- Xorg `modeset` 的 glamor 在真机上**启用了 PowerVR 加速**：
  `glamor X acceleration enabled on PowerVR A-Series AXM-8-256`；
- 因此"硬件 + 厂商软件栈"在 Megrez 上是可用的；Asterinas 侧缺的是
  内核驱动与其用户态接口，而不是硬件能力；
- 板载 KVM + virgl 的捷径仍然不通：即使有 `renderD128`，QEMU
  `egl-headless` 仍报 `eglChooseConfig failed (core mode)`（厂商 Mesa
  只提供 GLES 配置，virglrenderer 请求的 core GL 配置不存在）。

本收据只证明 **RockOS 参考栈**，不是 Asterinas 结果。

## 引导方式

- 默认 extlinux 未修改；通过 U-Boot 一次性加载
  `/boot/vmlinuz-6.6.87-win2030-origin`（包内核）+
  `/boot/initrd.img-6.6.87-win2030` + 厂商
  `eic7700-milkv-megrez.dtb`，bootargs 使用 RockOS 原 extlinux 参数；
- 运行中的 RockOS 自定义内核（`6.6.87`，2025-09-01 构建）与厂商模块
  vermagic `6.6.87-win2030` 不匹配；匹配的包内核仍在 `/boot`，
  `linux-image-6.6.87-win2030` 版本
  `6.6.87-2025.04.20.18.43+bb0c69aea`。

## 串口/日志证据

```text
# dmesg（origin 内核）
pvrsrvkm 51400000.gpu: EM: created perf domain
PVR_K:  153: RGX Device registered with BVNC 30.3.408.101
PVR_K:  499: AXI fabric coherency (RGX_CR_SOC_AXI): 0xc
PVR_K:  499: RGX Firmware image 'rgx.fw.30.3.408.101' loaded
PVR_K:  499: Shader binary image 'rgx.sh.30.3.408.101' loaded

# /var/log/Xorg.0.log:82-83
[    18.985] (II) modeset(0): glamor X acceleration enabled on PowerVR A-Series AXM-8-256
[    18.985] (II) modeset(0): glamor initialized
```

设备与驱动：

| 项 | 值 |
|---|---|
| 内核 | `uname -r` = `6.6.87-win2030` |
| GPU 设备 | `/sys/class/drm/card1` → `platform-51400000.gpu-card`；driver `pvrsrvkm` |
| render node | `/dev/dri/renderD128`，`platform-51400000.gpu-render` |
| 显示设备 | `/sys/class/drm/card0` → driver `es_drm` |
| 模块 | `/lib/modules/6.6.87-win2030/kernel/drivers/gpu/drm/img/img-volcanic/pvrsrvkm.ko`，vermagic `6.6.87-win2030` |
| 固件 | `rgx.fw.30.3.408.101`（126,976 B）、`rgx.sh.30.3.408.101`（364,684 B） |
| 用户态 | `eswin-eic7x-gpu 24.2+0rockos3`；Mesa `22.3.5+1rockos1+0pvr2` 含 `pvr_dri.so` |

## 板载 virgl smoke（失败）

`qemu-system-riscv64 -display egl-headless -device virtio-gpu-gl-pci
-S -accel tcg`（10 秒有界）：

```text
qemu-system-riscv64: egl: eglChooseConfig failed (core mode): EGL_SUCCESS
qemu-system-riscv64: egl: render node init failed
QEMU_RC=1
```

此前 2026-10-19 的 S13 no-go 原因是"没有 render node"；本次重新判定后，
render node 已存在，但厂商 Mesa 的 EGL 配置仍不满足 virglrenderer 的
core-profile 请求，因此结论仍为 no-go（原因不同）。

## 边界

- 未修改默认 extlinux、未 `saveenv`、未写 p2/p3；
- 板子当前停留在 origin 内核的 RockOS，GPU 栈加载中；正常重启会回到
  自定义 `6.6.87` 内核（GPU 模块不匹配，不会加载）；
- 未在 RockOS 上运行 Firefox；`glxinfo` 在 greeter 显示上未找到 RGB GLX
  visual，`xeglinfo` 创建上下文时 segfault——GLX/EGL 客户端路径尚未证明，
  glamor 加速是本次唯一完整的渲染证据。

## 原始证据（未跟踪）

`target/megrez-powervr-reference-20261021/`：

- `dmesg-pvr.txt`
- `Xorg-glamor-lines.txt`、`Xorg-glamor-context.txt`
- `qemu-virgl-smoke.txt`

## 许可

厂商 `img-volcanic` 源码为 **Dual MIT/GPLv2**（`pvr_drm.c` 等文件头），
MIT 分支可与 MPL-2.0 的 Asterinas 组合；移植计划见
[2026-10-21 Megrez PowerVR 原生加速移植计划](../../superpowers/plans/2026-10-21-megrez-powervr-native-acceleration.md)。
