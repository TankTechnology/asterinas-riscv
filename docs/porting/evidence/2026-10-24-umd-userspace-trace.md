# UMD 用户态诊断（P5b-18，2026-10-24）

## 结论

- 用 LD_PRELOAD shim 抓到 UMD 在 unload/失败前的最后调用：
  `PVRDRICreateScreen` 报 **`Unsupported device node type`**；
- 直接原因：guest 里 libdrm 的 `drmGetNodeTypeFromFd()` 对
  `/dev/dri/card0` 和 `/dev/dri/renderD128` 都返回 **-1**；
- 再往下：libdrm 走的是
  `/sys/dev/char/<major>:<minor>/device/drm`，而 Asterinas 的
  `/sys/dev/char/226:0/device` → `/sys/devices/platform/pvr`，
  **`/sys/devices/platform/pvr/drm` 不存在**，所以节点类型探测失败；
- 这不是某个 ioctl 的返回值问题，而是 **sysfs 拓扑缺一个
  `<device>/drm` 目录**；补上它应能让 `PVRDRICreateScreen` 接受节点，
  从而发出第一笔 `PVRSRV_BRIDGE_CONNECT`。

## shim

源码与产物（均未入库，在 `target/` 下）：

- `target/drmtrace.c`
- `target/libdrmtrace.so`（riscv64，12,896 B，交叉编译
  `riscv64-linux-gnu-gcc-13 -shared -fPIC`）

拦截并记录到 `/tmp/drm-trace.log`：

- `drmGetDevices2` / `drmGetDevice2`（含 bustype、available_nodes、
  节点路径）
- `drmGetVersion` / `drmGetCap`
- `open` / `open64` / `openat` / `openat64`（只记 `/dev/dri/*`）
- `ioctl`（只记 DRM type `'d'` 或已记录的 DRM fd，前 64 条）

实现注意点（本轮踩到的两个坑）：

1. 早期 `dlsym(RTLD_NEXT, "ioctl"/"open")` 可能返回 NULL；直接调用会
   跳到地址 0（内核日志：page fault address 0x0、required_perms EXEC、
   SIGSEGV）。现在所有 wrapper 在 dlsym 失败时回退到 raw syscall；
2. Python 的 `os.open` 走 `open64`，只拦 `open` 抓不到；已加
   `open64/openat64`。

DDK 调试：`libsrv_um.so` 里有 `PVRDebugLevel`、`powervr.ini`、
`/etc/powervr.ini` 字符串；guest 的 `/etc/powervr.ini` 写入：

```ini
[default]
PVRDebugLevel=0x1ff
```

DDK 随后打出 `PVR:(Error): PVRDRICreateScreen: ...`，即本条关键错误。

## 板级 trace

### eglconfigs（`EGL_PLATFORM=gbm`，baseline Mesa22 环境）

```text
TRACE open64 /dev/dri/card0 flags=0x80002 -> 3
TRACE ioctl#1 fd=3 req=0xc010640c -> 0 errno=2
TRACE drmGetCap fd=3 cap=0x5 -> r=0 val=3 errno=2
( 492) PVR:(Error): PVRDRICreateScreen: Unsupported device node type [ :257 ]
```

`0xc010640c` 是 `DRM_IOCTL_GET_UNIQUE`（nr 0x0c，16 B）；随后查
`DRM_CAP_PRIME`（=3），再进入 `PVRDRICreateScreen` 并拒绝节点。
（成功 ioctl 后面的 errno 是调用方遗留值，不是该调用的错误。）

### 直接 gbm 探针（`/dev/dri/renderD128`）

