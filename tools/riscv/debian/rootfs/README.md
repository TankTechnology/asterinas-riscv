# Debian RISC-V persistent-root gate

This workflow builds one signed Debian Trixie `riscv64` ext2 image and then
boots it twice on current Asterinas. The first boot writes and syncs a random
nonce; the second boot must read the same nonce from the same writable root
disk. The runtime is headless, has four harts, and uses `-nic none`.

Run all commands from the repository root. The validated development image is
`asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached`.

## Proxy and container setup

### binfmt safety boundary

The rootfs builder needs a `qemu-riscv64` binfmt handler for the target-side
`chroot` steps.  **Do not enable or register that handler on the host** with
`update-binfmts`, `tonistiigi/binfmt`, or a write to
`/proc/sys/fs/binfmt_misc/register`: Docker's privileged mount can propagate
the registration back to the host and leave a persistent global interpreter.
Before any build, inspect the host registration read-only and stop if it is
missing or unexpected.  The supported build runner must provide an already
audited, isolated binfmt boundary and pass its mounted tree through
`ASTERINAS_BINFMT_ROOT`; the builder only verifies the tree and never mutates
it.  If that boundary is unavailable, keep the rootfs build deferred and run
the unit/contract tests instead of changing host binfmt state.

Check Clash without changing Docker, apt, Cargo, or Git configuration:

```bash
export ASTERINAS_PROXY=http://127.0.0.1:17892
curl --proxy "$ASTERINAS_PROXY" --fail --head \
  https://mirrors.tuna.tsinghua.edu.cn/debian/dists/trixie/InRelease
```

Pass the proxy only to this container invocation:

```bash
docker run --rm -it --network=host \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  -e http_proxy="$ASTERINAS_PROXY" -e https_proxy="$ASTERINAS_PROXY" \
  -e HTTP_PROXY="$ASTERINAS_PROXY" -e HTTPS_PROXY="$ASTERINAS_PROXY" \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached
```

Inside the container, install the build, signature, filesystem, and emulation
dependencies. This does not weaken Debian signature verification.

```bash
apt-get update
apt-get install -y --no-install-recommends \
  debootstrap qemu-user-static binfmt-support debian-archive-keyring \
  gcc-riscv64-linux-gnu libc6-dev-riscv64-cross \
  linux-libc-dev-riscv64-cross cpio e2fsprogs curl gpgv device-tree-compiler \
  qemu-system-misc
# Historical host-mutating command; do not run:
# update-binfmts --enable qemu-riscv64
cat /proc/sys/fs/binfmt_misc/qemu-riscv64
```

The final read-only check must show `enabled`, the
`qemu-riscv64-static` interpreter, and the `F` flag. If the registration is
already supplied by the host kernel, do not replace it with handwritten
binfmt magic.

## Build the frozen root once

The default HTTPS mirror is TUNA:

```bash
tools/riscv/debian/rootfs/build_rootfs.sh
```

If TUNA fails, retry explicitly with USTC, then official Debian. Do not persist
either URL in system apt configuration.

```bash
tools/riscv/debian/rootfs/build_rootfs.sh \
  --mirror https://mirrors.ustc.edu.cn/debian
tools/riscv/debian/rootfs/build_rootfs.sh \
  --mirror https://deb.debian.org/debian
```

The signed output is under `target/debian-riscv/rootfs/`. Reuse it while this
verification succeeds; do not rebuild merely to repeat a test:

```bash
python3 -m tools.riscv.debian.rootfs.contract verify \
  --image target/debian-riscv/rootfs/debian-root.ext2 \
  --manifest target/debian-riscv/rootfs/rootfs-manifest.json \
  --packages-lock target/debian-riscv/rootfs/packages.lock
```

Build the separate schema-v2 systemd profile only when the M1 artifact is not
the intended input. It has a distinct label, UUID, and output directory, so it
cannot alias the interactive root:

```bash
tools/riscv/debian/rootfs/build_rootfs.sh --profile systemd-m2
python3 -m tools.riscv.debian.rootfs.contract verify \
  --image target/debian-riscv/systemd-m2/rootfs/debian-root.ext2 \
  --manifest target/debian-riscv/systemd-m2/rootfs/rootfs-manifest.json \
  --packages-lock target/debian-riscv/systemd-m2/rootfs/packages.lock
```

The M2 profile installs Debian's packaged systemd as PID 1, requires
`systemd-logind` to reach its active state on the first cold boot, provides a
deterministic serial evidence service, and contains no `qemu-riscv64-static`
guest binary. The logind check is the first desktop-session foundation gate:
it verifies the service responsible for seats, sessions, and device ownership.
The second boot remains a persistence and normal-reboot check; it intentionally
does not duplicate the logind check. Neither marker claims that a display
server or desktop session has started.
Stage1 must receive the exact init argument `--root-init=systemd`; the gate
places it after the kernel command-line `--` separator so Asterinas forwards it
as init argv.

## Build current-main boot artifacts

