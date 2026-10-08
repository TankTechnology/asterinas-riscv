# Megrez Asterinas Debian 分区安装证据

日期：2026-08-25

内核提交：`b48cfeea3c8d04c4a95812b8228ccee04dbffe2b`

目标设备：Megrez eMMC `/dev/mmcblk0p2`

## 结论

Megrez 已在 **Asterinas 内核及其 PID 1 安装器**中，把冻结的 Debian
trixie RISC-V ext2 镜像安装到 eMMC 分区 2，并从同一分区重新读取完整
1 GiB 后获得预期 SHA-256：

```text
DEBIAN_INSTALL_PASS sha256=060f613281f2e77fa2232f31322213a310f48b5b18df2991ade9eb2fca7bebae bytes=1073741824
```

Linux/RockOS 没有参与分区写入或最终验证。它只用于事先把不可变的 Asterinas
Image、DTB 和 initramfs 文件放到 U-Boot 可读取的启动文件系统。

## 产物身份

| 产物 | 身份 |
|---|---|
| Asterinas Image | `target-ubuntu/megrez-m2b/asterinas-sv39-b48cfeea3.booti` |
| Image SHA-256 | `4ec8b5d1a0de23e1adc1d6ae55dabbe00a44e7d37ff0221b345e28bcdad4415b` |
| Image U-Boot CRC32 | `f62270d9` |
| 安装 initramfs | `/asterinas-debian-installer-eb0e27af.cpio` |
| initramfs SHA-256 | `eb0e27af7ea83ae4212fe69e0c99ab15d05bb1f14c1a6e9c099e361b5eb587a0` |
| Debian 镜像 SHA-256 | `060f613281f2e77fa2232f31322213a310f48b5b18df2991ade9eb2fca7bebae` |
| Debian 镜像大小 | `1073741824` bytes |

两次启动都使用修正后的 compiled-Sv39 单模式内核，均越过
`Enter riscv_boot`、4 hart、OSTD、组件初始化和 MMC 注册。写权限只通过精确
bootarg `asterinas.mmc_write_partition2` 对分区 2 开启。

## 两轮安装

第一轮使用 900 秒软件重启保护：

- 32 个 32 MiB 分块中，27 块哈希已匹配并跳过；
- `0012`、`0016`、`0020`、`0024`、`0028` 五块实际写入；
- 每一块写入后都从 eMMC 读回并验证为预期 SHA-256；
- 实际写入吞吐约 1.2--1.3 MiB/s；
- 900 秒保护计时器在最终 1 GiB 摘要完成前触发冷重启，因此第一轮不宣称
  完成安装。

第二轮使用 1800 秒软件重启保护：

- 32/32 分块全部重新读取并得到 `DEBIAN_INSTALL_CHUNK_SKIP`；
- 本轮没有任何分块写入；
- 完整读取 `/dev/mmcblk0p2` 的前 1 GiB 后得到上述
  `DEBIAN_INSTALL_PASS`；
- 串口中没有 `DEBIAN_INSTALL_FAIL`、panic 或 MMC I/O error。

这证明五个修复分块在重启后仍然持久，并证明目标分区的完整内容与冻结镜像
逐字节身份一致；它尚不证明 Debian 用户态已经通过 Stage1 启动。

## 原始证据

本地串口记录：
`target-ubuntu/megrez-m2b/installer-authorized-write.log`

记录 SHA-256：
`f926cc0e35c71453b913c0f7c370f1620fbe3ac8cb0e0ff0ff94aaed8a132fe7`

该日志是本地原始证据，不随仓库提交；本页只提交可审查的产物身份、观察结果
和证据边界。

## 下一门禁

下一步使用 Asterinas Stage1 initramfs 只读发现标签为
`ASTER_DEBIANROOT` 的 ext2 分区，挂载并进入 Debian `/bin/bash`，验证 Debian
版本、包身份和跨两次启动的持久化 nonce。完成该门禁前，不宣称“Debian 已能
在 Megrez 上运行”。
