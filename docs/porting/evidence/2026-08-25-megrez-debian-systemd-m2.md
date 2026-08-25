# Megrez Asterinas Debian systemd M2 证据

日期：2026-08-25

启动候选提交：`6576d661f`

## 结论

Milk-V Megrez 已通过 Asterinas 把冻结的 Debian Trixie 13.6 `riscv64`
systemd M2 根文件系统写入 eMMC 分区 2，并连续两次从同一个持久根启动
Debian systemd 257.13。第一次启动由 Debian 用户态执行强制重启，随后出现新
的 DDR、OpenSBI 和 U-Boot 周期；第二次启动读到持久化 boot count，并输出：

```text
DEBIAN_SYSTEMD_M2_READY boot=2 arch=riscv64 release=13.6
DEBIAN_SYSTEMD_M2_PASS boot=2
```

这次真机结果证明了 Asterinas Sv39、4 hart、MMC、ext2、Stage1、动态链接
Debian 用户态、systemd PID 1、持久写入和用户态重启的连续链路。RockOS 只
负责把不可变启动文件放入 `/boot`；Debian 根分区的写入和验证全部由
Asterinas 完成，没有绕过 Asterinas 使用 Linux 写根分区。

## 冻结产物

| 产物 | Bytes | SHA-256 / CRC32 |
| --- | ---: | --- |
| Asterinas Sv39 Image | 13968656 | SHA-256 `e8a3b155876b0b6cfee59c09ebb0401a50d43f3cbecb63c21fdcd53e7c5ea66c`; CRC32 `4a3b8d89` |
| systemd M2 Stage1 initramfs | 563712 | SHA-256 `ef6d7555b5d48abc0f89345e51aef414efb040754682d78b1fb86febd02eec0d`; CRC32 `db779652` |
| signed Debian systemd M2 ext2 | 1073741824 | SHA-256 `9429f1632083ad2387de9699813f2feba4f63143d0710d14f3f0d7429c535463` |
| Asterinas M2 installer initramfs | 131475968 | SHA-256 `be99b2b287ee91a256440ed3659454da3bc63fce4917a566b6cd213ab858d5fb`; CRC32 `0ad03a45` |
| Megrez DTB | 154800 | CRC32 `4afcb20e` |

DTB 来自板上
`/boot/dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb`。
每次 `booti` 前，U-Boot 都重新加载并核对 Image、Stage1 和 DTB 的大小与
CRC32；没有复用上一次启动残留在 RAM 中的内容。

## Asterinas 安装根文件系统

安装启动只开放精确的 eMMC 分区 2 写能力，并绑定预期完整镜像 SHA-256：

```text
asterinas.mmc_write_partition2
asterinas.debian_install_sha256=9429f1632083ad2387de9699813f2feba4f63143d0710d14f3f0d7429c535463
```

安装器按 32 MiB 分块比较。11 个不同分块被写入并回读验证，21 个相同分块
被跳过；最后 Asterinas 读取完整 1 GiB 分区并输出：

```text
DEBIAN_INSTALL_PASS sha256=9429f1632083ad2387de9699813f2feba4f63143d0710d14f3f0d7429c535463 bytes=1073741824
```

## 只读诊断尝试

第一次 systemd 诊断启动刻意没有提供分区写能力。Asterinas 把 MMC 登记为
只读，Stage1 在为 systemd 创建 API 目录时稳定失败：

```text
DEBIAN_ROOTFS_FAIL reason=api-directories
```

这说明签名根不是只读演示镜像：systemd 启动必须持久写入目录和运行状态。
随后两次正式启动只增加已有的精确 partition-2 能力，没有放宽为整盘写入。

## systemd 双启动

两次正式启动使用相同参数：

```text
console=ttyS0 loglevel=info init=/init \
asterinas.mmc_write_partition2 asterinas.reboot_after=600 \
-- --root-init=systemd
```

第一次启动中，Asterinas 检测 compiled Sv39 和 4 个 hart，MMC 显示
`partition-2 writes armed`，Debian systemd 报告版本
`257.13-1~deb13u1`，并到达 `basic.target`。证据服务输出：

```text
DEBIAN_SYSTEMD_M2_READY boot=1 arch=riscv64 release=13.6
```

约在 Asterinas 启动后 40 秒，板卡进入新的 DDR/OpenSBI/U-Boot 周期。这远早
于 600 秒保护重启，且符合证据服务在 boot 1 调用 Debian `/sbin/reboot -f`
的冻结契约，因此不是等待保护定时器得到的结果。

第二次启动重新加载并核对三项启动产物，再次进入 systemd 257.13，在约
32 秒输出 boot 2 和 PASS。boot count 来自同一个 ext2 根上的
`/var/lib/asterinas-debian-m2`，所以 PASS 同时证明第一次启动的写入已跨
固件重启持久化。

## 已知兼容性缺口

两次启动都能到达基础 systemd 目标，但仍有可复现的 Linux 兼容性缺口：

- `/run/lock` 与 `/tmp` mount unit 报 `protocol` 失败；
- kmod 初始化不受支持，根中也没有 `/sbin/modprobe`；
- configfs、systemd-logind 和部分 sysusers 路径失败；
- `fs.nr_open`、kbrequest、cgroup BPF、libmount watch、若干 clone/syscall
  路径尚未实现；
- 串口可出现重复的 syscall 272 与 SCM_RIGHTS 资源警告。

这些问题没有阻止 `basic.target`、证据服务、持久 boot count、用户态重启或
第二次 PASS，但它们是后续基础兼容性工作的优先项。

## 原始证据与边界

本地原始串口：
`target-ubuntu/megrez-m2b/installer-authorized-write.log`。
冻结切片取自 byte offset 218381 到当时文件末尾，保存为本地忽略文件
`target-ubuntu/megrez-m2b/systemd-m2-board.log`：

| 字段 | 值 |
| --- | --- |
| Bytes | 126136 |
| SHA-256 | `d9b0a3e75f9b1d1e39d829abf69b65b3e7f18204a048662cc87ff0953d54244f` |
| OpenSBI / `Starting kernel ...` | 4 / 4（安装、只读诊断、boot 1、boot 2） |
| M2 READY boot 1 / boot 2 / PASS | 1 / 1 / 1 |
| M2 FAIL / kernel panic / Oops / MMC I/O error | 0 / 0 / 0 / 0 |

原始日志体积较大，只保留在本地忽略目录；提交的是可审计的身份、顺序和边界。

本结果不证明网络、在线 APT、USB/xHCI、键盘、DRM/HDMI、图形登录或桌面
环境。它证明的是这些体验功能所依赖的 Asterinas→持久 Debian→systemd
基础链路已经在 Megrez 真机成立。