Build the current Sv39/SMP=4 kernel and deterministic stage-1 handoff archive:

```bash
make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
tools/riscv/debian/rootfs/build_stage1.sh \
  target/debian-riscv/stage1/initramfs.cpio
```

Prepare the pinned U-Boot build and an exact four-hart QEMU DTB. This bootstrap
disk is only a convenient producer of U-Boot and the DTB; the Debian gate
builds and validates its own three-file boot disk.

```bash
ASTERINAS_RISCV_BOOTI="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
ASTERINAS_INITRAMFS="$PWD/target/debian-riscv/stage1/initramfs.cpio" \
QEMU_UBOOT_PROFILE=generic-sv39-ltp-smp4 \
QEMU_UBOOT_OUT_DIR="$PWD/target/qemu-uboot/debian-root" \
QEMU_UBOOT_BUILD_DIR="$PWD/target/qemu-uboot/cache/u-boot-build" \
  tools/riscv/prepare_qemu_uboot_booti.sh prepare
```

## Test and run

The unit gate is local and does not launch QEMU or use the network:

```bash
make test_riscv_debian_rootfs_unit
```

## Short Firefox GDB probe

For startup diagnosis, use the bounded qemu-user probe before attempting the
full Asterinas web gate.  It requires an extracted, writable riscv64 rootfs
and invokes an explicit `qemu-riscv64-static` inside a bwrap rootfs namespace
with `-L /`; it never registers or changes a host `binfmt_misc` handler. This
is important because `-L` alone changes loader lookup, not guest absolute
paths:

```bash
tools/riscv/debian/rootfs/firefox_gdb_probe.sh \
  /path/to/extracted-rootfs \
  "$PWD/../backups/firefox-gdb-probe-$(date +%Y%m%d)" \
  45
```

The output directory contains the exact GDB command file, register and
backtrace evidence, QEMU stderr, and metadata including the binfmt read-only
check. `riscv64-linux-gnu-gdb` is required because the target is RISC-V; the
host-native `gdb` is not a substitute. The host also needs `bwrap` and an
explicit `qemu-riscv64-static` binary (the latter can be supplied via
`QEMU_RISCV64_STATIC`). This probe is intentionally limited
to loader entry and `__libc_start_main@plt`; use the Asterinas QEMU `-S -gdb`
workflow separately for kernel breakpoints.

The kernel-side reset probe is also reusable and does not boot the guest past
the first instruction:

```bash
tools/riscv/qemu_system_gdb_probe.sh \
  target/osdk/aster-kernel/aster-kernel-osdk-bin.Image \
  "$PWD/../backups/asterinas-system-gdb-$(date +%Y%m%d)" \
  15
```

The raw `.Image` is used as the QEMU payload.  To add kernel symbols, point
`ASTERINAS_KERNEL_SYMBOLS` at the matching unstripped ELF
(`target/osdk/aster-kernel/aster-kernel-osdk-bin`) before running the command.
The default remains a reset-only snapshot; set `ASTERINAS_KERNEL_CONTINUE=1`
only when deliberately testing a boot handoff toward `_start`.

For a live Firefox Web gate that is already known to reach the slow page
phase, enable the system-emulation stub explicitly on a loopback port.  The
normal gate does not contain a GDB argument, and this opt-in does not add
`-S`, so the VM continues booting before a debugger connects:

```bash
ASTERINAS_QEMU_GDB_PORT=23456 \
  python3 -m tools.riscv.debian.rootfs.browser_web_qemu_gate \
  ...the frozen browser-web gate arguments...

tools/riscv/qemu_live_pc_sampler.sh 23456 \
  "$PWD/../backups/firefox-live-pc-$(date +%Y%m%d)" 20 2
```

The sampler uses `riscv64-linux-gnu-gdb`, captures PC/RA/SP for every QEMU
hart, and detaches after each sample.  It stops early after three consecutive
connection failures, which normally means the gate has already terminated.
Use a matching unstripped kernel ELF to symbolize kernel PCs.  User PCs must
be interpreted with the sampled process's `/proc/<pid>/maps`; an address alone
does not distinguish a shared library from JIT/anonymous executable memory.
Neither command registers or modifies a host binfmt handler.

## Firefox Web acceptance semantics

The schema-seven `browser-web` gate separates deterministic browser
functionality from third-party anti-automation policy.  It submits the real
form in the host-served `/browser-quality/` fixture and requires the resulting
URL, DOM, CJK/Latin text, PNG resource timing, and screenshot.  It also
requires strict HTTPS/DOM/screenshots for the Baidu home page, Bilibili home
page, and a live BV detail link selected from that page.  A Baidu search is
recorded as either a validated result page or an exact
`wappass.baidu.com/static/captcha/` challenge whose back URL contains the
submitted query.  The latter is reported as `external-captcha`; it is
observable public-site evidence, not a successful search result.

