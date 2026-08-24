# NixOS on Asterinas RISC-V — 并行推进计划

> 2026-08-13 创建。与 asterinas-riscv (Xorg 图形栈) 并行推进。
> 注意: 上游 Asterinas NixOS 是 x86_64 独占, riscv64 属开创性工作。

## 目标

在 RISC-V QEMU 上跑起 Nix 工具链, 最终评估完整 NixOS 的可行性。
上游已验证 100+ NixOS 包 (systemd, Firefox, Xfce) 在 x86_64 上运行。

## 现状

- 内核: 245 syscall, procfs/sysfs/cgroupfs/tmpfs/ext2/virtio 全家齐备
- 用户态: 最小 initramfs (汇编 init), 已有 riscv64-linux-gnu-gcc 15.1 工具链
- Xorg 轨道已有 glibc 动态链接的 riscv64 用户态先例 (xserver 交叉编译)
- 上游参考: github.com/asterinas/asterinas/issues/2356 (x86_64 PoC)

## 里程碑

### M1: 静态 busybox + 核心 syscall 冒烟测试 (1-2 天)
- 交叉编译 busybox (riscv64, 静态)
- 接入现有 initramfs 构建管线
- 在 QEMU 中跑通: sh, ls, cat, mount, ps
- 输出: 一份"syscall 缺口清单"(哪些调用导致崩溃/ENOSYS)

### M2: 动态 glibc 用户态 (2-3 天)
- 构建 riscv64 glibc + 动态链接器 (工具链自带或构建)
- 跑通一个动态链接的 C 程序 (fork + exec + mmap 全路径)
- 重点验证: dlopen, TLS, AT_PHDR auxv 语义

### M3: Nix 包管理器跑起来 (3-5 天)
- 交叉编译 Nix (或 nix-portable 静态版)
- 依赖链: SQLite, libcurl, libarchive, openssl
- 冒烟测试: nix eval, 构建一个 hello derivation
- 需要: /nix/store 布局, unix socket, 用户命名空间评估

### M4: nix-daemon + store 操作 (3-5 天)
- nix-daemon 需要: AF_UNIX, SCM_RIGHTS, fork 模型
- nix build 完整 derivation
- 沙箱 (bubblewrap/seccomp) 评估 — 可先绕过

### M5: NixOS stage-2 可行性 (1-2 周)
- systemd 依赖清单评估 (cgroup v2, D-Bus, mount ns)
- 决定: 完整 NixOS vs 轻量 init (busybox-init + nix profile)
- 输出: go/no-go 报告

## 内核侧已知缺口 (M1 优先验证)

- statx, openat2, renameat2 (现代 coreutils 需要)
- mlock/munlock (小应用常调)
- System V shm (可 memfd 替代)
- /proc 节点完整性 (self, mounts, cpuinfo, meminfo)
- 用户命名空间 (Nix 沙箱可选)
- termios 边界 (交互 shell 需要)

## 并行策略

- 本仓库只做内核 syscall 硬化 + 用户态引导, 不碰图形栈
- 与 asterinas-riscv 主仓库定期对拍: 内核修复互相 cherry-pick
- 每个里程碑输出验证脚本 (QEMU 冒烟测试) 到 tools/riscv/nixos/

## 验收标准

M3 完成 = `nix eval nixpkgs#hello.name` 在 QEMU 里输出 "hello"

## 2026-08-13 更新: pku4090 可复用资产

在 pku4090 发现之前的 Asterinas RISC-V + NixOS 探索工作 (2026-04/07):

### Docker 镜像 (pku4090)
- `asterinas-env:nixos-build` (33.7GB) — 完整 Nix (8167 store paths) + riscv64-unknown-linux-musl 交叉工具链 + LTP 测试门禁 + busybox 1.37.0
- `asterinas-env:sifive-u-cross` (15GB) — SiFive U 系列交叉环境
- `asterinas-env:uboot-sim` — u-boot 模拟
- `asterinas/asterinas:0.18.0-20260702-riscv-cross[-dtc]` — 官方 riscv 交叉镜像

### 关键发现
- 路线选择: 之前走的是 **musl** (riscv64-unknown-linux-musl), 不是 glibc → M2 里程碑改为 musl 优先
- LTP 门禁: `make run_kernel AUTO_TEST=conformance TARGET_ARCH=riscv64 RELEASE=1` (SMP=1/4)
- NixOS 风格 setuid 包装器已为 riscv64 定义 (su/mount/passwd/fusermount/newgidmap)
- go 1.22.12 riscv64 bootstrap 已就位
- VDSO riscv64 参考二进制 (Linux 6.8) 在 /home/ubuntu/linux_vdso/
- 上游贡献: PR #3174 (测试可移植性), #3179 (virtio-net), issue #3178 (clone3 CLONE_FILES bug — 需验证)

### 策略调整
- 构建场: pku4090 (Docker 镜像内交叉编译) → 产物传输到 thinkpad → QEMU 运行
- M2 改为: 动态 musl 用户态 (比 glibc 引导快)

## 2026-08-13 修订: 单仓库 + thinkpad 单机策略

用户决策 (取代之前的 pku4090 构建场方案):
- **不另开仓库**: 本工作区是 asterinas-riscv 主仓库的 track/nixos 分支
- **只在 thinkpad 推进**: 构建 + QEMU 验证都在本机
- pku4090 的 Docker 镜像资产 (musl 工具链/LTP) 暂不使用, 保留为未来选项
- 分支布局: main = Xorg 轨道; track/nixos = NixOS 轨道 (本工作区)
- 内核修复以 PR 形式合并回 main
