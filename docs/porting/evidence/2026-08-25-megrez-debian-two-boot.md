# Megrez Asterinas Debian 双启动持久性证据

日期：2026-08-25

内核提交：`b48cfeea3c8d04c4a95812b8228ccee04dbffe2b`

## 结论

Megrez 已连续两次通过 Asterinas Stage1 从 eMMC 分区 2 启动同一个 Debian
trixie RISC-V 根文件系统。第一次启动写入并同步随机 nonce；Asterinas 冷重启
后，第二次启动读回精确相同的 nonce，并成功创建第二次启动 probe。

因此当前证据已经覆盖：

- Asterinas Sv39、4 hart、OSTD 和 MMC；
- ext2 标签发现及 `/dev/mmcblk0p2` 挂载；
- `/dev`、proc、sysfs、`/run`、`/tmp` 和 chroot 交接；
- Debian 动态链接 `/bin/bash` 执行；
- Debian 版本及关键包身份；
- Asterinas 冷重启后的 ext2 写入持久性。

## 产物身份

| 产物 | SHA-256 |
|---|---|
| Asterinas Sv39 Image | `4ec8b5d1a0de23e1adc1d6ae55dabbe00a44e7d37ff0221b345e28bcdad4415b` |
| Stage1 initramfs | `6c5236fed64db7e14cdbe9a32a60bac64d7225781ff551e8fade2e7feb6b7a7f` |
| Stage1 static `/init` | `ce7cfc5cfd91ae9a1cdfa7bd045fa24044d6331e5a9e2997f6efca3f96ec735f` |
| Debian ext2 image | `060f613281f2e77fa2232f31322213a310f48b5b18df2991ade9eb2fca7bebae` |

U-Boot 在两次启动前都重新加载文件；Image CRC32 都是 `f62270d9`，Stage1
大小都是 563200 bytes，DTB 大小都是 154800 bytes。两次 bootargs 都只对
分区 2 开启写权限：

```text
console=ttyS0 loglevel=info init=/init asterinas.mmc_write_partition2 asterinas.reboot_after=600
```

## 第一次启动

Stage1 打印：

```text
__DEBIAN_ROOTFS_SHELL_READY__
asterinas-debian#
```

Debian 用户态返回：

```text
riscv64
13.6
ext2/ext3
base-files     13.8+deb13u6
bash           5.2.37-2+b9
coreutils      9.7-3
libc6          2.41-12+deb13u3
util-linux     2.41-5
```

随机 nonce 写入 `/var/lib/asterinas-debian-m1/persist` 并执行 `sync`。为避免
在提交记录中公开 nonce，本页只记录文件摘要：

```text
6553ee6553035d09821a6e77b6cca8a95462dc0f135ad83df4c7f756e23e91f2
__PERSIST1_OK__
```

最小根文件系统没有安装 `reboot` 命令。Bash PID 1 随后正常 `exit`；内核按
既有契约报告 init 终止，并由已启用的 fatal-abort restart policy 调用 SBI
emergency restart。串口立即出现新的 DDR、OpenSBI 和 U-Boot 周期。

## 第二次启动

相同产物再次进入 `__DEBIAN_ROOTFS_SHELL_READY__`。命令先把持久化文件与
第一次随机值做精确比较，再重新计算摘要；摘要仍为：

```text
6553ee6553035d09821a6e77b6cca8a95462dc0f135ad83df4c7f756e23e91f2
```

随后写入并同步第二次启动 probe：

```text
boot2-probe-created
__PERSIST2_OK__
```

两轮没有 `DEBIAN_ROOTFS_FAIL`、MMC I/O error 或未恢复的 kernel panic。

## 原始证据与边界

本地串口记录：
`target-ubuntu/megrez-m2b/installer-authorized-write.log`

在 `__PERSIST2_OK__` 后取得的日志 SHA-256：
`8d14c59e663fbc32ecfa92241a83b5f137ed89b0a36690af73496fddbc30ceab`

该日志包含串口回显中的随机值，因此只保留在本地忽略目录，不提交原文。

这项证据证明的是可交互 Debian shell 和双启动持久根。它不证明 systemd、
网络、包管理器在线操作、USB/xHCI、图形显示或桌面环境。下一步应先补齐最小
PID 1/关机工具和基础系统启动，再扩展网络与桌面。