The guest image precreates the ten JSON/PNG evidence inodes.  The gate
overwrites them, performs a whole-filesystem `sync`, and only then emits
`DEBIAN_BROWSER_WEB_READY`.  DNS and curl probes have explicit outer bounds,
and shell/Python timeline producers share the `/proc/uptime` monotonic clock.
The host validates the exact evidence set after QEMU stops, including PNG
structure, trust hashes, process identity, phase pairs, and ordered timeline
markers.

Firefox ESR currently does not enable the Linux sandbox by default for
RISC-V: Mozilla's `sandbox_default` supports Linux x86/x86_64/ARM/AArch64,
and Debian's riscv64 package does not override it with `--enable-sandbox`.
Consequently, a riscv64 content process with `Seccomp: 0` is recorded as
`unavailable-firefox-riscv64-build`, never as `sandbox=normal`.  The gate
still requires uid 1000, zero capabilities, `NoNewPrivileges=1`, stable
service identity, and absence of sandbox-disable arguments/environment.  A
future build that actually enables the sandbox must report `Seccomp: 2` and
is recorded as `enabled`.

The read-only toolchain helper provides a common preflight, evidence summary,
and hash manifest:

```bash
python3 -m tools.riscv.firefox_debug_tool preflight
BACKUP_DIR="$PWD/../backups/firefox-gdb-probe-$(date +%Y%m%d)"
python3 -m tools.riscv.firefox_debug_tool summarize \
  --gdb "$BACKUP_DIR/gdb-entry.txt" \
  --gdb "$BACKUP_DIR/gdb-libc-start.txt" \
  --syscall-log "$BACKUP_DIR/qemu-user-strace.log" \
  --output "$BACKUP_DIR/summary.json"
python3 -m tools.riscv.firefox_debug_tool manifest \
  "$BACKUP_DIR" \
  --output "$BACKUP_DIR/manifest.json"
```

For a bounded guest startup profile, append the opt-in kernel parameters
`asterinas.vm_profile=1 asterinas.vm_pagecache_profile=1
asterinas.futex_profile=1 asterinas.syscall_profile=1
asterinas.epoll_profile=1` before the `--`
separator in the gate boot arguments.  The syscall profiler records entry,
completion, and elapsed jiffies for `close`, `ppoll`, `futex`,
`clock_gettime`, `sched_yield`, `clone`, and `execve`.  Entry counts are logged
before the handler runs, so a blocked syscall remains visible even when the
guest reaches the hard timeout.  Keep the profiler disabled for normal gates;
it is diagnostic instrumentation only.  `asterinas.epoll_profile=1` adds
bounded counts of timeout classes and return classes for epoll waits; the
epoll-entry evidence hook is compiled disabled and is only useful in a
purpose-built diagnostic image.

Run the explicit two-boot gate as root in the development container:

```bash
make test_riscv_debian_rootfs_gate \
  DEBIAN_KERNEL="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  DEBIAN_UBOOT="$PWD/target/qemu-uboot/cache/u-boot-build/u-boot" \
  DEBIAN_DTB="$PWD/target/qemu-uboot/debian-root/qemu-virt.dtb" \
  DEBIAN_STAGE1_INITRAMFS="$PWD/target/debian-riscv/stage1/initramfs.cpio" \
  DEBIAN_ROOT_IMAGE="$PWD/target/debian-riscv/rootfs/debian-root.ext2" \
  DEBIAN_ROOT_MANIFEST="$PWD/target/debian-riscv/rootfs/rootfs-manifest.json" \
  DEBIAN_PACKAGES_LOCK="$PWD/target/debian-riscv/rootfs/packages.lock" \
  DEBIAN_PACKAGE_CHECKSUMS="$PWD/target/debian-riscv/rootfs/source-metadata/package-checksums" \
  DEBIAN_GATE_OUTPUT="$PWD/target/debian-riscv/gate"
```

For Firefox-only startup profiling, use the bounded one-boot sampler. It waits
for `BOOT_BASIC_TARGET`, X socket readiness, Firefox `exec`, and Marionette,
then exits without running the full web protocol. The sampler adds only
`asterinas.vm_profile=1` and keeps the final transcript in the root-owned
output directory:

```bash
python3 tools/riscv/debian/rootfs/firefox_startup_profile.py \
  --kernel /path/to/kernel.Image \
  --uboot /path/to/u-boot \
  --dtb /path/to/qemu-virt.dtb \
  --stage1-initramfs /path/to/initramfs.cpio \
  --root-image /path/to/debian-root.ext2 \
  --root-manifest /path/to/rootfs-manifest.json \
  --packages-lock /path/to/packages.lock \
  --package-checksums /path/to/package-checksums \
  --output-directory /path/to/root-owned-profile \
  --boot-timeout 360
```

