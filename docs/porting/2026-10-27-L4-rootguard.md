# L4 coreutils：验证 root 守卫假设（FORCE_UNSAFE_CONFIGURE）

日期：2026-10-27
内核：主干（fork 停在 `c00db36af` 的代码线，ELF `eaa13d92f71c668c`）
基础镜像：`target/systemd-health/daily.ext2`（与 2026-10-25 的 3/3 基线同一镜像）

## 1. 假设（来自 `665f27871`）

gnulib 的 `gl_FUNC_MKNOD` 在 `geteuid()==0` 时直接 `return 99`，coreutils 的
`configure` 随即报

```
configure: error: you should not run configure as root
  (set FORCE_UNSAFE_CONFIGURE=1 in environment to bypass this check)
```

因此 L4 在 guest 内以 root 构建 coreutils 时，**只要把
`FORCE_UNSAFE_CONFIGURE=1` 传进 `dpkg-buildpackage` 的环境**，configure 就应
通过；之前 evidence 行里的 `GNULIB_TEST_MKNODAT` 只是 conftest 命令行的宏名。

## 2. 验证命令

```sh
make test_x86_debian_source_build \
  X86_DEBIAN_KERNEL=$PWD/target/osdk/asterinas-osdk-bin.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/systemd-health/daily.ext2 \
  X86_DEBIAN_XORG_ACCEL=kvm \
  SOURCE_BUILD_PACKAGES=coreutils \
  SOURCE_BUILD_WORKSPACE=$PWD/target/l4-rootguard \
  SOURCE_BUILD_TIMEOUT=2400
```

`tools/x86/debian/dpkg_build.sh:505-513` 的实际执行行（未改 coreutils 源码）：

```
timeout -k 10 … env DEBIAN_FRONTEND=noninteractive \
    DEB_BUILD_OPTIONS="nocheck parallel=4" \
    FORCE_UNSAFE_CONFIGURE=1 \
    dpkg-buildpackage -b -us -uc >"$LOGDIR/pkg$index-build.log" 2>&1
```

## 3. 结果（原始日志行）

### 3.1 主机侧 DPKGB（`target/l4-rootguard/console.txt`）

```
DPKGB pkg=1 start name=coreutils
DPKGB pkg=1 disk_free_kb=2084828 attempt=1
DPKGB pkg=1 stage=builddeps rc=0 dur=289.30 attempt=1
DPKGB pkg=1 stage=source rc=0 dur=11.34 attempt=1
DPKGB pkg=1 stage=build rc=0 dur=491.24 attempt=1
DPKGB pkg=1 artifact=coreutils-dbgsym_9.7-3_amd64.deb
DPKGB pkg=1 artifact=coreutils_9.7-3_amd64.deb
DPKGB pkg=1 done name=coreutils result=ok attempts=1 dur=803.20
```

`source-build/packages.tsv`：`1 coreutils ok ok 1 803.20 0 289.3 0 11.34 0 491.24 …`
（builddeps/source/build 三段 rc 全 0）。整跑 `gate_rc=0`，无 panic。

### 3.2 guest 侧 configure 原始行（`/var/tmp/dpkg-logs/pkg1-build.log:815-832`）

```
checking for mkfifo... yes
checking whether mkfifo rejects trailing slashes... yes
checking for mknodat... yes
checking for mkfifoat... yes
checking whether mkfifoat rejects trailing slashes... yes
checking whether mknod can create fifo without root privileges... no
```

关键点：

- `checking for mknodat... yes` —— mknodat 在 Asterinas 上被正确探测到；
- `checking whether mknod can create fifo without root privileges... no` ——
  **这一项确实执行了**（root 下 conftest 返回 99，被判为 "no"），configure
  **没有报错退出**（`FORCE_UNSAFE_CONFIGURE=1` 生效），继续往下走；
- `config.status: creating Makefile`（日志第 1137 行）——configure 完整结束；
- 最后一行 `dpkg-buildpackage: info: binary-only upload (no source included)`，
  两个 `.deb` 产出；
- `GNULIB_TEST_MKNODAT` 在整份 636 KB 构建日志中出现 **0 次**。

注：判为 "no" 后 gnulib 会置 `MKNOD_FIFO_BUG` 并使用自己的 mknod 替换实现——
这与在 **Linux 上以 root 构建** 的行为完全一致（root 构建永远拿不到 "yes"），
不影响产物。

## 4. 结论

| 判据 | 结果 |
|---|---|
| configure 是否通过 | **通过**（`config.status: creating Makefile`，无 root-guard 报错） |
| 是否仍出现 `GNULIB_TEST_MKNODAT` 失败行 | **没有**（该符号在构建日志中出现 0 次） |
| 是否推进到新阶段 | 是：完整编译 + 链接 + 打包，产出 `coreutils_9.7-3_amd64.deb` 与 `coreutils-dbgsym_9.7-3_amd64.deb` |
| L4 状态 | coreutils **ok（1/1，803.20 s，首次尝试）** → 与前两个包（hello、python3-minimal）合计 **L4 = 3/3** |

**假设成立**：L4 coreutils 的失败点就是 gnulib 的 root 守卫；
`FORCE_UNSAFE_CONFIGURE=1`（`dpkg_build.sh:505-513` 的环境前缀）使其通过，
**不改 coreutils 源码**、不改 gnulib，内核侧零改动。

**仍未做的事**：本轮只跑了 `coreutils` 单包（`SOURCE_BUILD_PACKAGES=coreutils`），
L4 的 3/3 是把本结果与 2026-10-25 的 `hello`/`python3-minimal` 结果合并的结论；
如需一次跑满三包，用同样的入口去掉 `SOURCE_BUILD_PACKAGES`（约 16 分钟）。
