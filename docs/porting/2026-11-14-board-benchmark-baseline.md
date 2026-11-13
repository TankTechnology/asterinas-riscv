# 板上基准记录：启动、包管理、A/L 比率（2026-11-14）

所有数字取自 Milk-V Megrez（EIC7700，eMMC 根分区，`/tmp` 为 tmpfs），
Linux 列为厂商 RockOS 内核 `6.6.87-win2030-origin`，同一 rootfs 只读挂载。

## 1. 启动到可用 shell

| | 样本 | 中位数 | p95 | 口径 |
|---|---|---|---|---|
| Asterinas | 6 | **4.69 s** | 4.74 s | `Starting kernel` → `ASTERINAS_DEBUG_CONSOLE_READY`（root shell 可用） |
| Linux 6.6.87 | 2 | **8.56 s** | — | kernel 启动 → initramfs shell 提示符（`rdinit=/bin/sh`，同一 rootfs 只需挂载） |

Asterinas 快约 1.8×。两列口径都是「内核开始执行 → 出现可输入的命令提示符」，
Linux 侧未跑完整 systemd。

systemd 完整启动（Asterinas，供参考，n=33 次启动）：

| | 中位数 | p95 |
|---|---|---|
| kernel | 2.01 s | 2.03 s |
| userspace | 39.98 s | 41.48 s |

## 2. 包管理

`apt-get update`（Asterinas，包列表写到 `/tmp` 以避开 eMMC 写，n=5）：
20.3 / 13.7 / 13.9 / 14.7 / 15.2 s → **中位数 14.7 s，p95 20.3 s**。该项由镜像站网络主导，
不用于对比内核。

`dpkg -i` / `apt install`：**BLOCKED**。`/var/lib/apt/extended_states` 与
`/var/lib/dpkg/updates/*` 在盘上指向已释放 inode，读取返回 ESTALE（重启不清除），
来自一次被 eMMC 写错误中断的 `apt install fio`。需 repair initramfs + e2fsck 修复，
本轮按约束未执行。

## 3. Asterinas / Linux 比率（同板、同 rootfs、同静态 musl 基准）

| 指标 | Linux | Asterinas | 比率 |
|---|---|---|---|
| `stat`（缓存路径） | 2.33 µs | 18.66 µs | 8.0× |
| `fstat`（fd） | 0.65 µs | 3.64 µs | 5.6× |
| `getpid` | 0.19 µs | 3.25 µs | 17.0× |
| `open`+`close` | 4.44 µs | 29.42 µs | 6.6× |
| pipe 往返 | 1.68 µs | 8.74 µs | 5.2× |
| fork+exec+wait | 2.73 ms | 3.34 ms | 1.22× |
| 4 KiB 随机写（tmpfs） | 293k IOPS | 58k IOPS | 5.1× |
| 4 KiB 随机读（tmpfs） | 154k IOPS | 33k IOPS | 4.8× |
| 顺序写 | 1350 MiB/s | 369 MiB/s | 3.7× |
| 顺序读 | 644 MiB/s | 193 MiB/s | 3.3× |
| 创建+fsync+关闭 | 15.5 µs | 85.1 µs | 5.5× |
| unlink | 8.8 µs | 48.6 µs | 5.5× |
| memcpy（用户态） | 1564 MiB/s | 244 MiB/s | 6.4× |

门禁基线：`tools/riscv/perf/board_al_baseline.json`（冻结 Linux 列）+
`tools/riscv/perf/al_gate.py`（重测 Asterinas 列并判定）：
`stat` ≤ 9.0×、4 KiB 随机 I/O ≤ 6.0×、fork+exec ≤ 1.4×，三项当前全部通过。

## 4. 复现命令

```bash
# Asterinas 侧（板上）
/tmp/board_l1                 # syscall / fork+exec / FS
/tmp/io4k /tmp/io4k.bin       # 4 KiB 随机读写在 64 MiB tmpfs 文件上
# 门禁
tools/riscv/perf/al_gate.py   # 走 SSH 重测并判定

# Linux 侧（U-Boot 手工启动厂商内核）
ext4load mmc 1:1 0x80200000 /vmlinuz-6.6.87-win2030-origin
ext4load mmc 1:1 0x88300000 /initrd.img-6.6.87-win2030
setenv initrd_size ${filesize}
ext4load mmc 1:1 0x88000000 /dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb
setenv bootargs console=ttyS0,115200 root=/dev/mmcblk1p2 ro rootwait rdinit=/bin/sh selinux=0
booti 0x80200000 0x88300000:${initrd_size} 0x88000000
# 进入 shell 后：mount devtmpfs/rootfs（ro）/tmpfs，chroot 进 rootfs，配置 eth0 后取回基准二进制
```

注意：Linux 下 eMMC 是 `mmcblk1`（Asterinas 里是 `mmcblk0`），写错设备名会导致找不到根。

## 5. 数据来龙去脉

- 2026-11-14 凌晨，同一块板、同一天内连续测量；Asterinas 侧每次测量前 `sync`。
- 跨启动对比不可信（同内核两次启动每层路径成本 2.35 vs 4.05 µs），
  因此凡是能同启动交替 A/B 的项目（权限顺序、路径栈缓冲）都用交替法，
  其余（A/L 对比）用「同一天内、同一批二进制」的方式，并在此标注口径。
- Linux 侧根分区只读挂载，基准写入 `/tmp`（tmpfs），不写 eMMC。