追加 `--firefox-process-diagnostic` 可在 Firefox exec 后启用有界的 `ps` 和
`/proc` 快照；它会增加少量串口扰动，只用于定位主进程/子进程状态。若要把
高频 epoll 调用归因到具体 caller/fd，可再追加
`--epoll-entry-diagnostic`；它启用 `asterinas.epoll_profile=1` 和
`asterinas.epoll_entry_profile=1`，同样只用于短 probe。guest procfs 的
`wchan` 等文件可能阻塞，因此进程快照不应作为唯一阻塞点证据。
追加 `--timerfd-diagnostic` 可统计 timerfd 的 set/expire/read/EAGAIN 聚合，
用于验证 epoll 伪就绪；追加 `--syscall-diagnostic` 可记录常见 syscall 的
进入/完成次数、累计 jiffies 及 clone/exec 边界。两者都只影响诊断镜像的
bootargs，默认关闭，不改变正常启动语义。

For the systemd M2 profile, use the M2 root and Stage1 archive. This gate keeps
one QEMU process alive across the guest's normal reboot, interrupts the second
U-Boot autoboot, and launches Asterinas a second time without `saveenv`:

```bash
make test_riscv_debian_systemd_m2_gate \
  DEBIAN_KERNEL="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  DEBIAN_UBOOT="$PWD/target/qemu-uboot/cache/u-boot-build/u-boot" \
  DEBIAN_DTB="$PWD/target/qemu-uboot/debian-root/qemu-virt.dtb" \
  DEBIAN_STAGE1_INITRAMFS="$PWD/target/debian-riscv/systemd-m2/stage1/initramfs.cpio" \
  DEBIAN_ROOT_IMAGE="$PWD/target/debian-riscv/systemd-m2/rootfs/debian-root.ext2" \
  DEBIAN_ROOT_MANIFEST="$PWD/target/debian-riscv/systemd-m2/rootfs/rootfs-manifest.json" \
  DEBIAN_PACKAGES_LOCK="$PWD/target/debian-riscv/systemd-m2/rootfs/packages.lock" \
  DEBIAN_PACKAGE_CHECKSUMS="$PWD/target/debian-riscv/systemd-m2/rootfs/source-metadata/package-checksums" \
  DEBIAN_SYSTEMD_M2_GATE_OUTPUT="$PWD/target/debian-riscv/systemd-m2/gate"
```

The target requires every frozen artifact and never invokes debootstrap, a
mirror, or `build_rootfs.sh`. Network is permitted only while constructing the
signed base root. The M1 and M2 gates are always launched with `-nic none`;
the explicit M5 gate below is the only slirp-enabled exception.

### QEMU M5 HTTPS desktop gate

Build the `desktop-m5-network` root when the external-network desktop is the
intended input. This profile adds the `curl` command as a frozen package
identity. The QEMU gate uses slirp only for the guest data path; it does not
inherit host proxy variables, TAP configuration, or a Linux guest kernel.

```bash
tools/riscv/debian/rootfs/build_rootfs.sh --profile desktop-m5-network
make test_riscv_debian_desktop_m5_qemu_gate \
  DEBIAN_KERNEL="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  DEBIAN_UBOOT="$PWD/target/qemu-uboot/cache/u-boot-build/u-boot" \
  DEBIAN_DTB="$PWD/target/qemu-uboot/debian-root/qemu-virt.dtb" \
  DEBIAN_STAGE1_INITRAMFS="$PWD/target/debian-riscv/desktop-m5-network/stage1/initramfs.cpio" \
  DEBIAN_ROOT_IMAGE="$PWD/target/debian-riscv/desktop-m5-network/rootfs/debian-root.ext2" \
  DEBIAN_ROOT_MANIFEST="$PWD/target/debian-riscv/desktop-m5-network/rootfs/rootfs-manifest.json" \
  DEBIAN_PACKAGES_LOCK="$PWD/target/debian-riscv/desktop-m5-network/rootfs/packages.lock" \
  DEBIAN_PACKAGE_CHECKSUMS="$PWD/target/debian-riscv/desktop-m5-network/rootfs/source-metadata/package-checksums" \
  DEBIAN_DESKTOP_M5_QEMU_GATE_OUTPUT="$PWD/target/debian-riscv/desktop-m5-network/qemu-gate"
```

The gate requires ordered DNS and HTTPS evidence for `www.baidu.com`, the
exact slirp local address `10.0.2.15`, all M4 application-window milestones,
and a non-blank framebuffer capture. ICMP ping sockets and `ip` route dumps
are not part of this browser gate because those Linux interfaces remain
separate compatibility work; UDP DNS and TCP/TLS are tested directly.

NetSurf can render ordinary raster and SVG page assets supplied by its Debian
dependencies. Its JavaScript engine is intentionally not a modern Chromium
compatibility claim: JavaScript must be reported by a separate local DOM smoke
test and is not allowed to turn a successful DNS/HTTPS gate into a failure.
The current non-blank framebuffer check proves the desktop, not that the
foreground window has finished rendering Baidu.

### QEMU M6 browser evidence gate

After rebuilding the `desktop-m5-network` root, use the M6 gate to foreground
and capture a Baidu-hosted PNG in NetSurf before navigating the same window to
a fixed local JavaScript fixture. The direct image isolates HTTPS transfer and
image decoding from the modern Baidu homepage's script workload:

```bash
make test_riscv_debian_desktop_m6_browser_gate \
  DEBIAN_KERNEL="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  DEBIAN_UBOOT="$PWD/target/qemu-uboot/cache/u-boot-build/u-boot" \
  DEBIAN_DTB="$PWD/target/qemu-uboot/debian-root/qemu-virt.dtb" \
  DEBIAN_STAGE1_INITRAMFS="$PWD/target/debian-riscv/desktop-m5-network/stage1/initramfs.cpio" \
  DEBIAN_ROOT_IMAGE="$PWD/target/debian-riscv/desktop-m5-network/rootfs/debian-root.ext2" \
  DEBIAN_ROOT_MANIFEST="$PWD/target/debian-riscv/desktop-m5-network/rootfs/rootfs-manifest.json" \
  DEBIAN_PACKAGES_LOCK="$PWD/target/debian-riscv/desktop-m5-network/rootfs/packages.lock" \
  DEBIAN_PACKAGE_CHECKSUMS="$PWD/target/debian-riscv/desktop-m5-network/rootfs/source-metadata/package-checksums" \
  DEBIAN_DESKTOP_M6_BROWSER_GATE_OUTPUT="$PWD/target/debian-riscv/desktop-m5-network/m6-qemu-gate"
```

`desktop-m6-browser.ppm` records the foreground Baidu logo image and
`desktop-m6-javascript.ppm` records the local fixture after classification.
`result.json` reports `limited-pass`, `disabled`, or `failed`. A
`limited-pass` proves only that the packaged NetSurf engine executed a local
script-only navigation from the pending fixture to a fixed pass document; it
is not a claim of Chromium-compatible JavaScript. The changing remote pixels
are inspected as visual evidence and are not compared to a fixed hash.
Rendering the full modern Baidu homepage is a later compatibility target, not
a prerequisite for this foundational gate.

### QEMU M7 real Baidu page evidence

The M7 gate reuses the M6 network, desktop, remote-image, and local-JavaScript
evidence before starting a fresh NetSurf process with JavaScript disabled. It
loads Baidu's official mobile page because the HTTPS desktop endpoint falls
back to an approximately 700-KiB legacy document that NetSurf 3.11 does not
finish laying out. The mobile document is approximately 80 KiB and retains
the real Baidu logo, search form, remote stylesheets, and image requests.

```bash
make test_riscv_debian_desktop_m7_baidu_gate \
  DEBIAN_KERNEL="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  DEBIAN_UBOOT="$PWD/target/qemu-uboot/cache/u-boot-build/u-boot" \
  DEBIAN_DTB="$PWD/target/qemu-uboot/debian-root/qemu-virt.dtb" \
  DEBIAN_STAGE1_INITRAMFS="$PWD/target/debian-riscv/desktop-m5-network/stage1/initramfs.cpio" \
  DEBIAN_ROOT_IMAGE="$PWD/target/debian-riscv/desktop-m7-baidu/rootfs/debian-root.ext2" \
  DEBIAN_ROOT_MANIFEST="$PWD/target/debian-riscv/desktop-m7-baidu/rootfs/rootfs-manifest.json" \
  DEBIAN_PACKAGES_LOCK="$PWD/target/debian-riscv/desktop-m7-baidu/rootfs/packages.lock" \
  DEBIAN_PACKAGE_CHECKSUMS="$PWD/target/debian-riscv/desktop-m7-baidu/rootfs/source-metadata/package-checksums" \
  DEBIAN_DESKTOP_M7_BAIDU_GATE_OUTPUT="$PWD/target/debian-riscv/desktop-m7-baidu/m7-qemu"
```

`desktop-m7-baidu-home.ppm` is published only after the title identifies a
real page from `https://m.baidu.com/`. A search-result frame and a passing
`result.json` additionally require the submitted query to return a result
title. If Baidu sends its `wappass.baidu.com` security challenge, the gate
publishes `desktop-m7-baidu-failure.ppm` and remains failed. The challenge is
evidence that DNS, HTTPS, navigation, and rendering reached Baidu, but it is
not accepted as search-result evidence.

The 2026-08-28 generic-Sv39/SMP=4 run reached the homepage milestone and
captured a visible Baidu logo and search box. The subsequent `asterinas`
query rendered `百度安全验证`, so the final M7 result remained failed with
`search-title-timeout`. The current image also lacks a CJK font, leaving some
Chinese text as missing-glyph boxes. These are user-space/browser and remote
service limitations; the run did not expose a new Asterinas DNS, TCP, TLS,
VirtIO input, Xorg, or framebuffer failure.

### QEMU M9 desktop software smoke gate

The M9 profile is a separate signed rootfs derived from the M5 desktop
package set. It uses Debian's official `riscv64` `netsurf-gtk`, adds `vim` and
`ffmpeg`, and does not list `ffprobe` separately: Debian ships `/usr/bin/ffprobe`
from the `ffmpeg` package. The manifest deliberately keeps schema version 5
for compatibility with existing readers while binding the new profile name,
label, UUID, and package identity.

