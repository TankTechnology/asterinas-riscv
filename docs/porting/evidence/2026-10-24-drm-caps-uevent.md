# PowerVR DRM caps 与 uevent OF 字段（P5b-17，2026-10-24）

## 结论

- PowerVR 后端现在回答 `pvr_dri.so` 实际探测的四个 cap：
  `TIMESTAMP_MONOTONIC(0x06)`=1、`ADDFB2_MODIFIERS(0x10)`=0、
  `SYNCOBJ(0x13)`=1、`SYNCOBJ_TIMELINE(0x14)`=0；
  其它后端保持原行为；
- `/sys/devices/platform/pvr/uevent` 现在带 GPU 的设备树身份：
  `OF_NAME=gpu`、`OF_FULLNAME=/soc/gpu@51400000`、
  `OF_COMPATIBLE_0=img,gpu`、`OF_COMPATIBLE_N=1`、
  `MODALIAS=of:NgpuT(null)Cimg,gpu`；
- ktest `gpu_drm_caps_and_uevent` 通过；板级 uevent 输出与代码一致；
- **但 CONNECT 仍未出现**：card0 与 renderD128 两条探针都停在
  `gbm_create_device=True`、`eglGetPlatformDisplay=NULL`，
  整份串口 `ASTERINAS_POWERVR_BRIDGE` 仍为 0 条。
- 至此 P5b-11..17 已把 card0、DRM version、caps、uevent 四个可对齐点
  全部对齐，CONNECT 仍未发生；失败点在 UMD/Mesa 路径的更深处，
  需要用户态 instrumentation，而不是继续猜内核字段。

## 实现（commit `ee3fc5ae9`）

### caps（`kernel/src/device/dri.rs`）

- 新增 `drm_cap_value(driver_name, capability)`：
  - 公共：`DUMB_BUFFER`=1、`DUMB_PREFERRED_DEPTH`=24、
    `DUMB_PREFER_SHADOW`=0、`PRIME`=IMPORT|EXPORT；
  - 仅 `pvr`：`TIMESTAMP_MONOTONIC`=1、`ADDFB2_MODIFIERS`=0、
    `SYNCOBJ`=1、`SYNCOBJ_TIMELINE`=0；
  - 未知 cap 仍返回 `-EINVAL`（DRM uapi 允许）。
- `GetCap` 处理改为调用该函数；virtio 后端行为不变。

### uevent（`kernel/src/device/sysfs.rs`）

`FirmwarePvrPlatformNode` 的 `uevent` 在 `DRIVER=/DEVTYPE=` 之后新增：

```text
OF_NAME=gpu
OF_FULLNAME=/soc/gpu@51400000
OF_COMPATIBLE_0=img,gpu
OF_COMPATIBLE_N=1
MODALIAS=of:NgpuT(null)Cimg,gpu
```

任务文本里的 `OF_NAME=pvr`/`/soc/pvr@...` 是占位；这里按实际
device tree 节点（`/soc/gpu@51400000`，compatible `img,gpu`）填写。

### ktest

`gpu_drm_caps_and_uevent`（sysfs.rs）断言四个 cap 值、PRIME=3、
非 pvr 后端对 `SYNCOBJ` 仍报错，以及 uevent 含上述全部键。
原有的 `gpu_drm_sysfs_nodes` 里 64 字节 uevent 缓冲已扩到 256，
因为新 uevent 超过 64 字节。

| 测试 | 结果 |
|---|---|
| `gpu_drm_caps_and_uevent`（新增） | `1 passed; 0 failed; 361 filtered out` |
| `gpu_drm_sysfs_nodes` | `1 passed; 0 failed; 361 filtered out` |
| `gpu_drm_driver_name` | `1 passed; 0 failed; 361 filtered out` |
| `gpu_drm_version_matches_vendor` | `1 passed; 0 failed; 361 filtered out` |

原始串口：`target/kt-p5b17-*.serial.log`（未跟踪）。

## 镜像与板级

| 项 | 值 |
|---|---|
| 内核 | `/home/debian/kernel-pvr-caps.Image`，16,745,296 B，CRC32 `916b5422`，SHA-256 `5fe8b138fe1b15febc14e22dfa68028011a773f43a45ab5a67f2e345195649f9` |
| initramfs | 原 `stage1-pvr-batch.cpio`（CRC32 `c8f825a7`），未修改 |
| DTB | `megrez_dtb-465cb129333cc334.dtb`（CRC32 `40ed4c65`），未修改 |
| 串口 | `target/board-p5b17-20261024/board_p5b17.serial.log` |

两条探针（baseline 环境 + `MESA_LOADER_DRIVER_OVERRIDE=pvr`）：

```text
NODE /dev/dri/card0 FD 3
VER 24 2 6643903
BEFORE_GBM
AFTER_GBM True
BEFORE_DISP
AFTER_DISP False

NODE /dev/dri/renderD128 FD 3
VER 24 2 6643903
BEFORE_GBM
AFTER_GBM True
BEFORE_DISP
AFTER_DISP False
```

`eglInitialize` 未执行（display 为 NULL）。board uevent：

```text
DRIVER=pvr
DEVTYPE=platform
OF_NAME=gpu
OF_FULLNAME=/soc/gpu@51400000
OF_COMPATIBLE_0=img,gpu
OF_COMPATIBLE_N=1
MODALIAS=of:NgpuT(null)Cimg,gpu
```

bridge marker：

```text
ASTERINAS_POWERVR_BRIDGE 出现次数：0
```

## 追加观察（为下一步）

`pvr_dri.so` 导出 **36 个** `__driDriverGetExtensions_*` 符号，其中
包括：

```text
__driDriverGetExtensions_pvr
__driDriverGetExtensions_es_drm
__driDriverGetExtensions_kms_swrast
```

所以 Mesa loader 按驱动名 `pvr` 查找的符号是存在的；它仍然把
`pvr_dri.so` dlclose，说明拒绝发生在扩展列表之后的
screen/extension 校验或 DDK 初始化，而不是符号缺失。

## 判定

- caps 与 uevent 的 vendor 对齐已完成并验证；
- 它们不是 CONNECT 的门槛：两条节点探针都没有触发任何 bridge ioctl；
- 失败点仍在 UMD/Mesa 用户态，位于 `__driDriverGetExtensions_pvr`
  返回之后、第一次 `PVRSRV_BRIDGE_CONNECT` 之前。

## 下一步（下轮候选，建议先做诊断）

1. **LD_PRELOAD 探针**：拦 `drmGetDevices2`、`open`/`openat`、
   `ioctl`，记录 libsrv 在 unload 前最后调用的 DRM 操作；这能确定
   它是否打开过我们的节点、是否在某个 ioctl 上失败；
2. **Mesa loader 调试**：`MESA_DEBUG=1` 之外，检查
   `__driDriverGetExtensions_pvr` 返回的扩展列表是否缺少 Mesa 22.3.5
   要求的项（对比 `kms_swrast` 的扩展）；
3. **DDK 调试输出**：`PVRSRVDebugPrintf` 由 UMD 内部级别控制；
   若有环境变量/ini 能提高级别，可直接看到 DDK 的失败原因；
4. 仍不建议在没有 UMD 侧证据前继续改内核字段。

## 边界

- 未改 initramfs；ktest 均带 `timeout 180`；板级命令带 `timeout`；
- 未执行 apt，未修改 RockOS 系统文件；
- EGL 未通过，不写成通过。