```text
NODE /dev/dri/renderD128 FD 3
VER 24 2 6643903
BEFORE_GBM
AFTER_GBM True
BEFORE_DISP
AFTER_DISP False

TRACE open64 /dev/dri/renderD128 flags=0x80002 -> 3
TRACE ioctl#1 fd=3 req=0xc0406400 -> 0 errno=0
TRACE ioctl#2 fd=3 req=0xc0406400 -> 0 errno=0
TRACE ioctl#3 fd=3 req=0xc010640c -> 0 errno=2
TRACE drmGetCap fd=3 cap=0x5 -> r=0 val=3 errno=2
( 588) PVR:(Error): PVRDRICreateScreen: Unsupported device node type [ :257 ]
```

`0xc0406400` 是 `DRM_IOCTL_VERSION`（libdrm 两次调用：先取长度再取
字符串）；两个节点都走到同一个 DDK 错误。

### 节点类型探测

guest 内直接调用 libdrm：

```text
/dev/dri/card0 -1
/dev/dri/renderD128 -1
```

即 `drmGetNodeTypeFromFd` 对两个节点都失败，不是“只支持 render 节点”
或“只支持 card 节点”的策略问题。

### 缺失的 sysfs 路径

```text
$ ls -l /sys/dev/char/226:0/device
lrwxrwxrwx ... /sys/dev/char/226:0/device -> ../../../devices/platform/pvr
$ ls /sys/dev/char/226:0/device/drm
ls: cannot access '/sys/dev/char/226:0/device/drm': No such file or directory
```

`drmGetNodeTypeFromFd` 在较新的 libdrm 里会扫描
`/sys/dev/char/<maj>:<min>/device/drm`，用条目名（`card*`/`renderD*`/
`controlD*`）和 `dev` 文件匹配节点的 rdev；目录不存在时返回 -1，
`PVRDRICreateScreen` 就报 “Unsupported device node type”。

注意：`/sys/dev/char/226:0` 本身的 `realpath` 是正常的
（`/sys/class/drm/card0`），所以旧式“读 symlink 名”的实现能过；
这里卡住的是“扫描 `device/drm` 目录”的实现。

## 判定

- UMD 在 unload 前的最后一步：`PVRDRICreateScreen` 的节点类型检查；
- 检查失败的原因：`<device>/drm` 目录缺失，`drmGetNodeTypeFromFd`
  返回 -1；
- 这解释了为什么此前 card0/version/caps/uevent 全部对齐仍无 CONNECT：
  失败发生在 `PVRDRICreateScreen`，早于任何 `PVRSRV_BRIDGE_CONNECT`。

## 下一条最小改动（下轮实施，内核 sysfs）

在 `/sys/devices/platform/pvr` 下补 `drm` 目录，并放入两个条目：

```text
/sys/devices/platform/pvr/drm/card0       -> ../../../class/drm/card0
/sys/devices/platform/pvr/drm/renderD128  -> ../../../class/drm/renderD128
```

这样：

- `/sys/dev/char/226:0/device/drm` 存在；
- 目录项名是 `card0` / `renderD128`，指向的 class 节点有 `dev`
  （`226:0` / `226:128`）；
- libdrm 的 `drmGetNodeTypeFromFd` 应返回 `DRM_NODE_PRIMARY(0)` /
  `DRM_NODE_RENDER(2)`；
- `PVRDRICreateScreen` 通过后，预期出现第一条
  `ASTERINAS_POWERVR_BRIDGE` marker（CONNECT）。

用 symlink 是最小改动；若 libdrm 对条目类型有额外要求，再改成完整
节点（含 `dev`/`uevent`）。

## 本轮对 guest rootfs 的改动（非 initramfs）

- `/opt/pvr/bin/libdrmtrace.so`（诊断 shim）；
- `/etc/powervr.ini`（`[default] PVRDebugLevel=0x1ff`）。

两者都在 guest 的 p2 上，可随时删除；未改 Asterinas 内核或 initramfs。

## 边界

- 未改内核/initramfs；所有板级/guest 命令带 `timeout`；
- 串口输出限量；完整串口：
  `target/board-p5b18-20261024/board_p5b18.serial.log`（未跟踪）；
- EGL 未通过，不写成通过。