Build and verify the image once, then reuse it for both QEMU and the bounded
Megrez run:

```bash
tools/riscv/debian/rootfs/build_rootfs.sh --profile desktop-m9-software
python3 -m tools.riscv.debian.rootfs.contract verify \
  --image target/debian-riscv/desktop-m9-software/rootfs/debian-root.ext2 \
  --manifest target/debian-riscv/desktop-m9-software/rootfs/rootfs-manifest.json \
  --packages-lock target/debian-riscv/desktop-m9-software/rootfs/packages.lock
```

Run the complete four-hart QEMU contract with finite timeouts. This single
gate reuses the M5 slirp/DNS/HTTPS, M4 desktop, and M7 NetSurf/Baidu evidence,
then executes the M9 software service. The M8 quality gate remains an
independent optional run because its title assertion is intentionally not a
prerequisite for application smoke evidence:

```bash
make test_riscv_debian_desktop_m9_software_gate \
  DEBIAN_KERNEL="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  DEBIAN_UBOOT="$PWD/target/qemu-uboot/cache/u-boot-build/u-boot" \
  DEBIAN_DTB="$PWD/target/qemu-uboot/debian-root/qemu-virt.dtb" \
  DEBIAN_STAGE1_INITRAMFS="$PWD/target/debian-riscv/stage1/initramfs.cpio" \
  DEBIAN_ROOT_IMAGE="$PWD/target/debian-riscv/desktop-m9-software/rootfs/debian-root.ext2" \
  DEBIAN_ROOT_MANIFEST="$PWD/target/debian-riscv/desktop-m9-software/rootfs/rootfs-manifest.json" \
  DEBIAN_PACKAGES_LOCK="$PWD/target/debian-riscv/desktop-m9-software/rootfs/packages.lock" \
  DEBIAN_PACKAGE_CHECKSUMS="$PWD/target/debian-riscv/desktop-m9-software/rootfs/source-metadata/package-checksums" \
  DEBIAN_DESKTOP_M9_SOFTWARE_GATE_OUTPUT="$PWD/target/debian-riscv/desktop-m9-software/m9-qemu-gate" \
  DEBIAN_DESKTOP_BOOT_TIMEOUT=900
```

The software service is intentionally small and fail-closed. It saves a
marker line with non-interactive Vim, generates a deterministic 16x16 RGB
frame with single-threaded FFmpeg on the ext2-backed `/var/tmp`, and checks
its dimensions with ffprobe. Each command has a 120-second timeout and the
service has a 300-second systemd budget, so a missing package or a hung
process produces `DEBIAN_DESKTOP_M9_FAIL reason=...` instead of leaving a
QEMU or board session running indefinitely. The QEMU result retains the
serial transcript, screenshot, network fixture summary, and immutable
manifest identity.

For an unreliable Chinese-network path, first use the proxy preflight above;
if TUNA cannot provide a complete signed closure, retry the build explicitly
with USTC and then `deb.debian.org`. Do not change the mirror in the guest or
run an unbounded `apt` command on Megrez. The verified `.deb` closure and its
`package-checksums` are the offline installation input for the physical test.

### Megrez static-RJ45 browser gate

The physical browser milestone reuses the signed `desktop-m5-network` root
and the kernel's reviewed static profile. The exact guest identity is:

```text
asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1
interface=eth0
primary DNS=10.2.0.5
fallback DNS=10.2.0.6
```

