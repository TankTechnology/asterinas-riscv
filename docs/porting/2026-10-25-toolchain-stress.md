# 编译器闭包与 30 轮编译压力复测（build-essential 独立 profile）

日期：2026-10-25　HEAD：`f7534bf23`（改动前）
内核：`target/prime/kernels/x86.elf`，sha256 前缀 `9e8ee5dd544fa03a`
镜像：`target/debian-x86-toolchain3/rootfs/debian-root.ext2`
（**2.0 GiB**，**409 包**，独立 profile `desktop-drm-toolchain`）

## 结论摘要

- **编译器闭包已补**：`gcc (Debian 14.2.0-19) 14.2.0`、`GNU Make 4.4.1`
  在 guest 内可用；一次性编译测试 `gcc -O2 /tmp/t.c && /tmp/t` 输出 **`ok`**。
- **30 轮编译压力全部成功**：`gcc_ok=30/30`，`gcc` 失败 **0/30**；
  `python3` 512 MiB 内存压力仍 **0/30** 失败。
- **内存仍稳定**：`MemAvailable` 3,970,264 → 3,911,744 kB，
  **Δ = -58,520 kB（-1.474%）**，低于 5% 阈值；`dmesg` 中
  `Uncaught panic` / `oom-killer` / `Out of memory` / `allocation failed`
  **全部为 0**。
- 包数 **357 → 409（+52）**；apt 取包 **314 条 / 229.0 MB**；
  镜像 **2.0 GiB**（为容纳工具链把该 profile 的根从 1 GiB 调到 2 GiB）。

## 1. 独立 profile（不改默认剖面）

`tools/debian/rootfs/profiles.py` 新增：

```python
_PROFILES["desktop-drm-toolchain"] = RootfsProfile(
    name="desktop-drm-toolchain",
    schema_version=9,
    root_label="ASTER_DRM_TOOLCH",
    root_uuid="9d3a7c52-6f1e-5b84-a2c7-4e8d1f6b0c93",
    requested_packages=_PROFILES["desktop-drm"].requested_packages
    + ("build-essential",),
    identity_packages=_PROFILES["desktop-drm"].identity_packages
    + ("build-essential",),
    root_size_bytes=2 * 1024 * 1024 * 1024,
)
```

`desktop-drm`、各里程碑 profile 的包集**未改动**；只新增这个用于压测的
独立身份。`build_rootfs.sh` 只增加该 profile 的白名单、输出目录映射与
desktop 配置/串口登录面复用。

| 项 | 值 |
|---|---|
| 包数 | 357 → **409（+52）** |
| 下载体积 | apt `Get:` **314 条 / 229.0 MB** |
| 镜像 | **2.0 GiB**（2 GiB ext2，409 包） |
| 新增直接包 | `build-essential`（拉入 gcc/g++/make/libc6-dev/binutils 等） |

## 2. 构建期遇到并修复的两个问题

1. **manifest 阶段 SIGPIPE（两次 `BUILD_EXIT=141`）**：
   `write_rootfs_manifest()` 用
   `mke2fs -V 2>&1 | head -n 1` 取版本，`set -o pipefail` 下 `head`
   提前关闭管道会偶发让 `mke2fs` 收到 SIGPIPE，脚本以 141 退出。
   改为 `sed -n '1p'`（读完全部输入），同时把 `debootstrap`/QEMU 的
   版本探测一并改掉；第三次构建 `BUILD_EXIT=0`。
2. **stage1 不认识新卷标**：镜像按 label 找根，`stage1_init.c` 的白名单
   没有 `ASTER_DRM_TOOLCH`，启动时 `probe-complete result=no-match` →
   `DEBIAN_ROOTFS_FAIL reason=root-discovery-timeout`。已在白名单补该标签，
   并重建 `target/debian-x86/stage1/initramfs.cpio`（1.4 MB）。

## 3. guest 内编译器验证（原始输出）

```
toolchain gcc=/usr/bin/gcc make=/usr/bin/make
toolchain_versions gcc=gcc (Debian 14.2.0-19) 14.2.0 make=GNU Make 4.4.1
compiler_test rc=0 out=ok
```

对应任务要求的命令：

```
command -v gcc make          -> /usr/bin/gcc, /usr/bin/make
gcc -O2 /tmp/t.c -o /tmp/t && /tmp/t   -> ok
```

## 4. 30 轮压测结果

每轮：python3 分配 512 MiB 并逐页写 + `gcc -O2` 编译最小 C 程序 + 采样
`MemAvailable` + sleep 10 s。脚本同时打印版本与一次性编译结果，并统计
`gcc_ok`。

| 指标 | 结果 |
|---|---|
| 编译成功轮次 | **30/30**（`gcc_ok=30/30`，`gcc=0/30` 失败） |
| python3 失败 | **0/30** |
| `MemAvailable` baseline | **3,970,264 kB** |
| t=10 / t=20 / t=30 采样 | 3,930,324 / 3,920,356 / 3,902,132 kB |
| 末次 `MemAvailable` | **3,911,744 kB** |
| Δ | **-58,520 kB（-1.474%）** |
| `Uncaught panic` / `oom-killer` / `Out of memory` / `allocation failed` | **0 / 0 / 0 / 0** |
| 实际墙钟 | ≈8 分钟（30 轮 × ~13 s；`t=Nmin` 是轮次编号） |

## 5. 判定

- **编译压力：通过**（成功 30/30，≥ 28/30 的验收线）；
- **内存/稳定性：稳定**（Δ -1.47%，无 panic/OOM/分配失败）；
- 门禁 `GATE_EXIT=2` 仍是因为自定义 checklist 不输出标准
  `DAILY_<域>=ok` 行（`DAILY_CLIENT ... done=True`、`DAILY_DONE=ok`），
  压测本身完整跑完。

## 6. 限制与后续

1. 每轮编译的是一个最小 C 文件，不是大型项目；若要更重的编译压力，
   可换成解包并编译真实源码包（时间会显著增加）。
2. 独立 profile 的根为 2 GiB；默认 `desktop-drm` 仍是 1 GiB/357 包，
   L1–L6 与 GTK 用例不受影响。
3. stage1 的新卷标已进源码；其他新 profile 若新增 label，需要同步加入
   `stage1_init.c`，否则会以 `root-discovery-timeout` 启动失败。

## 7. 证据

- `target/stress-toolchain2/console.txt`（串口全量）
- `target/stress-toolchain2/host.log`
- `target/debian-x86-toolchain3/build.log`（构建、包锁、下载体积）
- 归档：`docs/porting/evidence/2026-10-25-toolchain-stress/console-excerpt.txt`