Prepare the host with the canonical
[Megrez debugging tool list](../../README.md#host-side-megrez-debugging).
Keep QEMU and cross-build dependencies in the pinned container;
the host tools are only for serial, link, packet, throughput, and screenshot observations.

Install a newly built ext2 image with
`tools.riscv.debian.rootfs.megrez_installer`; Asterinas must write and read
back eMMC partition 2. Linux may stage immutable boot files but is not an
accepted runtime or installer kernel. On the currently verified firmware,
U-Boot exposes only `ethernet@50400000`, while the live RJ45 path selected by
Asterinas is the other GMAC. Its TFTP path therefore cannot be the default
recovery transport. Stage the current Image, frozen Megrez DTB, and Stage1
under their basenames on eMMC partition 1, verify the copied hashes, unmount
the partition, and run the bounded gate with the read-only MMC loader:

```bash
crc32_file() {
  python3 -c 'import pathlib,sys,zlib; print(f"{zlib.crc32(pathlib.Path(sys.argv[1]).read_bytes()):08x}")' "$1"
}
BOOTI_CRC32="$(crc32_file target/megrez-browser-network/tftp/asterinas-browser-net.booti)"
STAGE1_CRC32="$(crc32_file target/megrez-browser-network/tftp/debian-browser-stage1.cpio)"
python3 -m tools.riscv.megrez_gmac_gate /dev/ttyUSB0 \
  --booti asterinas-browser-net.booti \
  --dtb eic7700-milkv-megrez.dtb \
  --initrd debian-browser-stage1.cpio \
  --expected-crc32 "booti=$BOOTI_CRC32,dtb=4afcb20e,initrd=$STAGE1_CRC32" \
  --host-interface enp12s0 --load-transport mmc \
  --reboot-after 420 \
  --output-directory target/megrez-browser-network/gate \
  --boot-timeout 360 --drain-timeout 5
```

The strict serial order is selected GMAC, physical M5 link/DNS/HTTPS/PNG
evidence, M5 READY, M4 desktop READY, M6 remote image, one JavaScript status,
and matching M6 READY. `limited-pass`, `disabled`, and `failed` describe only
the packaged NetSurf JavaScript engine; none claims Chromium compatibility.
The gate drains the full serial transcript before publishing `passed: true`.
It deliberately tests UDP DNS and TCP/TLS rather than ICMP: the current
Asterinas network path does not provide the Linux ping-socket contract, and a
ping result would not prove that browser traffic works.

On a physical browser run, one exact input-capability degradation is collected
rather than treated as a reason to release the serial port early. If M4 emits
`DEBIAN_DESKTOP_M4_DIAGNOSTIC missing=pointer-device` followed by
`DEBIAN_DESKTOP_M4_FAIL reason=desktop-timeout`, the collector continues to
require every M6/M7 marker and the fresh automatic U-Boot recovery. A complete
Baidu homepage and search sequence is then published as `passed: false` with
`guest-failure-recovered:browser-pass-input-missing:pointer-device`; it proves
the browser path but deliberately does not claim mouse usability. Missing,
reordered, duplicated, or differently attributed M4 failure evidence remains
a hard failure.

For a desktop plan with `asterinas.reboot_after=600`, invoke
`tools.riscv.megrez_debug board` with `--timeout 900`. The timeout is measured
by the host, while the recovery timer is measured by Asterinas; the guest clock
can advance more slowly on Megrez. Shorter host budgets can therefore publish
`recovery-not-observed` after the browser evidence even though the board later
returns to U-Boot automatically.

This is a bounded useful-network contract, not a general Linux network stack
milestone. DHCP, `RTM_NEWADDR`, `RTM_NEWROUTE`, NetworkManager, cable-replug
recovery, live GMAC failover, USB Ethernet, Wi-Fi, Firefox, and modern
JavaScript remain outside this gate.

## Evidence and cleanup

Inspect the immutable provenance and the current-run evidence:

```bash
python3 -m json.tool target/debian-riscv/rootfs/rootfs-manifest.json
sed -n '1,20p' target/debian-riscv/rootfs/packages.lock
sed -n '1,200p' target/debian-riscv/gate/boot1.serial.log
sed -n '1,200p' target/debian-riscv/gate/boot2.serial.log
python3 -m json.tool target/debian-riscv/gate/result.json
python3 -c 'import json; print(json.load(open("target/debian-riscv/gate/result.json"))["final_root_sha256"])'
```

`debian-root.ext2` is immutable. Only the run-private
`debian-root.run.ext2` is writable. A successful run retains both logs, the
writable root, boot disk, and `result.json`. A failed run retains the complete
available logs and a failing result, never a stale `passed: true`, and never
mutates the base image.

## Verified M1 evidence (2026-08-24)

The documented Make target passed on source commit `d50b17aef` in container
image `sha256:4f054ba7e4d35567cd1b974506ecc6ae4a9e35e52616ca048cf302f8dfca8b23`
with QEMU 10.2.1. The runtime container used `--network=none`.

Frozen inputs:

| Artifact | SHA-256 |
| --- | --- |
| Asterinas Sv39/SMP=4 kernel | `9b0b352bc5f3fb38c7d4ee67f3abc40785f3a26861032944a67db8a38da63b60` |
| stage-1 initramfs | `aef46a338a158dbb9fbe4ed220167eb95c6a392c9f17de15d3877230eb740b08` |
| four-hart QEMU DTB | `3886fd4e5e7f47e3ba1536b3a374f89d4d06cf42f9c3bb5c9038e418ebf9dec9` |
| U-Boot | `cd1f164d4d6c3493bdceec168d2d066aaa218fe516ea9cd8cbc049427f9b55bc` |
| immutable Debian root | `060f613281f2e77fa2232f31322213a310f48b5b18df2991ade9eb2fca7bebae` |
| rootfs manifest | `6f246da49af0759184b47867047bec2e73d0be7228f08d0f723ce248412a14ae` |
| package lock | `fd817c8db7bd71098113b8c2ed4f52c3d70542efaaf97ced2b81637c5528dfff` |
| signed TUNA Trixie `InRelease` | `98b25b5cd185c59d34aa6e4c3e9b5b8f01bbe9d104fe2dcfbcd30dc0a14a59ed` |

The manifest records Trixie `13.6`, `riscv64`, mirror
`https://mirrors.tuna.tsinghua.edu.cn/debian`, and build timestamp
`2024-01-01T00:00:00Z`. The checked identity packages were
`base-files=13.8+deb13u6`, `libc6=2.41-12+deb13u3`,
`bash=5.2.37-2+b9`, `coreutils=9.7-3`, and `util-linux=2.41-5`.

The final result reported `passed: true`, reason `pass`, two QEMU argv vectors,
boot-one duration 14.042 seconds, boot-two duration 13.940 seconds, and final
writable-root SHA-256
`f6300db673c17c038a8bdbca76f092e891d0874b6d74737e3bb766bbe7492262`.
Both logs contain `__DEBIAN_ROOTFS_SHELL_READY__`, zero command statuses, and
the second-boot probe; nonce plaintext is replaced by `<nonce-redacted>`.

This evidence proves the generic QEMU Sv39/SMP=4 two-boot persistence
contract. It does not claim physical Megrez operation, guest networking,
systemd boot, display, USB, or desktop support.

## Megrez persistent Debian shell

Build two distinct current kernels. The generic QEMU artifact requires
`FEATURES=riscv_sv39_mode`; the Megrez artifact is a separate default Sv48
build. The frozen plan rejects swapping them and also records the exact
signed root, Stage1, U-Boot, and four-hart DTBs.

The board sequence is ` inventory ` before ` install-if-needed `. Inventory is
read-only, and a matching result skips installation. Only a measured image
hash mismatch may enter the Asterinas-only installer, which may write only
`/dev/mmcblk0p2`. The operator must not boot Linux to install or validate this
root. Do not arm the short EIC7700X watchdog while hashing the full device or
installing; use the bounded Asterinas timer and require a fresh U-Boot recovery
epoch. The `gate` command performs two bounded boots, and `handoff` is refused
unless their physical result passes.

Inventory, `gate`, and `handoff` transfer the compressed current kernel and
Stage1 over serial YMODEM. They load `eic7700-milkv-megrez.dtb` read-only from
eMMC partition 1 and reject a CRC mismatch. The current U-Boot GMAC probes
`0x50400000`, which is not the RJ45 path selected by Asterinas; consequently
these boot paths do not depend on TFTP. Only the Asterinas network installer
uses the verified board RJ45 path after the kernel has started.

```bash
make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
cp target/osdk/aster-kernel/aster-kernel-osdk-bin.Image /absolute/run/qemu-sv39.booti
make kernel TARGET_ARCH=riscv64 SMP=4
cp target/osdk/aster-kernel/aster-kernel-osdk-bin.Image /absolute/run/megrez-sv48.booti

RUN="$PWD/target/megrez-debian-shell/$(git rev-parse --short=12 HEAD)"
python3 -m tools.riscv.megrez_debian_shell check "$RUN/plan.json"
sudo -E python3 -m tools.riscv.megrez_debian_shell qemu \
  "$RUN/plan.json" --output "$RUN/qemu"
python3 -m tools.riscv.megrez_debian_shell permit \
  "$RUN/plan.json" --qemu-evidence "$RUN/qemu/qemu-evidence.json" \
  --output "$RUN/permit.json"
sudo -E python3 -m tools.riscv.megrez_debian_shell inventory \
  "$RUN/plan.json" /dev/ttyUSB0 --permit "$RUN/permit.json" \
  --output "$RUN/inventory-before" --yes
sudo -E python3 -m tools.riscv.megrez_debian_shell install-if-needed \
  "$RUN/plan.json" /dev/ttyUSB0 --permit "$RUN/permit.json" \
  --inventory "$RUN/inventory-before/result.json" --output "$RUN/install" --yes
if jq -e '.status == "needs-install"' "$RUN/inventory-before/result.json"; then
  sudo -E python3 -m tools.riscv.megrez_debian_shell inventory \
    "$RUN/plan.json" /dev/ttyUSB0 --permit "$RUN/permit.json" \
    --prior-inventory "$RUN/inventory-before/result.json" \
    --install-result "$RUN/install/result.json" \
    --output "$RUN/inventory-after" --yes
  cp "$RUN/inventory-after/result.json" "$RUN/inventory-current.json"
else
  cp "$RUN/inventory-before/result.json" "$RUN/inventory-current.json"
fi
sudo -E python3 -m tools.riscv.megrez_debian_shell gate \
  "$RUN/plan.json" /dev/ttyUSB0 --permit "$RUN/permit.json" \
  --inventory "$RUN/inventory-current.json" --output "$RUN/physical" \
  --host-interface enp12s0 --yes
sudo -E python3 -m tools.riscv.megrez_debian_shell handoff \
  "$RUN/plan.json" /dev/ttyUSB0 --result "$RUN/physical/result.json" \
  --host-interface enp12s0 --yes
picocom --baud 115200 --flow n --parity n --databits 8 /dev/ttyUSB0
```

This stage proves an interactive persistent shell only. The next scope is
systemd, network, and desktop; none is claimed by this milestone.
