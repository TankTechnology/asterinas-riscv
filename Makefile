# SPDX-License-Identifier: MPL-2.0

# =========================== Makefile options. ===============================

# Global build options.
TARGET_ARCH ?= x86_64
BENCHMARK ?= none
BOOT_METHOD ?= grub-rescue-iso
BOOT_PROTOCOL ?= multiboot2
ENABLE_KVM ?= 1
INTEL_TDX ?= 0
MEM ?= 8G
OVMF ?= on
RELEASE ?= 0
RELEASE_LTO ?= 0
LOG_LEVEL ?= error
SCHEME ?= ""
SMP ?= 1
RISCV_ICACHE_REQUIRE_SMP4 ?= 0
RISCV_LTP_SMP ?= 4
RISCV_LTP_SUITE ?= syscalls
DEBIAN_BROWSER_WEB_NETWORK_MODE ?= direct
DEBIAN_BROWSER_WEB_BASE_ROOTFS ?= $(CURDIR)/target/debian-riscv/browser-web/rootfs
DEBIAN_BROWSER_WEB_DEV_ROOTFS ?= $(CURDIR)/target/dev-overlays/browser-web/rootfs
DEBIAN_BROWSER_WEB_DEV_OVERLAY_SPEC ?= $(CURDIR)/tools/riscv/debian/rootfs/browser_web_dev_overlay.json
OSTD_TASK_STACK_SIZE_IN_PAGES ?= 64
FEATURES ?=
NO_DEFAULT_FEATURES ?= 0
COVERAGE ?= 0
INITRAMFS ?= on
CMDLINE ?=

# Specify the primary system console (supported: tty0, ttyS0, hvc0).
# - tty0: The active virtual terminal (VT).
# - ttyS0: The serial (UART) terminal.
# - hvc0: The virtio-console terminal.
# Asterinas will automatically fall back to tty0 if hvc0 is not available.
# Note that currently the virtual terminal (tty0) can only work with
# linux-efi-handover64 and linux-efi-pe64 boot protocol.
CONSOLE ?= hvc0
# End of global build options.

# GDB debugging and profiling options.
GDB_TCP_PORT ?= 1234
GDB_PROFILE_FORMAT ?= flame-graph
GDB_PROFILE_COUNT ?= 200
GDB_PROFILE_INTERVAL ?= 0.1
# End of GDB options.

# The Makefile provides a way to run arbitrary tests in the kernel
# mode using the kernel command line.
# Here are the options for the auto test feature.
AUTO_TEST ?= none
# Specify whether to build conformance tests under `test/initramfs/src/conformance`.
ENABLE_CONFORMANCE_TEST ?= false
CONFORMANCE_TEST_SUITE ?= ltp
CONFORMANCE_TEST_WORKDIR ?= /tmp
CONFORMANCE_TEST_SELECTOR ?=
# Whitespace-separated extra blocklist paths for conformance runners.
# - `gvisor` treats each entry as a directory relative to its runner directory,
#   and loads a per-test blocklist file from that directory.
# - `kselftest` treats each entry as a blocklist file relative to its runner
#   directory, and appends that file directly.
EXTRA_BLOCKLISTS ?= ""
# Parameters for xfstests.
XFSTESTS_RUNLIST ?= /opt/xfstests/short.list
XFSTESTS_DISK_SIZE ?= 12G
XFSTESTS_TEST_DEV ?= /dev/vdd
XFSTESTS_SCRATCH_DEV ?= /dev/vde
# Specify whether to build regression tests under `test/initramfs/src/regression`.
ENABLE_REGRESSION_TEST ?= false
REGRESSION_TEST_DIRS ?= null
# End of auto test features.

FOCUSED_NETWORK_AUTO_TESTS := \
	ifconf \
	ifreq \
	ip_socket_netns \
	netlink_route_netns \
	proc_net_dev \
	ipv6_dual_stack \
	ipv6_dual_stack_udp \
	ipv6_udp \
	tcp_user_buffer_prefault \
	udp_user_buffer_prefault \
	udp_msg_dontwait \
	tcp_msg_dontwait_send
ifneq ($(filter $(AUTO_TEST),$(FOCUSED_NETWORK_AUTO_TESTS)),)
REGRESSION_TEST_DIRS := [ "network" ]
endif

# Network settings
# NETDEV possible values are user,tap
NETDEV ?= user
VHOST ?= off
# The name server listed by /etc/resolv.conf inside the Asterinas VM
DNS_SERVER ?= none
# End of network settings

# NixOS settings
NIXOS_DISK_SIZE_IN_MB ?= 8192
NIXOS_DISABLE_SYSTEMD ?= false
# The following option is only effective when NIXOS_DISABLE_SYSTEMD is set to 'true'.
# Use a login shell to ensure that environment variables are initialized correctly.
NIXOS_STAGE_2_INIT ?= /bin/sh -l
# End of NixOS settings

# ISO installer settings
AUTO_INSTALL ?= true
# End of ISO installer settings

# Cachix binary cache settings
CACHIX_AUTH_TOKEN ?=
RELEASE_CACHIX_NAME ?= "aster-nixos-release"
RELEASE_SUBSTITUTER ?= https://aster-nixos-release.cachix.org
RELEASE_TRUSTED_PUBLIC_KEY ?= aster-nixos-release.cachix.org-1:xB6U/f5ck5vGDJZ04kPp3zGpZ4Nro9X4+TSSMAETVFE=
DEV_CACHIX_NAME ?= "aster-nixos-dev"
DEV_SUBSTITUTER ?= https://aster-nixos-dev.cachix.org
DEV_TRUSTED_PUBLIC_KEY ?= aster-nixos-dev.cachix.org-1:xrCbE2flfliFTQCY/2HeJoT2tCO+5kMTZeLIUH9lnIA=
# End of Cachix binary cache settings

# ========================= End of Makefile options. ==========================

export OSDK_TARGET_ARCH=$(TARGET_ARCH)

SHELL := /bin/bash

CARGO_OSDK := ~/.cargo/bin/cargo-osdk

# Common arguments for `cargo osdk` `build`, `run` and `test` commands.
CARGO_OSDK_COMMON_ARGS :=
# The build arguments also apply to the `cargo osdk run` command.
CARGO_OSDK_BUILD_ARGS := --kcmd-args="loglevel=$(LOG_LEVEL)"
CARGO_OSDK_BUILD_ARGS += --kcmd-args="earlycon"
CARGO_OSDK_BUILD_ARGS += --kcmd-args="console=$(CONSOLE)"
ifneq ($(strip $(CMDLINE)),)
CARGO_OSDK_BUILD_ARGS += $(foreach arg,$(CMDLINE),--kcmd-args="$(arg)")
endif
ifneq ($(strip $(EXT4_JOURNAL_FAULT_STAGE)),)
CARGO_OSDK_BUILD_ARGS += --kcmd-args="asterinas.ext4_fault_stage=$(EXT4_JOURNAL_FAULT_STAGE)"
endif
CARGO_OSDK_TEST_ARGS :=

ifeq ($(AUTO_TEST), conformance)
ENABLE_CONFORMANCE_TEST := true
CARGO_OSDK_BUILD_ARGS += --kcmd-args="CONFORMANCE_TEST_SUITE=$(CONFORMANCE_TEST_SUITE)"
CARGO_OSDK_BUILD_ARGS += --kcmd-args="CONFORMANCE_TEST_WORKDIR=$(CONFORMANCE_TEST_WORKDIR)"
ifneq ($(strip $(CONFORMANCE_TEST_SELECTOR)),)
CARGO_OSDK_BUILD_ARGS += --kcmd-args="CONFORMANCE_TEST_SELECTOR=$(CONFORMANCE_TEST_SELECTOR)"
endif
CARGO_OSDK_BUILD_ARGS += --kcmd-args="EXTRA_BLOCKLISTS=$(EXTRA_BLOCKLISTS)"
ifeq ($(CONFORMANCE_TEST_SUITE), xfstests)
CARGO_OSDK_BUILD_ARGS += --kcmd-args="XFSTESTS_RUNLIST=$(XFSTESTS_RUNLIST)"
CARGO_OSDK_BUILD_ARGS += --kcmd-args="XFSTESTS_TEST_DEV=$(XFSTESTS_TEST_DEV)"
CARGO_OSDK_BUILD_ARGS += --kcmd-args="XFSTESTS_SCRATCH_DEV=$(XFSTESTS_SCRATCH_DEV)"
endif
CARGO_OSDK_BUILD_ARGS += --init-args="/opt/run_conformance_test.sh"
else ifeq ($(AUTO_TEST), ifconf_gvisor)
ifneq ($(TARGET_ARCH), x86_64)
$(error AUTO_TEST=ifconf_gvisor requires TARGET_ARCH=x86_64)
endif
ENABLE_CONFORMANCE_TEST := true
CONFORMANCE_TEST_SUITE := gvisor
CONFORMANCE_TEST_SELECTOR := ioctl_test
export ENABLE_CONFORMANCE_TEST CONFORMANCE_TEST_SUITE CONFORMANCE_TEST_SELECTOR
CARGO_OSDK_BUILD_ARGS += --init-args="/opt/run_ifconf_gvisor_test.sh"
else ifeq ($(AUTO_TEST), regression)
ENABLE_REGRESSION_TEST := true
CARGO_OSDK_BUILD_ARGS += --kcmd-args="INTEL_TDX=$(INTEL_TDX)"
ifeq ($(RISCV_ICACHE_REQUIRE_SMP4), 1)
CARGO_OSDK_BUILD_ARGS += --kcmd-args="RISCV_ICACHE_REQUIRE_SMP4=1"
endif
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_regression_test.sh"
ifeq ($(TARGET_ARCH), riscv64)
ifneq ($(findstring network,$(REGRESSION_TEST_DIRS)),)
# The network suite expects eth0, which the default RISC-V QEMU scheme lacks.
CARGO_OSDK_BUILD_ARGS += --qemu-args="-netdev user,id=regression" \
	--qemu-args="-device virtio-net-device,netdev=regression"
endif
endif
else ifeq ($(AUTO_TEST), ext2_firefox_recovery)
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "fs" ]
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_ext2_firefox_recovery_test.sh"
else ifeq ($(AUTO_TEST), ext2_after_sync_cut)
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "fs" ]
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_ext2_after_sync_cut_test.sh"
else ifeq ($(AUTO_TEST), ext2_file_fsync_cut)
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "fs" ]
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_ext2_cut_point_test.sh file"
else ifeq ($(AUTO_TEST), ext2_directory_fsync_cut)
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "fs" ]
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_ext2_cut_point_test.sh directory"
else ifeq ($(AUTO_TEST), ext2_flush_eio)
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "fs" ]
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_ext2_flush_eio_test.sh"
else ifeq ($(AUTO_TEST), ext2_msync_eio)
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "fs" ]
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_ext2_msync_eio_test.sh"
else ifeq ($(AUTO_TEST), xfstests_ext4)
ENABLE_CONFORMANCE_TEST := true
CONFORMANCE_TEST_SUITE := xfstests
XFSTESTS_RUNLIST := /opt/xfstests/ext4-pr.list
XFSTESTS_FS_TYPE := ext4
CARGO_OSDK_BUILD_ARGS += --kcmd-args="CONFORMANCE_TEST_SUITE=$(CONFORMANCE_TEST_SUITE)"
CARGO_OSDK_BUILD_ARGS += --kcmd-args="XFSTESTS_RUNLIST=$(XFSTESTS_RUNLIST)"
CARGO_OSDK_BUILD_ARGS += --kcmd-args="XFSTESTS_FS_TYPE=$(XFSTESTS_FS_TYPE)"
CARGO_OSDK_BUILD_ARGS += --kcmd-args="XFSTESTS_TEST_DEV=$(XFSTESTS_TEST_DEV)"
CARGO_OSDK_BUILD_ARGS += --kcmd-args="XFSTESTS_SCRATCH_DEV=$(XFSTESTS_SCRATCH_DEV)"
CARGO_OSDK_BUILD_ARGS += --init-args="/opt/run_conformance_test.sh"
else ifeq ($(AUTO_TEST), ext4_regression)
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "fs" ]
EXT2_FS_TYPE := ext4
CARGO_OSDK_BUILD_ARGS += --kcmd-args="rootfs_type=ext4"
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_ext4_regression_test.sh"
else ifeq ($(AUTO_TEST), ext4_directory_journal)
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "fs" ]
EXT2_FS_TYPE := ext4
CARGO_OSDK_BUILD_ARGS += --kcmd-args="rootfs_type=ext4"
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_ext4_directory_journal_test.sh"
else ifeq ($(AUTO_TEST), ext4_concurrency)
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "fs" ]
EXT2_FS_TYPE := ext4
CARGO_OSDK_BUILD_ARGS += --kcmd-args="rootfs_type=ext4"
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_ext4_concurrency_test.sh"
else ifeq ($(AUTO_TEST), ext4_semantics)
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "fs" ]
EXT2_FS_TYPE := ext4
CARGO_OSDK_BUILD_ARGS += --kcmd-args="rootfs_type=ext4"
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_ext4_semantics_test.sh"
else ifeq ($(AUTO_TEST), debian_apt_smoke)
export ASTERINAS_EXT2_DRIVE_FILE := $(DEBIAN_ROOT_IMAGE)
DEBIAN_APT_SMOKE_PRECHECK = @test -n "$(DEBIAN_ROOT_IMAGE)" && test -f "$(DEBIAN_ROOT_IMAGE)" || { echo "DEBIAN_ROOT_IMAGE must name an existing Debian root image for AUTO_TEST=debian_apt_smoke" >&2; exit 2; }
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "fs" ]
EXT2_FS_TYPE := ext4
CARGO_OSDK_BUILD_ARGS += --kcmd-args="rootfs_type=ext4"
CARGO_OSDK_BUILD_ARGS += --kcmd-args="asterinas.net=Virtio-Net,10.0.2.15/24,10.0.2.2"
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_debian_apt_smoke_test.sh"
else ifeq ($(AUTO_TEST), ext4_uncommitted_cut)
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "fs" ]
EXT2_FS_TYPE := ext4
CARGO_OSDK_BUILD_ARGS += --kcmd-args="rootfs_type=ext4"
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_ext4_uncommitted_cut_test.sh"
else ifeq ($(AUTO_TEST), ext4_recovery_verify)
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "fs" ]
EXT2_FS_TYPE := ext4
CARGO_OSDK_BUILD_ARGS += --kcmd-args="rootfs_type=ext4"
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_ext4_recovery_verify_test.sh"
else ifeq ($(AUTO_TEST), exfat_syncfs_eio)
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "fs" ]
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_exfat_flush_eio_test.sh syncfs"
else ifeq ($(AUTO_TEST), exfat_fsync_eio)
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "fs" ]
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_exfat_flush_eio_test.sh fsync"
else ifeq ($(AUTO_TEST), exfat_sync_clean)
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "fs" ]
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_exfat_flush_eio_test.sh clean"
else ifeq ($(AUTO_TEST), exfat_sync_verify)
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "fs" ]
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_exfat_flush_eio_test.sh verify"
else ifeq ($(AUTO_TEST), ext2_rename_same_inode)
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "fs" ]
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_ext2_rename_same_inode_test.sh"
else ifeq ($(AUTO_TEST), fs_syscall_compat)
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "fs" ]
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_fs_syscall_compat_test.sh"
else ifeq ($(AUTO_TEST), ipv6_dual_stack)
ENABLE_REGRESSION_TEST := true
CARGO_OSDK_BUILD_ARGS += --init-args="/test/network/run_dual_stack_test.sh"
else ifeq ($(AUTO_TEST), ipv6_dual_stack_udp)
ENABLE_REGRESSION_TEST := true
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_ipv6_dual_stack_udp_test.sh"
else ifeq ($(AUTO_TEST), ipv6_udp)
ENABLE_REGRESSION_TEST := true
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_ipv6_udp_test.sh"
else ifeq ($(AUTO_TEST), ifconf)
ENABLE_REGRESSION_TEST := true
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_ifconf_test.sh"
else ifeq ($(AUTO_TEST), ifreq)
ENABLE_REGRESSION_TEST := true
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_ifreq_test.sh"
ifeq ($(TARGET_ARCH), riscv64)
# The default RISC-V QEMU scheme has no NIC; this test also checks eth0.
CARGO_OSDK_BUILD_ARGS += --qemu-args="-netdev user,id=ifreq" \
	--qemu-args="-device virtio-net-device,netdev=ifreq"
endif
else ifeq ($(AUTO_TEST), ip_socket_netns)
ENABLE_REGRESSION_TEST := true
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_ip_socket_netns_test.sh"
ifeq ($(TARGET_ARCH), riscv64)
# The namespace regression binds an address on the original eth0.
CARGO_OSDK_BUILD_ARGS += --qemu-args="-netdev user,id=ipsocketns" \
	--qemu-args="-device virtio-net-device,netdev=ipsocketns"
endif
else ifeq ($(AUTO_TEST), netlink_route_netns)
ENABLE_REGRESSION_TEST := true
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_netlink_route_netns_test.sh"
ifeq ($(TARGET_ARCH), riscv64)
CARGO_OSDK_BUILD_ARGS += --qemu-args="-netdev user,id=routens" \
	--qemu-args="-device virtio-net-device,netdev=routens"
endif
else ifeq ($(AUTO_TEST), proc_net_dev)
ENABLE_REGRESSION_TEST := true
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_proc_net_dev_test.sh"
ifeq ($(TARGET_ARCH), riscv64)
# The default RISC-V QEMU scheme has no NIC; this test also checks eth0.
CARGO_OSDK_BUILD_ARGS += --qemu-args="-netdev user,id=procnetdev" \
	--qemu-args="-device virtio-net-device,netdev=procnetdev"
endif
else ifeq ($(AUTO_TEST), udp_user_buffer_prefault)
ENABLE_REGRESSION_TEST := true
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_udp_user_buffer_prefault_test.sh"
else ifeq ($(AUTO_TEST), udp_msg_dontwait)
ENABLE_REGRESSION_TEST := true
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_udp_msg_dontwait_test.sh"
else ifeq ($(AUTO_TEST), tcp_msg_dontwait_send)
ENABLE_REGRESSION_TEST := true
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_tcp_msg_dontwait_send_test.sh"
else ifeq ($(AUTO_TEST), dynamic_clock)
ENABLE_REGRESSION_TEST := true
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_dynamic_clock_test.sh"
else ifeq ($(AUTO_TEST), pty)
ENABLE_REGRESSION_TEST := true
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_pty_test.sh"
else ifeq ($(AUTO_TEST), memfd_exec)
ENABLE_REGRESSION_TEST := true
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_memfd_exec_test.sh"
else ifeq ($(AUTO_TEST), shebang_argv)
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "process" ]
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_shebang_argv_test.sh"
else ifeq ($(AUTO_TEST), sched_policy)
ENABLE_REGRESSION_TEST := true
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_sched_policy_test.sh"
else ifeq ($(AUTO_TEST), schedstat)
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "fs" ]
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_schedstat_test.sh"
else ifeq ($(AUTO_TEST), kcmp)
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "process" ]
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_kcmp_test.sh"
else ifeq ($(AUTO_TEST), riscv_ptrace_regset)
ifneq ($(TARGET_ARCH), riscv64)
$(error AUTO_TEST=riscv_ptrace_regset requires TARGET_ARCH=riscv64)
endif
ENABLE_REGRESSION_TEST := true
REGRESSION_TEST_DIRS := [ "process" ]
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_riscv_ptrace_regset_test.sh"
else ifeq ($(AUTO_TEST), riscv_icache_smp4)
ifneq ($(TARGET_ARCH), riscv64)
$(error AUTO_TEST=riscv_icache_smp4 requires TARGET_ARCH=riscv64)
endif
ifneq ($(SMP), 4)
$(error AUTO_TEST=riscv_icache_smp4 requires SMP=4)
endif
ENABLE_REGRESSION_TEST := true
# Keep this gate independent of QEMU's incomplete Zkr seed-CSR emulation.
RISCV_QEMU_CPU := rv64,svpbmt=true,zkr=false
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_riscv_icache_smp4_test.sh"
else ifeq ($(AUTO_TEST), tcp_user_buffer_prefault)
ENABLE_REGRESSION_TEST := true
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_tcp_user_buffer_prefault_test.sh"
else ifeq ($(AUTO_TEST), tcp_ppoll_wakeup)
ENABLE_REGRESSION_TEST := true
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_tcp_ppoll_wakeup_test.sh"
else ifeq ($(AUTO_TEST), tcp_event_handoff)
ENABLE_REGRESSION_TEST := true
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_tcp_event_handoff_test.sh"
else ifeq ($(AUTO_TEST), pthread_cond_handoff)
ENABLE_REGRESSION_TEST := true
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_pthread_cond_handoff_test.sh"
else ifeq ($(AUTO_TEST), sysv_shm)
ENABLE_REGRESSION_TEST := true
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_sysv_shm_test.sh"
else ifeq ($(AUTO_TEST), boot)
CARGO_OSDK_BUILD_ARGS += --init-args="/test/boot_hello.sh"
else ifeq ($(AUTO_TEST), vsock)
ENABLE_REGRESSION_TEST := true
export VSOCK=on
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_vsock_test.sh"
endif

ifeq ($(RELEASE_LTO), 1)
CARGO_OSDK_COMMON_ARGS += --profile release-lto
OSTD_TASK_STACK_SIZE_IN_PAGES = 8
else ifeq ($(RELEASE), 1)
CARGO_OSDK_COMMON_ARGS += --release
	ifeq ($(TARGET_ARCH), riscv64)
	# FIXME: Unwinding in RISC-V seems to cost more stack space, so we increase
	# the stack size for it. This may need further investigation.
	# See https://github.com/asterinas/asterinas/pull/2383#discussion_r2307673156
	OSTD_TASK_STACK_SIZE_IN_PAGES = 16
	else
	OSTD_TASK_STACK_SIZE_IN_PAGES = 8
	endif
endif

# If the BENCHMARK is set, we will run the benchmark in the kernel mode.
ifneq ($(BENCHMARK), none)
CARGO_OSDK_BUILD_ARGS += --init-args="/benchmark/common/bench_runner.sh $(BENCHMARK) asterinas"
endif

ifeq ($(INTEL_TDX), 1)
BOOT_PROTOCOL = linux-efi-handover64
CARGO_OSDK_COMMON_ARGS += --scheme tdx
endif

ifeq ($(BOOT_PROTOCOL), multiboot)
BOOT_METHOD = qemu-direct
endif

ifeq ($(SCHEME), microvm)
BOOT_METHOD = qemu-direct
endif

ifeq ($(SCHEME), "")
	ifeq ($(TARGET_ARCH), riscv64)
	SCHEME = riscv
	else ifeq ($(TARGET_ARCH), loongarch64)
	SCHEME = loongarch
	endif
endif

# The RISC-V scheme boots a Linux-format Image through U-Boot in the Debian
# and QEMU workflows.  The historical multiboot2 default selects an ELF-only
# build and leaves a previously generated `.Image` stale, which can make a
# runtime gate execute an older kernel than the source tree.  Keep an explicit
# user override, but make the normal RISC-V scheme produce the artifact that
# its consumers actually load.
ifeq ($(TARGET_ARCH),riscv64)
ifeq ($(SCHEME),riscv)
ifeq ($(BOOT_PROTOCOL),multiboot2)
BOOT_PROTOCOL := linux
endif
endif
endif

ifneq ($(SCHEME), "")
CARGO_OSDK_COMMON_ARGS += --scheme $(SCHEME)
else
CARGO_OSDK_COMMON_ARGS += --boot-method="$(BOOT_METHOD)"
endif

ifeq ($(COVERAGE), 1)
CARGO_OSDK_COMMON_ARGS += --coverage
endif

ifdef FEATURES
CARGO_OSDK_COMMON_ARGS += --features="$(FEATURES)"
endif
ifeq ($(NO_DEFAULT_FEATURES), 1)
CARGO_OSDK_COMMON_ARGS += --no-default-features
endif

# To test the linux-efi-handover64 boot protocol, we need to use Debian's
# GRUB release, which is installed in /usr/bin in our Docker image.
ifeq ($(BOOT_PROTOCOL), linux-efi-handover64)
CARGO_OSDK_COMMON_ARGS += --grub-mkrescue=/usr/bin/grub-mkrescue --grub-boot-protocol="linux"
else ifeq ($(BOOT_PROTOCOL), linux-efi-pe64)
CARGO_OSDK_COMMON_ARGS += --grub-boot-protocol="linux"
else ifeq ($(BOOT_PROTOCOL), linux-legacy32)
CARGO_OSDK_COMMON_ARGS += --linux-x86-legacy-boot --grub-boot-protocol="linux" --strip-elf
else
CARGO_OSDK_COMMON_ARGS += --grub-boot-protocol=$(BOOT_PROTOCOL)
endif

ifeq ($(ENABLE_KVM), 1)
	ifeq ($(TARGET_ARCH), x86_64)
	CARGO_OSDK_COMMON_ARGS += --qemu-args="-accel kvm"
	endif
endif

# Select the initramfs image explicitly so `cargo osdk run` cannot silently
# fall back to a root-filesystem-only boot when the test image is compressed.
ifeq ($(INITRAMFS_SKIP_GZIP),1)
CARGO_OSDK_INITRAMFS_OPTION := --initramfs=$(abspath test/initramfs/build/initramfs.cpio)
CARGO_OSDK_COMMON_ARGS += $(CARGO_OSDK_INITRAMFS_OPTION)
else
CARGO_OSDK_INITRAMFS_OPTION := --initramfs=$(abspath test/initramfs/build/initramfs.cpio.gz)
CARGO_OSDK_COMMON_ARGS += $(CARGO_OSDK_INITRAMFS_OPTION)
endif

CARGO_OSDK_BUILD_ARGS += $(CARGO_OSDK_COMMON_ARGS)
CARGO_OSDK_TEST_ARGS += $(CARGO_OSDK_COMMON_ARGS)

# Pass make variables to all subdirectory makes
export

# OSDK dependencies
OSDK_SRC_FILES := \
	$(shell find osdk/Cargo.toml osdk/Cargo.lock osdk/src -type f)

.PHONY: all
all: kernel

# Install or update OSDK from source
# To uninstall, do `cargo uninstall cargo-osdk`
.PHONY: install_osdk
install_osdk:
	@# The `OSDK_LOCAL_DEV` environment variable is used for local development
	@# without the need to publish the changes of OSDK's self-hosted
	@# dependencies to `crates.io`.
	@OSDK_LOCAL_DEV=1 cargo install cargo-osdk --locked --path osdk

# This will install and update OSDK automatically
$(CARGO_OSDK): $(OSDK_SRC_FILES)
	@$(MAKE) --no-print-directory install_osdk

.PHONY: check_osdk
check_osdk:
	@./tools/clippy_check.sh osdk

.PHONY: test_osdk
test_osdk:
	@cd osdk && \
		OSDK_LOCAL_DEV=1 cargo build && \
		OSDK_LOCAL_DEV=1 cargo test

QEMU_UBOOT_OUT_DIR ?= $(CURDIR)/target/qemu-uboot/current
QEMU_UBOOT_BUILD_DIR ?= $(CURDIR)/target/qemu-uboot/cache/u-boot-build
RISCV_SIFIVE_U_OUT_DIR ?= $(CURDIR)/target/qemu-uboot/sifive-u
RISCV_SIFIVE_U_LINUX_OUT_DIR ?= $(CURDIR)/target/qemu-uboot/sifive-u-linux
RISCV_SIFIVE_U_BUILD_DIR ?= $(CURDIR)/target/qemu-uboot/cache/sifive-u-uboot-build
MEGREZ_DEBUG_FAST_OUT_DIR ?= $(CURDIR)/target/qemu-uboot/megrez-debug/fast
MEGREZ_DEBUG_UBOOT_BUILD_DIR ?= $(CURDIR)/target/qemu-uboot/megrez-debug/uboot
MEGREZ_DEBUG_BOARD_OUT_DIR ?= $(CURDIR)/target/megrez-debug/board
MEGREZ_DEBUG_BOARD_TIMEOUT ?= 300
RISCV_PHYSICAL_GRAPHICS_QEMU_GATE_OUTPUT ?=
MEGREZ_PHYSICAL_GRAPHICS_PLAN ?=
MEGREZ_PHYSICAL_GRAPHICS_DEVICE ?=
MEGREZ_PHYSICAL_GRAPHICS_DISPLAY ?= hdmi
MEGREZ_PHYSICAL_GRAPHICS_CYCLES ?= 3
MEGREZ_PHYSICAL_GRAPHICS_HDMI_CAPTURE ?=
MEGREZ_PHYSICAL_GRAPHICS_MMC_KERNEL ?=
MEGREZ_PHYSICAL_GRAPHICS_MMC_INITRAMFS ?=
MEGREZ_PHYSICAL_GRAPHICS_MMC_DTB ?=
MEGREZ_PHYSICAL_GRAPHICS_OUTPUT ?= $(CURDIR)/target/current-main-physical-graphics/physical/evidence
MEGREZ_FIREFOX_DAILY_USE_PLAN ?=
MEGREZ_FIREFOX_DAILY_USE_DEVICE ?=
MEGREZ_FIREFOX_DAILY_USE_OUTPUT ?= $(CURDIR)/target/firefox-daily-use-physical/run-a1
MEGREZ_FIREFOX_DAILY_USE_FIXTURE_BIND ?= 10.100.19.216
MEGREZ_FIREFOX_DAILY_USE_FIXTURE_PORT ?= 17894
MEGREZ_FIREFOX_DAILY_USE_BOARD_PEER ?= 10.100.19.200
MEGREZ_FIREFOX_DAILY_USE_MMC_KERNEL ?=
MEGREZ_FIREFOX_DAILY_USE_MMC_INITRAMFS ?=
MEGREZ_FIREFOX_DAILY_USE_MMC_DTB ?=
MEGREZ_DESKTOP_BOOT_PLAN ?= $(CURDIR)/target/megrez-desktop-boot/build/plan.json
MEGREZ_DESKTOP_BOOT_DEVICE ?= /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0
MEGREZ_DESKTOP_BOOT_DEVICE_PROFILE ?= full
MEGREZ_DESKTOP_BOOT_HOST_ADDRESS ?= 10.100.19.216
MEGREZ_DESKTOP_BOOT_PORT ?= 18080
MEGREZ_DESKTOP_BOOT_OUTPUT_ROOT ?= $(CURDIR)/target/megrez-desktop-boot
MEGREZ_DESKTOP_BOOT_RUN_ID ?= $(shell date -u +%Y%m%dT%H%M%SZ)
MEGREZ_DESKTOP_BOOT_PREPARE_OUTPUT ?= $(MEGREZ_DESKTOP_BOOT_OUTPUT_ROOT)/prepare-$(MEGREZ_DESKTOP_BOOT_RUN_ID)
MEGREZ_DESKTOP_BOOT_START_OUTPUT ?= $(MEGREZ_DESKTOP_BOOT_OUTPUT_ROOT)/start-$(MEGREZ_DESKTOP_BOOT_RUN_ID)
DEBIAN_DESKTOP_BOOT_TIMEOUT ?= 420
DEBIAN_DESKTOP_M5_QEMU_GATE_TARGET ?= browser
DEBIAN_WEB_NETWORK_MODE ?=
DEBIAN_WEB_NETWORK_EXPECT_FAILURE ?= none
RISCV_ROOTFS_BASE_IMAGE ?= asterinas/asterinas:0.18.0-20260702
RISCV_ROOTFS_IMAGE ?= asterinas/asterinas:0.18.0-20260702-riscv-rootfs
RISCV_ROOTFS_DOCKERFILE ?= tools/docker/riscv-rootfs/Dockerfile

effective_path = $(abspath $(or $(strip $(1)),$(2)))
QEMU_UBOOT_OUT_DIR_EFFECTIVE := $(call effective_path,$(QEMU_UBOOT_OUT_DIR),$(CURDIR)/target/qemu-uboot/current)
QEMU_UBOOT_BUILD_DIR_EFFECTIVE := $(call effective_path,$(QEMU_UBOOT_BUILD_DIR),$(CURDIR)/target/qemu-uboot/cache/u-boot-build)
RISCV_SIFIVE_U_OUT_DIR_EFFECTIVE := $(call effective_path,$(RISCV_SIFIVE_U_OUT_DIR),$(CURDIR)/target/qemu-uboot/sifive-u)
RISCV_SIFIVE_U_LINUX_OUT_DIR_EFFECTIVE := $(call effective_path,$(RISCV_SIFIVE_U_LINUX_OUT_DIR),$(CURDIR)/target/qemu-uboot/sifive-u-linux)
RISCV_SIFIVE_U_BUILD_DIR_EFFECTIVE := $(call effective_path,$(RISCV_SIFIVE_U_BUILD_DIR),$(CURDIR)/target/qemu-uboot/cache/sifive-u-uboot-build)
MEGREZ_DEBUG_FAST_OUT_DIR_EFFECTIVE := $(call effective_path,$(MEGREZ_DEBUG_FAST_OUT_DIR),$(CURDIR)/target/qemu-uboot/megrez-debug/fast)
MEGREZ_DEBUG_UBOOT_BUILD_DIR_EFFECTIVE := $(call effective_path,$(MEGREZ_DEBUG_UBOOT_BUILD_DIR),$(CURDIR)/target/qemu-uboot/megrez-debug/uboot)
MEGREZ_DEBUG_BOARD_OUT_DIR_EFFECTIVE := $(call effective_path,$(MEGREZ_DEBUG_BOARD_OUT_DIR),$(CURDIR)/target/megrez-debug/board)

.PHONY: build_riscv_rootfs_image
build_riscv_rootfs_image:
	@docker build --pull=false \
		--build-arg BASE_IMAGE="$(RISCV_ROOTFS_BASE_IMAGE)" \
		-f "$(RISCV_ROOTFS_DOCKERFILE)" \
		-t "$(RISCV_ROOTFS_IMAGE)" .

.PHONY: test_riscv_ltp_unit
test_riscv_ltp_unit:
	@PYTHONPATH=tools/riscv python3 -m unittest \
		tools.riscv.tests.test_ltp_result \
		tools.riscv.tests.test_ltp_manifest \
		tools.riscv.tests.test_ltp_suite \
		tools.riscv.tests.test_ltp_package \
		tools.riscv.tests.test_ltp_gate \
		tools.riscv.tests.test_ltp_guest_runner -v

.PHONY: test_riscv_debian_rootfs_unit
test_riscv_debian_rootfs_unit:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_debian_dev_overlay \
		tools.riscv.tests.test_debian_rootfs \
		tools.riscv.tests.test_debian_m5_network \
		tools.riscv.tests.test_debian_m6_browser \
		tools.riscv.tests.test_debian_m7_baidu \
		tools.riscv.tests.test_debian_m8_browser_quality \
		tools.riscv.tests.test_debian_m9_software -v

.PHONY: test_riscv_debian_debug_console
test_riscv_debian_debug_console:
	@python3 -m unittest \
		tools.riscv.tests.test_debian_debug_console \
		tools.riscv.tests.test_debian_rootfs.DebianStage1Tests -v

.PHONY: build_riscv_debian_browser_web_dev_overlay
build_riscv_debian_browser_web_dev_overlay:
	@python3 -m tools.debian.rootfs.dev_overlay materialize \
		--base-dir "$(DEBIAN_BROWSER_WEB_BASE_ROOTFS)" \
		--spec "$(DEBIAN_BROWSER_WEB_DEV_OVERLAY_SPEC)" \
		--output-dir "$(DEBIAN_BROWSER_WEB_DEV_ROOTFS)"

.PHONY: test_riscv_megrez_debian_shell
test_riscv_megrez_debian_shell:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_megrez_debian_shell -v

.PHONY: test_riscv_rootfs_builder_image_unit
test_riscv_rootfs_builder_image_unit:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_riscv_rootfs_builder_image -v

.PHONY: test_x86_debian_tooling_unit
test_x86_debian_tooling_unit:
	@python3 -W error::ResourceWarning -m unittest \
		tools.x86.tests.test_x86_debian_boot \
		tools.x86.tests.test_x86_debian_apt \
		tools.x86.tests.test_x86_debian_fs_acl \
		tools.x86.tests.test_x86_debian_fs_crash \
		tools.x86.tests.test_x86_debian_fs_concurrency \
		tools.x86.tests.test_x86_debian_fs_fallocate \
		tools.x86.tests.test_x86_debian_fs_durability \
		tools.x86.tests.test_x86_debian_fs_ext4_features \
		tools.x86.tests.test_x86_debian_fs_metadata_csum \
		tools.x86.tests.test_x86_debian_fs_extents \
		tools.x86.tests.test_x86_debian_fs_punch_hole \
		tools.x86.tests.test_x86_debian_fs_renameat2 \
		tools.x86.tests.test_x86_debian_fs_timestamps \
		tools.x86.tests.test_lmbench_transcript \
		tools.riscv.tests.test_lmbench_native -v

# Boot the Debian root image on the x86_64 kernel and wait for the marker.
# X86_DEBIAN_KERNEL must be an OSDK x86_64 artifact built with
# `--boot-method vmm-direct`; X86_DEBIAN_STAGE1_INITRAMFS comes from
# `build_stage1.sh --architecture amd64 --minimal`.
X86_DEBIAN_KERNEL ?=
X86_DEBIAN_STAGE1_INITRAMFS ?=
X86_DEBIAN_ROOT_IMAGE ?=
X86_DEBIAN_BOOT_TRANSCRIPT ?= $(CURDIR)/target/x86-debian/boot.transcript
X86_DEBIAN_BOOT_MARKER ?= Reached target multi-user.target
X86_DEBIAN_BOOT_TIMEOUT ?= 1800

.PHONY: test_x86_debian_boot
test_x86_debian_boot:
	@test -n "$(X86_DEBIAN_KERNEL)" || { echo "X86_DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_STAGE1_INITRAMFS)" || { echo "X86_DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_ROOT_IMAGE)" || { echo "X86_DEBIAN_ROOT_IMAGE is required" >&2; exit 2; }
	@tools/x86/debian/boot_debian.sh \
		--kernel "$(X86_DEBIAN_KERNEL)" \
		--initramfs "$(X86_DEBIAN_STAGE1_INITRAMFS)" \
		--root-image "$(X86_DEBIAN_ROOT_IMAGE)" \
		--output "$(X86_DEBIAN_BOOT_TRANSCRIPT)" \
		--wait-for "$(X86_DEBIAN_BOOT_MARKER)" \
		--timeout "$(X86_DEBIAN_BOOT_TIMEOUT)"

# Run the native LMbench ALL suite inside the Debian guest and wait for the
# runner's completion marker. The transcript records the per-run report; the
# parameters below are echoed so a result can always be attributed.
#   X86_DEBIAN_LMBENCH_IMAGE  ext4 image prepared by prepare_lmbench_disk.sh
#   X86_DEBIAN_SMP            vCPU count (4 matches the published numbers)
#   X86_DEBIAN_ACCEL          kvm or tcg
#   X86_DEBIAN_LMBENCH_QEMU   optional wrapper around qemu-system-x86_64, used
#                             to pin the vCPUs (see the README's host section)
X86_DEBIAN_LMBENCH_IMAGE ?= $(CURDIR)/target/x86-debian/m4/lmbench2.ext2
X86_DEBIAN_LMBENCH_TRANSCRIPT ?= $(CURDIR)/target/x86-debian/lmbench.transcript
X86_DEBIAN_SMP ?= 4
X86_DEBIAN_ACCEL ?= kvm
X86_DEBIAN_LMBENCH_QEMU ?=
X86_DEBIAN_LMBENCH_TIMEOUT ?= 2400

.PHONY: test_x86_debian_lmbench
test_x86_debian_lmbench:
	@test -n "$(X86_DEBIAN_KERNEL)" || { echo "X86_DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_STAGE1_INITRAMFS)" || { echo "X86_DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -f "$(X86_DEBIAN_LMBENCH_IMAGE)" || { echo "missing $(X86_DEBIAN_LMBENCH_IMAGE)" >&2; exit 2; }
	@echo "lmbench: image=$(X86_DEBIAN_LMBENCH_IMAGE) smp=$(X86_DEBIAN_SMP) accel=$(X86_DEBIAN_ACCEL) loglevel=4 qemu=$(if $(X86_DEBIAN_LMBENCH_QEMU),$(X86_DEBIAN_LMBENCH_QEMU),default)"
	@e2fsck -fy "$(X86_DEBIAN_LMBENCH_IMAGE)" >/dev/null 2>&1 || true
	@tools/x86/debian/boot_debian.sh \
		--accel "$(X86_DEBIAN_ACCEL)" \
		--kernel "$(X86_DEBIAN_KERNEL)" \
		--initramfs "$(X86_DEBIAN_STAGE1_INITRAMFS)" \
		--root-image "$(X86_DEBIAN_LMBENCH_IMAGE)" \
		--output "$(X86_DEBIAN_LMBENCH_TRANSCRIPT)" \
		--smp "$(X86_DEBIAN_SMP)" \
		--loglevel 4 \
		--wait-for LMBENCH_RUN_DONE \
		$(if $(X86_DEBIAN_LMBENCH_QEMU),--qemu "$(X86_DEBIAN_LMBENCH_QEMU)",) \
		--timeout "$(X86_DEBIAN_LMBENCH_TIMEOUT)"
	@echo "lmbench: transcript=$(X86_DEBIAN_LMBENCH_TRANSCRIPT)"

# Rate-based LMbench verdict for one or more transcripts. The guest keeps the
# run directories of earlier boots in the image, so a transcript contains
# stale reports; the parser selects the run the boot itself produced. Pass
# `PREVIOUS=<older transcript>` for the same image lineage to identify that
# run by its new directory instead of by elapsed-time proximity.
#   X86_DEBIAN_LMBENCH_TRANSCRIPTS        transcripts to judge (space separated)
#   X86_DEBIAN_LMBENCH_PREVIOUS          optional preceding transcript
#   X86_DEBIAN_LMBENCH_REQUIRE_RATE      optional minimum green fraction (0..1)
X86_DEBIAN_LMBENCH_TRANSCRIPTS ?= $(X86_DEBIAN_LMBENCH_TRANSCRIPT)
X86_DEBIAN_LMBENCH_PREVIOUS ?=
X86_DEBIAN_LMBENCH_REQUIRE_RATE ?=

.PHONY: test_x86_debian_lmbench_verdict
test_x86_debian_lmbench_verdict:
	@test -n "$(X86_DEBIAN_LMBENCH_TRANSCRIPTS)" || { echo "X86_DEBIAN_LMBENCH_TRANSCRIPTS is required" >&2; exit 2; }
	@python3 tools/x86/debian/lmbench_transcript.py \
		$(if $(X86_DEBIAN_LMBENCH_PREVIOUS),--previous "$(X86_DEBIAN_LMBENCH_PREVIOUS)",) \
		$(if $(X86_DEBIAN_LMBENCH_REQUIRE_RATE),--require-rate "$(X86_DEBIAN_LMBENCH_REQUIRE_RATE)",) \
		$(X86_DEBIAN_LMBENCH_TRANSCRIPTS)

# Storage persistence gate: boot one image repeatedly and verify, from the
# host, that every boot's writes survive, earlier writes stay intact, and the
# image still passes `e2fsck -fn`.
#   X86_DEBIAN_STORAGE_BASE_ROOT  ext4 image copied once into the workspace
#   X86_DEBIAN_STORAGE_WORKSPACE  image, manifests and transcripts
#   X86_DEBIAN_STORAGE_BOOTS      consecutive boots (default 3)
X86_DEBIAN_STORAGE_BASE_ROOT ?=
X86_DEBIAN_STORAGE_WORKSPACE ?= $(CURDIR)/target/x86-debian/storage
X86_DEBIAN_STORAGE_BOOTS ?= 3
X86_DEBIAN_STORAGE_ACCEL ?= kvm
X86_DEBIAN_STORAGE_TIMEOUT ?= 1800

.PHONY: test_x86_debian_storage
test_x86_debian_storage:
	@test -n "$(X86_DEBIAN_KERNEL)" || { echo "X86_DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_STAGE1_INITRAMFS)" || { echo "X86_DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_STORAGE_BASE_ROOT)" || { echo "X86_DEBIAN_STORAGE_BASE_ROOT is required" >&2; exit 2; }
	@echo "storage: base=$(X86_DEBIAN_STORAGE_BASE_ROOT) boots=$(X86_DEBIAN_STORAGE_BOOTS) accel=$(X86_DEBIAN_STORAGE_ACCEL) workspace=$(X86_DEBIAN_STORAGE_WORKSPACE)"
	@tools/x86/debian/persistence_acceptance.sh \
		--kernel "$(X86_DEBIAN_KERNEL)" \
		--initramfs "$(X86_DEBIAN_STAGE1_INITRAMFS)" \
		--base-root "$(X86_DEBIAN_STORAGE_BASE_ROOT)" \
		--workspace "$(X86_DEBIAN_STORAGE_WORKSPACE)" \
		--boots "$(X86_DEBIAN_STORAGE_BOOTS)" \
		--accel "$(X86_DEBIAN_STORAGE_ACCEL)" \
		--timeout "$(X86_DEBIAN_STORAGE_TIMEOUT)"

# Package-management gate: boot once to install a package with apt/dpkg, run
# its binary, shut down, and boot the same image again to prove the package
# database and files survived. `apt_acceptance.sh` checks dpkg's status, the
# command output, the binary checksum and `e2fsck -fn` on every boot.
#   X86_DEBIAN_APT_BASE_ROOT  ext4 image copied once into the workspace
#   X86_DEBIAN_APT_PACKAGE    package to install (default hello)
#   X86_DEBIAN_APT_BOOTS      boots; the first one installs (default 2)
X86_DEBIAN_APT_BASE_ROOT ?=
X86_DEBIAN_APT_WORKSPACE ?= $(CURDIR)/target/x86-debian/apt
X86_DEBIAN_APT_PACKAGE ?= hello
X86_DEBIAN_APT_BOOTS ?= 2
X86_DEBIAN_APT_ACCEL ?= kvm
X86_DEBIAN_APT_TIMEOUT ?= 1800

.PHONY: test_x86_debian_apt
test_x86_debian_apt:
	@test -n "$(X86_DEBIAN_KERNEL)" || { echo "X86_DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_STAGE1_INITRAMFS)" || { echo "X86_DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_APT_BASE_ROOT)" || { echo "X86_DEBIAN_APT_BASE_ROOT is required" >&2; exit 2; }
	@echo "apt: package=$(X86_DEBIAN_APT_PACKAGE) base=$(X86_DEBIAN_APT_BASE_ROOT) boots=$(X86_DEBIAN_APT_BOOTS) accel=$(X86_DEBIAN_APT_ACCEL) workspace=$(X86_DEBIAN_APT_WORKSPACE)"
	@tools/x86/debian/apt_acceptance.sh \
		--kernel "$(X86_DEBIAN_KERNEL)" \
		--initramfs "$(X86_DEBIAN_STAGE1_INITRAMFS)" \
		--base-root "$(X86_DEBIAN_APT_BASE_ROOT)" \
		--workspace "$(X86_DEBIAN_APT_WORKSPACE)" \
		--package "$(X86_DEBIAN_APT_PACKAGE)" \
		--boots "$(X86_DEBIAN_APT_BOOTS)" \
		--accel "$(X86_DEBIAN_APT_ACCEL)" \
		--timeout "$(X86_DEBIAN_APT_TIMEOUT)"

# Crash-consistency matrix: for each scenario and journal commit stage, crash
# the first metadata commit of a probe boot, replay on the next boot, and
# require the operation's invariant plus `e2fsck -fn` to hold.
#   X86_DEBIAN_CRASH_BASE_ROOT  ext4 image copied per cell
#   X86_DEBIAN_CRASH_SCENARIOS   subset of: create unlink rename truncate
#   X86_DEBIAN_CRASH_STAGES      journal stages (default 1 2 3)
X86_DEBIAN_CRASH_BASE_ROOT ?=
X86_DEBIAN_CRASH_WORKSPACE ?= $(CURDIR)/target/x86-debian/crash
X86_DEBIAN_CRASH_SCENARIOS ?= create unlink rename truncate
X86_DEBIAN_CRASH_STAGES ?= 1 2 3
X86_DEBIAN_CRASH_ACCEL ?= kvm
X86_DEBIAN_CRASH_TIMEOUT ?= 900

.PHONY: test_x86_debian_fs_crash
test_x86_debian_fs_crash:
	@test -n "$(X86_DEBIAN_KERNEL)" || { echo "X86_DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_STAGE1_INITRAMFS)" || { echo "X86_DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_CRASH_BASE_ROOT)" || { echo "X86_DEBIAN_CRASH_BASE_ROOT is required" >&2; exit 2; }
	@echo "fs-crash: base=$(X86_DEBIAN_CRASH_BASE_ROOT) scenarios=$(X86_DEBIAN_CRASH_SCENARIOS) stages=$(X86_DEBIAN_CRASH_STAGES) accel=$(X86_DEBIAN_CRASH_ACCEL)"
	@tools/x86/debian/fs_crash_matrix.sh \
		--kernel "$(X86_DEBIAN_KERNEL)" \
		--initramfs "$(X86_DEBIAN_STAGE1_INITRAMFS)" \
		--base-root "$(X86_DEBIAN_CRASH_BASE_ROOT)" \
		--workspace "$(X86_DEBIAN_CRASH_WORKSPACE)" \
		--scenarios "$(X86_DEBIAN_CRASH_SCENARIOS)" \
		--stages "$(X86_DEBIAN_CRASH_STAGES)" \
		--accel "$(X86_DEBIAN_CRASH_ACCEL)" \
		--timeout "$(X86_DEBIAN_CRASH_TIMEOUT)"

# fsync durability matrix: each scenario performs an operation, makes it
# durable with fsync (and a directory fsync where the operation changes a
# directory), then the host kills QEMU.  After a normal recovery boot the
# operation must be present in full and `e2fsck -fn` must accept the image.
#   X86_DEBIAN_DURABILITY_BASE_ROOT  ext4 image copied per cell
#   X86_DEBIAN_DURABILITY_SCENARIOS  subset of:
#                                    write rename unlink truncate dir-entry
#                                    syncfs o-sync
X86_DEBIAN_DURABILITY_BASE_ROOT ?=
X86_DEBIAN_DURABILITY_WORKSPACE ?= $(CURDIR)/target/x86-debian/durability
X86_DEBIAN_DURABILITY_SCENARIOS ?= write rename unlink truncate dir-entry syncfs o-sync
X86_DEBIAN_DURABILITY_ACCEL ?= kvm
X86_DEBIAN_DURABILITY_TIMEOUT ?= 900

.PHONY: test_x86_debian_fs_durability
test_x86_debian_fs_durability:
	@test -n "$(X86_DEBIAN_KERNEL)" || { echo "X86_DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_STAGE1_INITRAMFS)" || { echo "X86_DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_DURABILITY_BASE_ROOT)" || { echo "X86_DEBIAN_DURABILITY_BASE_ROOT is required" >&2; exit 2; }
	@echo "fs-durability: base=$(X86_DEBIAN_DURABILITY_BASE_ROOT) scenarios=$(X86_DEBIAN_DURABILITY_SCENARIOS) accel=$(X86_DEBIAN_DURABILITY_ACCEL)"
	@tools/x86/debian/fs_durability_matrix.sh \
		--kernel "$(X86_DEBIAN_KERNEL)" \
		--initramfs "$(X86_DEBIAN_STAGE1_INITRAMFS)" \
		--base-root "$(X86_DEBIAN_DURABILITY_BASE_ROOT)" \
		--workspace "$(X86_DEBIAN_DURABILITY_WORKSPACE)" \
		--scenarios "$(X86_DEBIAN_DURABILITY_SCENARIOS)" \
		--accel "$(X86_DEBIAN_DURABILITY_ACCEL)" \
		--timeout "$(X86_DEBIAN_DURABILITY_TIMEOUT)"

# Concurrency stress: N guest workers churn separate directories plus one
# shared directory with create/rewrite/unlink/rename/truncate/xattr, keep an
# on-disk manifest of expected contents, and read files back to catch inode
# reuse.  The in-guest verifier must report ok=True and `e2fsck -fn` must
# accept the image.
#   X86_DEBIAN_CONCURRENCY_BASE_ROOT  ext4 image copied per run
X86_DEBIAN_CONCURRENCY_BASE_ROOT ?=
X86_DEBIAN_CONCURRENCY_WORKSPACE ?= $(CURDIR)/target/x86-debian/concurrency
X86_DEBIAN_CONCURRENCY_WORKERS ?= 8
X86_DEBIAN_CONCURRENCY_SECONDS ?= 45
# Set to a number of seconds to cut power mid-run and require a clean
# recovery instead of the clean-run content verification.
X86_DEBIAN_CONCURRENCY_CRASH_AFTER ?=
X86_DEBIAN_CONCURRENCY_ACCEL ?= kvm
X86_DEBIAN_CONCURRENCY_TIMEOUT ?= 1800

.PHONY: test_x86_debian_fs_concurrency
test_x86_debian_fs_concurrency:
	@test -n "$(X86_DEBIAN_KERNEL)" || { echo "X86_DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_STAGE1_INITRAMFS)" || { echo "X86_DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_CONCURRENCY_BASE_ROOT)" || { echo "X86_DEBIAN_CONCURRENCY_BASE_ROOT is required" >&2; exit 2; }
	@echo "fs-concurrency: base=$(X86_DEBIAN_CONCURRENCY_BASE_ROOT) workers=$(X86_DEBIAN_CONCURRENCY_WORKERS) seconds=$(X86_DEBIAN_CONCURRENCY_SECONDS) accel=$(X86_DEBIAN_CONCURRENCY_ACCEL)"
	@tools/x86/debian/fs_concurrency_stress.sh \
		--kernel "$(X86_DEBIAN_KERNEL)" \
		--initramfs "$(X86_DEBIAN_STAGE1_INITRAMFS)" \
		--base-root "$(X86_DEBIAN_CONCURRENCY_BASE_ROOT)" \
		--workspace "$(X86_DEBIAN_CONCURRENCY_WORKSPACE)" \
		--workers "$(X86_DEBIAN_CONCURRENCY_WORKERS)" \
		--seconds "$(X86_DEBIAN_CONCURRENCY_SECONDS)" \
		--accel "$(X86_DEBIAN_CONCURRENCY_ACCEL)" \
		--timeout "$(X86_DEBIAN_CONCURRENCY_TIMEOUT)" \
		$(if $(X86_DEBIAN_CONCURRENCY_CRASH_AFTER),--crash-after "$(X86_DEBIAN_CONCURRENCY_CRASH_AFTER)")

# Nanosecond timestamp gate: set explicit utimensat values in one boot,
# remount in a second boot, require the same st_atime_ns/st_mtime_ns, a
# non-zero `i_*time_extra` fraction in the raw inode and `e2fsck -fn`.
#   X86_DEBIAN_TIMESTAMP_BASE_ROOT  ext4 image copied for the run
X86_DEBIAN_TIMESTAMP_BASE_ROOT ?=
X86_DEBIAN_TIMESTAMP_WORKSPACE ?= $(CURDIR)/target/x86-debian/timestamps
X86_DEBIAN_TIMESTAMP_ACCEL ?= kvm
X86_DEBIAN_TIMESTAMP_TIMEOUT ?= 900

.PHONY: test_x86_debian_fs_timestamps
test_x86_debian_fs_timestamps:
	@test -n "$(X86_DEBIAN_KERNEL)" || { echo "X86_DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_STAGE1_INITRAMFS)" || { echo "X86_DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_TIMESTAMP_BASE_ROOT)" || { echo "X86_DEBIAN_TIMESTAMP_BASE_ROOT is required" >&2; exit 2; }
	@echo "fs-timestamps: base=$(X86_DEBIAN_TIMESTAMP_BASE_ROOT) accel=$(X86_DEBIAN_TIMESTAMP_ACCEL)"
	@tools/x86/debian/fs_timestamp_check.sh \
		--kernel "$(X86_DEBIAN_KERNEL)" \
		--initramfs "$(X86_DEBIAN_STAGE1_INITRAMFS)" \
		--base-root "$(X86_DEBIAN_TIMESTAMP_BASE_ROOT)" \
		--workspace "$(X86_DEBIAN_TIMESTAMP_WORKSPACE)" \
		--accel "$(X86_DEBIAN_TIMESTAMP_ACCEL)" \
		--timeout "$(X86_DEBIAN_TIMESTAMP_TIMEOUT)"

# renameat2 gate: RENAME_EXCHANGE (same dir, cross dir, directory <-> file),
# RENAME_NOREPLACE, ENOENT/EINVAL handling; the image is remounted and the
# resulting tree re-checked before `e2fsck -fn`.
#   X86_DEBIAN_RENAMEAT2_BASE_ROOT  ext4 image copied for the run
X86_DEBIAN_RENAMEAT2_BASE_ROOT ?=
X86_DEBIAN_RENAMEAT2_WORKSPACE ?= $(CURDIR)/target/x86-debian/renameat2
X86_DEBIAN_RENAMEAT2_ACCEL ?= kvm
X86_DEBIAN_RENAMEAT2_TIMEOUT ?= 900

.PHONY: test_x86_debian_fs_renameat2
test_x86_debian_fs_renameat2:
	@test -n "$(X86_DEBIAN_KERNEL)" || { echo "X86_DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_STAGE1_INITRAMFS)" || { echo "X86_DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_RENAMEAT2_BASE_ROOT)" || { echo "X86_DEBIAN_RENAMEAT2_BASE_ROOT is required" >&2; exit 2; }
	@echo "fs-renameat2: base=$(X86_DEBIAN_RENAMEAT2_BASE_ROOT) accel=$(X86_DEBIAN_RENAMEAT2_ACCEL)"
	@tools/x86/debian/fs_renameat2_check.sh \
		--kernel "$(X86_DEBIAN_KERNEL)" \
		--initramfs "$(X86_DEBIAN_STAGE1_INITRAMFS)" \
		--base-root "$(X86_DEBIAN_RENAMEAT2_BASE_ROOT)" \
		--workspace "$(X86_DEBIAN_RENAMEAT2_WORKSPACE)" \
		--accel "$(X86_DEBIAN_RENAMEAT2_ACCEL)" \
		--timeout "$(X86_DEBIAN_RENAMEAT2_TIMEOUT)"

# fallocate gate: ZERO_RANGE over an existing range, past EOF and with
# KEEP_SIZE, plain allocation, and the punch-hole error contract; the image
# is remounted and the contents re-checked before `e2fsck -fn`.
#   X86_DEBIAN_FALLOCATE_BASE_ROOT  ext4 image copied for the run
X86_DEBIAN_FALLOCATE_BASE_ROOT ?=
X86_DEBIAN_FALLOCATE_WORKSPACE ?= $(CURDIR)/target/x86-debian/fallocate
X86_DEBIAN_FALLOCATE_ACCEL ?= kvm
X86_DEBIAN_FALLOCATE_TIMEOUT ?= 900

.PHONY: test_x86_debian_fs_fallocate
test_x86_debian_fs_fallocate:
	@test -n "$(X86_DEBIAN_KERNEL)" || { echo "X86_DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_STAGE1_INITRAMFS)" || { echo "X86_DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_FALLOCATE_BASE_ROOT)" || { echo "X86_DEBIAN_FALLOCATE_BASE_ROOT is required" >&2; exit 2; }
	@echo "fs-fallocate: base=$(X86_DEBIAN_FALLOCATE_BASE_ROOT) accel=$(X86_DEBIAN_FALLOCATE_ACCEL)"
	@tools/x86/debian/fs_fallocate_check.sh \
		--kernel "$(X86_DEBIAN_KERNEL)" \
		--initramfs "$(X86_DEBIAN_STAGE1_INITRAMFS)" \
		--base-root "$(X86_DEBIAN_FALLOCATE_BASE_ROOT)" \
		--workspace "$(X86_DEBIAN_FALLOCATE_WORKSPACE)" \
		--accel "$(X86_DEBIAN_FALLOCATE_ACCEL)" \
		--timeout "$(X86_DEBIAN_FALLOCATE_TIMEOUT)"

# Extent allocation gate: append to an inline-extent file until the tree is
# promoted to depth one, append again inside the leaf, then require a
# depth-one tree on disk, unchanged contents after a remount and `e2fsck -fn`.
#   X86_DEBIAN_EXTENTS_BASE_ROOT  ext4 image copied for the run
X86_DEBIAN_EXTENTS_BASE_ROOT ?=
X86_DEBIAN_EXTENTS_WORKSPACE ?= $(CURDIR)/target/x86-debian/extents
X86_DEBIAN_EXTENTS_ACCEL ?= kvm
X86_DEBIAN_EXTENTS_TIMEOUT ?= 900

.PHONY: test_x86_debian_fs_extents
test_x86_debian_fs_extents:
	@test -n "$(X86_DEBIAN_KERNEL)" || { echo "X86_DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_STAGE1_INITRAMFS)" || { echo "X86_DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_EXTENTS_BASE_ROOT)" || { echo "X86_DEBIAN_EXTENTS_BASE_ROOT is required" >&2; exit 2; }
	@echo "fs-extents: base=$(X86_DEBIAN_EXTENTS_BASE_ROOT) accel=$(X86_DEBIAN_EXTENTS_ACCEL)"
	@tools/x86/debian/fs_extent_alloc_check.sh \
		--kernel "$(X86_DEBIAN_KERNEL)" \
		--initramfs "$(X86_DEBIAN_STAGE1_INITRAMFS)" \
		--base-root "$(X86_DEBIAN_EXTENTS_BASE_ROOT)" \
		--workspace "$(X86_DEBIAN_EXTENTS_WORKSPACE)" \
		--accel "$(X86_DEBIAN_EXTENTS_ACCEL)" \
		--timeout "$(X86_DEBIAN_EXTENTS_TIMEOUT)"

# 64BIT/FLEX_BG gate: the Debian root image is converted to 64-byte group
# descriptors and FLEX_BG, and a fresh `mke2fs -O 64bit,flex_bg` image (whose
# group metadata `mke2fs` packs into another group) is mounted as a second
# disk, written, fsynced and unmounted.  Both images are re-checked after a
# remount and must survive `e2fsck -fn`.
#   X86_DEBIAN_FEATURES_BASE_ROOT  ext4 image converted and copied per run
X86_DEBIAN_FEATURES_BASE_ROOT ?=
X86_DEBIAN_FEATURES_WORKSPACE ?= $(CURDIR)/target/x86-debian/features
X86_DEBIAN_FEATURES_ACCEL ?= kvm
X86_DEBIAN_FEATURES_TIMEOUT ?= 900

.PHONY: test_x86_debian_fs_ext4_features
test_x86_debian_fs_ext4_features:
	@test -n "$(X86_DEBIAN_KERNEL)" || { echo "X86_DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_STAGE1_INITRAMFS)" || { echo "X86_DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_FEATURES_BASE_ROOT)" || { echo "X86_DEBIAN_FEATURES_BASE_ROOT is required" >&2; exit 2; }
	@echo "fs-ext4-features: base=$(X86_DEBIAN_FEATURES_BASE_ROOT) accel=$(X86_DEBIAN_FEATURES_ACCEL)"
	@tools/x86/debian/fs_ext4_features_check.sh \
		--kernel "$(X86_DEBIAN_KERNEL)" \
		--initramfs "$(X86_DEBIAN_STAGE1_INITRAMFS)" \
		--base-root "$(X86_DEBIAN_FEATURES_BASE_ROOT)" \
		--workspace "$(X86_DEBIAN_FEATURES_WORKSPACE)" \
		--accel "$(X86_DEBIAN_FEATURES_ACCEL)" \
		--timeout "$(X86_DEBIAN_FEATURES_TIMEOUT)"

# metadata_csum gate: the Debian image is converted with tune2fs and every
# checksummed structure is exercised (superblock, descriptors, bitmaps,
# inodes, extent leaf, directory tails including an htree->linear conversion,
# and an external xattr block).  Journal checksum v3 is enabled, the kernel
# is crashed right after a journal commit, and e2fsprogs must replay that
# journal without a checksum error.  Every phase ends with `e2fsck -fn`.
#   X86_DEBIAN_METADATA_CSUM_BASE_ROOT  ext4 image converted per run
X86_DEBIAN_METADATA_CSUM_BASE_ROOT ?=
X86_DEBIAN_METADATA_CSUM_WORKSPACE ?= $(CURDIR)/target/x86-debian/metadata-csum
X86_DEBIAN_METADATA_CSUM_ACCEL ?= kvm
X86_DEBIAN_METADATA_CSUM_TIMEOUT ?= 900

.PHONY: test_x86_debian_fs_metadata_csum
test_x86_debian_fs_metadata_csum:
	@test -n "$(X86_DEBIAN_KERNEL)" || { echo "X86_DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_STAGE1_INITRAMFS)" || { echo "X86_DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_METADATA_CSUM_BASE_ROOT)" || { echo "X86_DEBIAN_METADATA_CSUM_BASE_ROOT is required" >&2; exit 2; }
	@echo "fs-metadata-csum: base=$(X86_DEBIAN_METADATA_CSUM_BASE_ROOT) accel=$(X86_DEBIAN_METADATA_CSUM_ACCEL)"
	@tools/x86/debian/fs_metadata_csum_check.sh \
		--kernel "$(X86_DEBIAN_KERNEL)" \
		--initramfs "$(X86_DEBIAN_STAGE1_INITRAMFS)" \
		--base-root "$(X86_DEBIAN_METADATA_CSUM_BASE_ROOT)" \
		--workspace "$(X86_DEBIAN_METADATA_CSUM_WORKSPACE)" \
		--accel "$(X86_DEBIAN_METADATA_CSUM_ACCEL)" \
		--timeout "$(X86_DEBIAN_METADATA_CSUM_TIMEOUT)"

# Punch-hole gate: holes inside direct blocks, across the direct/indirect
# boundary, up to EOF and over a whole double-indirect file (which must free
# every block); misaligned and extent-mapped requests must fail cleanly.  The
# image is remounted and re-checked before `e2fsck -fn`.
#   X86_DEBIAN_PUNCH_BASE_ROOT  ext4 image copied for the run
X86_DEBIAN_PUNCH_BASE_ROOT ?=
X86_DEBIAN_PUNCH_WORKSPACE ?= $(CURDIR)/target/x86-debian/punch
X86_DEBIAN_PUNCH_ACCEL ?= kvm
X86_DEBIAN_PUNCH_TIMEOUT ?= 900

.PHONY: test_x86_debian_fs_punch_hole
test_x86_debian_fs_punch_hole:
	@test -n "$(X86_DEBIAN_KERNEL)" || { echo "X86_DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_STAGE1_INITRAMFS)" || { echo "X86_DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_PUNCH_BASE_ROOT)" || { echo "X86_DEBIAN_PUNCH_BASE_ROOT is required" >&2; exit 2; }
	@echo "fs-punch-hole: base=$(X86_DEBIAN_PUNCH_BASE_ROOT) accel=$(X86_DEBIAN_PUNCH_ACCEL)"
	@tools/x86/debian/fs_punch_hole_check.sh 		--kernel "$(X86_DEBIAN_KERNEL)" 		--initramfs "$(X86_DEBIAN_STAGE1_INITRAMFS)" 		--base-root "$(X86_DEBIAN_PUNCH_BASE_ROOT)" 		--workspace "$(X86_DEBIAN_PUNCH_WORKSPACE)" 		--accel "$(X86_DEBIAN_PUNCH_ACCEL)" 		--timeout "$(X86_DEBIAN_PUNCH_TIMEOUT)"

# POSIX ACL gate: store system.posix_acl_access, run reads/writes as different
# users (owner, named user, named group, other, root override), re-check after
# a remount and require `e2fsck -fn`.
#   X86_DEBIAN_ACL_BASE_ROOT  ext4 image copied for the run
X86_DEBIAN_ACL_BASE_ROOT ?=
X86_DEBIAN_ACL_WORKSPACE ?= $(CURDIR)/target/x86-debian/acl
X86_DEBIAN_ACL_ACCEL ?= kvm
X86_DEBIAN_ACL_TIMEOUT ?= 900

.PHONY: test_x86_debian_fs_acl
test_x86_debian_fs_acl:
	@test -n "$(X86_DEBIAN_KERNEL)" || { echo "X86_DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_STAGE1_INITRAMFS)" || { echo "X86_DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(X86_DEBIAN_ACL_BASE_ROOT)" || { echo "X86_DEBIAN_ACL_BASE_ROOT is required" >&2; exit 2; }
	@echo "fs-acl: base=$(X86_DEBIAN_ACL_BASE_ROOT) accel=$(X86_DEBIAN_ACL_ACCEL)"
	@tools/x86/debian/fs_acl_check.sh 		--kernel "$(X86_DEBIAN_KERNEL)" 		--initramfs "$(X86_DEBIAN_STAGE1_INITRAMFS)" 		--base-root "$(X86_DEBIAN_ACL_BASE_ROOT)" 		--workspace "$(X86_DEBIAN_ACL_WORKSPACE)" 		--accel "$(X86_DEBIAN_ACL_ACCEL)" 		--timeout "$(X86_DEBIAN_ACL_TIMEOUT)"

.PHONY: test_riscv_run_kernel_log_unit
test_riscv_run_kernel_log_unit:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_validate_run_kernel_log -v

.PHONY: test_riscv_megrez_gmac_unit
test_riscv_megrez_gmac_unit:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_megrez_gmac_contract \
		tools.riscv.tests.test_megrez_gmac_gate \
		tools.riscv.tests.test_megrez_network_fixture \
		tools.riscv.tests.test_megrez_proxy_bridge \
		tools.riscv.tests.test_megrez_xmodem -v

.PHONY: test_riscv_dwmac_rx_model
test_riscv_dwmac_rx_model:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_dwmac_rx_liveness_model -v

.PHONY: test_riscv_megrez_debug_unit
test_riscv_megrez_debug_unit:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_first_process_diag_source \
		tools.riscv.tests.test_megrez_debug \
		tools.riscv.tests.test_megrez_debug_desktop \
		tools.riscv.tests.test_megrez_install_workflow \
		tools.riscv.tests.test_megrez_preboard -v

.PHONY: test_riscv_physical_graphics_unit
test_riscv_physical_graphics_unit:
	@PYTHONPATH="$(CURDIR)/tools/riscv:$(CURDIR)" \
		python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_physical_graphics_gate \
		tools.riscv.tests.test_hdmi_pixel_oracle \
		tools.riscv.tests.test_megrez_physical_graphics \
		tools.riscv.tests.test_physical_graphics_qemu_gate -v

.PHONY: test_riscv_firefox_daily_use_physical_unit
test_riscv_firefox_daily_use_physical_unit:
	@PYTHONPATH="$(CURDIR)" python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_browser_daily_use_contract \
		tools.riscv.tests.test_browser_daily_use_gate \
		tools.riscv.tests.test_browser_daily_use_upload \
		tools.riscv.tests.test_megrez_network_fixture \
		tools.riscv.tests.test_debian_rootfs \
		tools.riscv.tests.test_megrez_physical_graphics \
		tools.riscv.tests.test_megrez_firefox_daily_use \
		tools.riscv.tests.test_megrez_firefox_daily_use_report -v

.PHONY: test_riscv_megrez_boot_stability_unit
test_riscv_megrez_boot_stability_unit:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_megrez_boot_stability \
		tools.riscv.tests.test_megrez_rockos_attestation \
		tools.riscv.tests.test_megrez_physical_graphics \
		tools.riscv.tests.test_debian_rootfs.DebianRootfsGateRuntimeTests -v

.PHONY: test_riscv_megrez_desktop_unit
test_riscv_megrez_desktop_unit:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_megrez_desktop \
		tools.riscv.tests.test_megrez_desktop_boot \
		tools.riscv.tests.test_megrez_firefox_browse \
		tools.riscv.tests.test_megrez_clock_sync -v

.PHONY: test_riscv_megrez_boot_manifest_unit
test_riscv_megrez_boot_manifest_unit:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_megrez_boot_manifest \
		tools.riscv.tests.test_megrez_rockos_attestation -v

.PHONY: test_riscv_megrez_boot_menu_unit
test_riscv_megrez_boot_menu_unit: test_riscv_megrez_boot_manifest_unit
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_megrez_boot_menu \
		tools.riscv.tests.test_debian_rootfs.DebianStage1Tests -v

.PHONY: test_riscv_megrez_probe_unit
test_riscv_megrez_probe_unit: test_riscv_megrez_boot_manifest_unit
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_megrez_probe \
		tools.riscv.tests.test_debian_rootfs.DebianStage1Tests -v

MEGREZ_PROBE_BUNDLE ?= target/megrez-probe/current.json
MEGREZ_PROBE_KERNEL ?= target/osdk/aster-kernel-osdk-bin.Image
MEGREZ_PROBE_INITRAMFS ?= target/megrez-probe/build/initramfs.cpio

.PHONY: test_riscv_megrez_probe_qemu
test_riscv_megrez_probe_qemu:
	@python3 -m tools.riscv.megrez_probe boot syscall213 \
		--bundle "$(MEGREZ_PROBE_BUNDLE)" \
		--output-directory target/megrez-probe/qemu-normal \
		--qemu --qemu-kernel "$(MEGREZ_PROBE_KERNEL)" \
		--qemu-initramfs "$(MEGREZ_PROBE_INITRAMFS)"
	@python3 -m tools.riscv.megrez_probe boot \
		--bundle "$(MEGREZ_PROBE_BUNDLE)" \
		--output-directory target/megrez-probe/qemu-deadline \
		--qemu --qemu-kernel "$(MEGREZ_PROBE_KERNEL)" \
		--qemu-initramfs "$(MEGREZ_PROBE_INITRAMFS)" \
		--qemu-deadline-only

DUAL_HOST_PROBE_KERNEL ?= target/osdk/aster-kernel-osdk-bin.Image
DUAL_HOST_PROBE_INITRAMFS ?= target/dual-host-qemu-probe/artifacts/initramfs.cpio
ROCKOS_SSH ?=

.PHONY: test_riscv_dual_host_probe
test_riscv_dual_host_probe:
	@python3 -m tools.riscv.dual_host_qemu_probe \
		--kernel "$(DUAL_HOST_PROBE_KERNEL)" \
		--initramfs "$(DUAL_HOST_PROBE_INITRAMFS)" \
		--rockos "$(ROCKOS_SSH)"

.PHONY: test_riscv_megrez_debug_desktop
test_riscv_megrez_debug_desktop:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_megrez_debug_desktop -v

.PHONY: prepare_riscv_megrez_desktop_boot
prepare_riscv_megrez_desktop_boot:
	@test -f "$(MEGREZ_DESKTOP_BOOT_PLAN)" || \
		{ echo "MEGREZ_DESKTOP_BOOT_PLAN must name a regular file" >&2; exit 2; }
	@test ! -e "$(MEGREZ_DESKTOP_BOOT_PREPARE_OUTPUT)" || \
		{ echo "MEGREZ_DESKTOP_BOOT_PREPARE_OUTPUT already exists" >&2; exit 2; }
	@python3 -m tools.riscv.megrez_desktop_boot prepare \
		--plan "$(MEGREZ_DESKTOP_BOOT_PLAN)" \
		--device "$(MEGREZ_DESKTOP_BOOT_DEVICE)" \
		--host-address "$(MEGREZ_DESKTOP_BOOT_HOST_ADDRESS)" \
		--port "$(MEGREZ_DESKTOP_BOOT_PORT)" \
		--factory-login \
		--output "$(MEGREZ_DESKTOP_BOOT_PREPARE_OUTPUT)"

.PHONY: run_riscv_megrez_desktop
run_riscv_megrez_desktop:
	@test -f "$(MEGREZ_DESKTOP_BOOT_PLAN)" || \
		{ echo "MEGREZ_DESKTOP_BOOT_PLAN must name a regular file" >&2; exit 2; }
	@test ! -e "$(MEGREZ_DESKTOP_BOOT_START_OUTPUT)" || \
		{ echo "MEGREZ_DESKTOP_BOOT_START_OUTPUT already exists" >&2; exit 2; }
	@python3 -m tools.riscv.megrez_desktop_boot start \
		--plan "$(MEGREZ_DESKTOP_BOOT_PLAN)" \
		--device "$(MEGREZ_DESKTOP_BOOT_DEVICE)" \
		--device-profile "$(MEGREZ_DESKTOP_BOOT_DEVICE_PROFILE)" \
		--output "$(MEGREZ_DESKTOP_BOOT_START_OUTPUT)"

.PHONY: test_riscv_megrez_preboard
test_riscv_megrez_preboard:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_megrez_preboard -v

.PHONY: test_riscv_megrez_install_unit
test_riscv_megrez_install_unit:
	@PYTHONPATH="$(CURDIR)/tools/riscv:$(CURDIR)" \
		python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_megrez_debian_installer \
		tools.riscv.tests.test_megrez_board_session \
		tools.riscv.tests.test_megrez_install_workflow -v

.PHONY: test_riscv_megrez_debug_fast
test_riscv_megrez_debug_fast: test_riscv_megrez_debug_unit
	@test -n "$(MEGREZ_DEBUG_PLAN)" || \
		{ echo "MEGREZ_DEBUG_PLAN is required" >&2; exit 2; }
	@PYTHONPATH="$(CURDIR)" python3 -m tools.riscv.megrez_debug simulate \
		"$(MEGREZ_DEBUG_PLAN)" --tier fast \
		--output-directory "$(MEGREZ_DEBUG_FAST_OUT_DIR_EFFECTIVE)" \
		--uboot-build-directory "$(MEGREZ_DEBUG_UBOOT_BUILD_DIR_EFFECTIVE)"

.PHONY: test_riscv_megrez_debug_board
test_riscv_megrez_debug_board: test_riscv_megrez_debug_unit
	@test -n "$(MEGREZ_DEBUG_PLAN)" || \
		{ echo "MEGREZ_DEBUG_PLAN is required" >&2; exit 2; }
	@test -n "$(MEGREZ_DEBUG_DEVICE)" || \
		{ echo "MEGREZ_DEBUG_DEVICE is required" >&2; exit 2; }
	@test -n "$(MEGREZ_DEBUG_SIMULATION_RESULT)" || \
		{ echo "MEGREZ_DEBUG_SIMULATION_RESULT is required" >&2; exit 2; }
	@PYTHONPATH="$(CURDIR)" python3 -m tools.riscv.megrez_debug board \
		"$(MEGREZ_DEBUG_PLAN)" "$(MEGREZ_DEBUG_DEVICE)" \
		--simulation-result "$(MEGREZ_DEBUG_SIMULATION_RESULT)" \
		--output-directory "$(MEGREZ_DEBUG_BOARD_OUT_DIR_EFFECTIVE)" \
		--timeout "$(MEGREZ_DEBUG_BOARD_TIMEOUT)"

.PHONY: prepare_riscv_megrez_physical_graphics
prepare_riscv_megrez_physical_graphics:
	@test -n "$(MEGREZ_PHYSICAL_GRAPHICS_PLAN)" || \
		{ echo "MEGREZ_PHYSICAL_GRAPHICS_PLAN is required" >&2; exit 2; }
	@test -n "$(MEGREZ_PHYSICAL_GRAPHICS_DEVICE)" || \
		{ echo "MEGREZ_PHYSICAL_GRAPHICS_DEVICE is required" >&2; exit 2; }
	@test "$(MEGREZ_PHYSICAL_GRAPHICS_DISPLAY)" = hdmi -o \
		"$(MEGREZ_PHYSICAL_GRAPHICS_DISPLAY)" = operator-attested || \
		{ echo "MEGREZ_PHYSICAL_GRAPHICS_DISPLAY must be hdmi or operator-attested" >&2; exit 2; }
	@test "$(MEGREZ_PHYSICAL_GRAPHICS_CYCLES)" = 1 -o \
		"$(MEGREZ_PHYSICAL_GRAPHICS_CYCLES)" = 3 || \
		{ echo "MEGREZ_PHYSICAL_GRAPHICS_CYCLES must be 1 or 3" >&2; exit 2; }
	@if [ "$(MEGREZ_PHYSICAL_GRAPHICS_DISPLAY)" = hdmi ]; then \
		test -n "$(MEGREZ_PHYSICAL_GRAPHICS_HDMI_CAPTURE)" || \
			{ echo "MEGREZ_PHYSICAL_GRAPHICS_HDMI_CAPTURE is required for hdmi" >&2; exit 2; }; \
		command -v ffmpeg >/dev/null 2>&1 || \
			{ echo "ffmpeg is required for HDMI pixel comparison" >&2; exit 2; }; \
		command -v ffprobe >/dev/null 2>&1 || \
			{ echo "ffprobe is required for HDMI pixel comparison" >&2; exit 2; }; \
	else \
		test -z "$(MEGREZ_PHYSICAL_GRAPHICS_HDMI_CAPTURE)" || \
			{ echo "MEGREZ_PHYSICAL_GRAPHICS_HDMI_CAPTURE conflicts with operator-attested" >&2; exit 2; }; \
	fi
	@set -eu; count=0; \
	for value in \
		"$(MEGREZ_PHYSICAL_GRAPHICS_MMC_KERNEL)" \
		"$(MEGREZ_PHYSICAL_GRAPHICS_MMC_INITRAMFS)" \
		"$(MEGREZ_PHYSICAL_GRAPHICS_MMC_DTB)"; do \
		test -z "$$value" || count=$$((count + 1)); \
	done; \
	test "$$count" -eq 0 -o "$$count" -eq 3 || \
		{ echo "all three MEGREZ_PHYSICAL_GRAPHICS_MMC_* values are required together" >&2; exit 2; }
	@test -n "$(MEGREZ_PHYSICAL_GRAPHICS_OUTPUT)" || \
		{ echo "MEGREZ_PHYSICAL_GRAPHICS_OUTPUT is required" >&2; exit 2; }
	@PYTHONPATH="$(CURDIR)" python3 -c \
		'from pathlib import Path; import sys; from tools.riscv.megrez_debug_simulation import _validate_current_artifacts; from tools.riscv.megrez_physical_graphics import _read_plan; _validate_current_artifacts(_read_plan(Path(sys.argv[1])))' \
		"$(MEGREZ_PHYSICAL_GRAPHICS_PLAN)"
	@set -eu; printf '%q ' env "PYTHONPATH=$(CURDIR)" python3 -m \
		tools.riscv.megrez_physical_graphics \
		"$(MEGREZ_PHYSICAL_GRAPHICS_DEVICE)" \
		--plan "$(MEGREZ_PHYSICAL_GRAPHICS_PLAN)" \
		--output-directory "$(MEGREZ_PHYSICAL_GRAPHICS_OUTPUT)"; \
	if [ "$(MEGREZ_PHYSICAL_GRAPHICS_DISPLAY)" = hdmi ]; then \
		printf '%q ' --hdmi-capture "$(MEGREZ_PHYSICAL_GRAPHICS_HDMI_CAPTURE)"; \
	else \
		printf '%q ' --operator-display-attestation; \
	fi; \
	printf '%q ' --cycles "$(MEGREZ_PHYSICAL_GRAPHICS_CYCLES)"; \
	if [ -n "$(MEGREZ_PHYSICAL_GRAPHICS_MMC_KERNEL)" ]; then \
		printf '%q ' \
			--mmc-kernel "$(MEGREZ_PHYSICAL_GRAPHICS_MMC_KERNEL)" \
			--mmc-initramfs "$(MEGREZ_PHYSICAL_GRAPHICS_MMC_INITRAMFS)" \
			--mmc-dtb "$(MEGREZ_PHYSICAL_GRAPHICS_MMC_DTB)"; \
	fi; \
	printf '%q ' \
		--open-timeout 60 --artifact-timeout 300 --boot-timeout 300 \
		--cycle-timeout 180 --hdmi-timeout 60 --recovery-timeout 930; printf '\n'

.PHONY: prepare_riscv_megrez_firefox_daily_use
prepare_riscv_megrez_firefox_daily_use:
	@test -n "$(MEGREZ_FIREFOX_DAILY_USE_PLAN)" || \
		{ echo "MEGREZ_FIREFOX_DAILY_USE_PLAN is required" >&2; exit 2; }
	@test -n "$(MEGREZ_FIREFOX_DAILY_USE_DEVICE)" || \
		{ echo "MEGREZ_FIREFOX_DAILY_USE_DEVICE is required" >&2; exit 2; }
	@case "$(MEGREZ_FIREFOX_DAILY_USE_DEVICE)" in \
		/dev/serial/by-id/*) ;; \
		*) echo "MEGREZ_FIREFOX_DAILY_USE_DEVICE must be an absolute /dev/serial/by-id/ path" >&2; exit 2 ;; \
	esac
	@test -n "$(MEGREZ_FIREFOX_DAILY_USE_OUTPUT)" || \
		{ echo "MEGREZ_FIREFOX_DAILY_USE_OUTPUT is required" >&2; exit 2; }
	@test ! -e "$(MEGREZ_FIREFOX_DAILY_USE_OUTPUT)" || \
		{ echo "MEGREZ_FIREFOX_DAILY_USE_OUTPUT already exists" >&2; exit 2; }
	@test "$(MEGREZ_FIREFOX_DAILY_USE_FIXTURE_BIND)" = 10.100.19.216 || \
		{ echo "MEGREZ_FIREFOX_DAILY_USE_FIXTURE_BIND must be 10.100.19.216" >&2; exit 2; }
	@test "$(MEGREZ_FIREFOX_DAILY_USE_FIXTURE_PORT)" = 17894 || \
		{ echo "MEGREZ_FIREFOX_DAILY_USE_FIXTURE_PORT must be 17894" >&2; exit 2; }
	@test "$(MEGREZ_FIREFOX_DAILY_USE_BOARD_PEER)" = 10.100.19.200 || \
		{ echo "MEGREZ_FIREFOX_DAILY_USE_BOARD_PEER must be 10.100.19.200" >&2; exit 2; }
	@set -eu; count=0; \
	for value in \
		"$(MEGREZ_FIREFOX_DAILY_USE_MMC_KERNEL)" \
		"$(MEGREZ_FIREFOX_DAILY_USE_MMC_INITRAMFS)" \
		"$(MEGREZ_FIREFOX_DAILY_USE_MMC_DTB)"; do \
		test -z "$$value" || count=$$((count + 1)); \
	done; \
	test "$$count" -eq 0 -o "$$count" -eq 3 || \
		{ echo "all three MEGREZ_FIREFOX_DAILY_USE_MMC_* values are required together" >&2; exit 2; }
	@set -eu; set -- env "PYTHONPATH=$(CURDIR)" python3 -m \
		tools.riscv.megrez_firefox_daily_use \
		"$(MEGREZ_FIREFOX_DAILY_USE_DEVICE)" \
		--plan "$(MEGREZ_FIREFOX_DAILY_USE_PLAN)" \
		--output-directory "$(MEGREZ_FIREFOX_DAILY_USE_OUTPUT)" \
		--fixture-bind "$(MEGREZ_FIREFOX_DAILY_USE_FIXTURE_BIND)" \
		--fixture-port "$(MEGREZ_FIREFOX_DAILY_USE_FIXTURE_PORT)" \
		--board-peer "$(MEGREZ_FIREFOX_DAILY_USE_BOARD_PEER)" \
		--profile-timeout 120 --open-timeout 60 --artifact-timeout 300 \
		--boot-timeout 300 --upload-timeout 30 --recovery-timeout 930; \
	if [ -n "$(MEGREZ_FIREFOX_DAILY_USE_MMC_KERNEL)" ]; then \
		set -- "$$@" \
			--mmc-kernel "$(MEGREZ_FIREFOX_DAILY_USE_MMC_KERNEL)" \
			--mmc-initramfs "$(MEGREZ_FIREFOX_DAILY_USE_MMC_INITRAMFS)" \
			--mmc-dtb "$(MEGREZ_FIREFOX_DAILY_USE_MMC_DTB)"; \
	fi; \
	"$$@" --prepare-only

.PHONY: test_riscv_debian_rootfs_gate
test_riscv_debian_rootfs_gate:
	@test -n "$(DEBIAN_KERNEL)" || \
		{ echo "DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(DEBIAN_UBOOT)" || \
		{ echo "DEBIAN_UBOOT is required" >&2; exit 2; }
	@test -n "$(DEBIAN_DTB)" || \
		{ echo "DEBIAN_DTB is required" >&2; exit 2; }
	@test -n "$(DEBIAN_STAGE1_INITRAMFS)" || \
		{ echo "DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_IMAGE)" || \
		{ echo "DEBIAN_ROOT_IMAGE is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_MANIFEST)" || \
		{ echo "DEBIAN_ROOT_MANIFEST is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGES_LOCK)" || \
		{ echo "DEBIAN_PACKAGES_LOCK is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGE_CHECKSUMS)" || \
		{ echo "DEBIAN_PACKAGE_CHECKSUMS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_GATE_OUTPUT)" || \
		{ echo "DEBIAN_GATE_OUTPUT is required" >&2; exit 2; }
	@python3 -m tools.riscv.debian.rootfs.rootfs_gate \
		--kernel "$(DEBIAN_KERNEL)" \
		--uboot "$(DEBIAN_UBOOT)" \
		--dtb "$(DEBIAN_DTB)" \
		--stage1-initramfs "$(DEBIAN_STAGE1_INITRAMFS)" \
		--root-image "$(DEBIAN_ROOT_IMAGE)" \
		--root-manifest "$(DEBIAN_ROOT_MANIFEST)" \
		--packages-lock "$(DEBIAN_PACKAGES_LOCK)" \
		--package-checksums "$(DEBIAN_PACKAGE_CHECKSUMS)" \
		--output-directory "$(DEBIAN_GATE_OUTPUT)" --smp 4

.PHONY: test_riscv_debian_systemd_m2_gate
test_riscv_debian_systemd_m2_gate:
	@test -n "$(DEBIAN_KERNEL)" || \
		{ echo "DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(DEBIAN_UBOOT)" || \
		{ echo "DEBIAN_UBOOT is required" >&2; exit 2; }
	@test -n "$(DEBIAN_DTB)" || \
		{ echo "DEBIAN_DTB is required" >&2; exit 2; }
	@test -n "$(DEBIAN_STAGE1_INITRAMFS)" || \
		{ echo "DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_IMAGE)" || \
		{ echo "DEBIAN_ROOT_IMAGE is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_MANIFEST)" || \
		{ echo "DEBIAN_ROOT_MANIFEST is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGES_LOCK)" || \
		{ echo "DEBIAN_PACKAGES_LOCK is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGE_CHECKSUMS)" || \
		{ echo "DEBIAN_PACKAGE_CHECKSUMS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_SYSTEMD_M2_GATE_OUTPUT)" || \
		{ echo "DEBIAN_SYSTEMD_M2_GATE_OUTPUT is required" >&2; exit 2; }
	@python3 -m tools.riscv.debian.rootfs.systemd_m2_gate \
		--kernel "$(DEBIAN_KERNEL)" \
		--uboot "$(DEBIAN_UBOOT)" \
		--dtb "$(DEBIAN_DTB)" \
		--stage1-initramfs "$(DEBIAN_STAGE1_INITRAMFS)" \
		--root-image "$(DEBIAN_ROOT_IMAGE)" \
		--root-manifest "$(DEBIAN_ROOT_MANIFEST)" \
		--packages-lock "$(DEBIAN_PACKAGES_LOCK)" \
		--package-checksums "$(DEBIAN_PACKAGE_CHECKSUMS)" \
		--output-directory "$(DEBIAN_SYSTEMD_M2_GATE_OUTPUT)" --smp 4

.PHONY: test_riscv_debian_systemd_ext4_gate
DEBIAN_SYSTEMD_EXT4_BOOT_TIMEOUT ?= 900
test_riscv_debian_systemd_ext4_gate:
	@test -n "$(DEBIAN_KERNEL)" || { echo "DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(DEBIAN_UBOOT)" || { echo "DEBIAN_UBOOT is required" >&2; exit 2; }
	@test -n "$(DEBIAN_DTB)" || { echo "DEBIAN_DTB is required" >&2; exit 2; }
	@test -n "$(DEBIAN_STAGE1_INITRAMFS)" || { echo "DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_IMAGE)" || { echo "DEBIAN_ROOT_IMAGE is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_MANIFEST)" || { echo "DEBIAN_ROOT_MANIFEST is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGES_LOCK)" || { echo "DEBIAN_PACKAGES_LOCK is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGE_CHECKSUMS)" || { echo "DEBIAN_PACKAGE_CHECKSUMS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_SYSTEMD_EXT4_GATE_OUTPUT)" || { echo "DEBIAN_SYSTEMD_EXT4_GATE_OUTPUT is required" >&2; exit 2; }
	@python3 -m tools.riscv.debian.rootfs.systemd_ext4_gate \
		--kernel "$(DEBIAN_KERNEL)" --uboot "$(DEBIAN_UBOOT)" --dtb "$(DEBIAN_DTB)" \
		--stage1-initramfs "$(DEBIAN_STAGE1_INITRAMFS)" --root-image "$(DEBIAN_ROOT_IMAGE)" \
		--root-manifest "$(DEBIAN_ROOT_MANIFEST)" --packages-lock "$(DEBIAN_PACKAGES_LOCK)" \
		--package-checksums "$(DEBIAN_PACKAGE_CHECKSUMS)" \
		--output-directory "$(DEBIAN_SYSTEMD_EXT4_GATE_OUTPUT)" --smp 4 \
		--boot-timeout "$(DEBIAN_SYSTEMD_EXT4_BOOT_TIMEOUT)"

.PHONY: test_riscv_debian_desktop_m5_qemu_gate
test_riscv_debian_desktop_m5_qemu_gate:
	@test -n "$(DEBIAN_KERNEL)" || \
		{ echo "DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(DEBIAN_UBOOT)" || \
		{ echo "DEBIAN_UBOOT is required" >&2; exit 2; }
	@test -n "$(DEBIAN_DTB)" || \
		{ echo "DEBIAN_DTB is required" >&2; exit 2; }
	@test -n "$(DEBIAN_STAGE1_INITRAMFS)" || \
		{ echo "DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_IMAGE)" || \
		{ echo "DEBIAN_ROOT_IMAGE is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_MANIFEST)" || \
		{ echo "DEBIAN_ROOT_MANIFEST is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGES_LOCK)" || \
		{ echo "DEBIAN_PACKAGES_LOCK is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGE_CHECKSUMS)" || \
		{ echo "DEBIAN_PACKAGE_CHECKSUMS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_DESKTOP_M5_QEMU_GATE_OUTPUT)" || \
		{ echo "DEBIAN_DESKTOP_M5_QEMU_GATE_OUTPUT is required" >&2; exit 2; }
	@python3 -m tools.riscv.debian.rootfs.desktop_m5_qemu_gate \
		--target "$(DEBIAN_DESKTOP_M5_QEMU_GATE_TARGET)" \
		$(if $(filter network,$(DEBIAN_DESKTOP_M5_QEMU_GATE_TARGET)),--network-mode "$(DEBIAN_WEB_NETWORK_MODE)" --expect-failure "$(DEBIAN_WEB_NETWORK_EXPECT_FAILURE)") \
		--kernel "$(DEBIAN_KERNEL)" \
		--uboot "$(DEBIAN_UBOOT)" \
		--dtb "$(DEBIAN_DTB)" \
		--stage1-initramfs "$(DEBIAN_STAGE1_INITRAMFS)" \
		--root-image "$(DEBIAN_ROOT_IMAGE)" \
		--root-manifest "$(DEBIAN_ROOT_MANIFEST)" \
		--packages-lock "$(DEBIAN_PACKAGES_LOCK)" \
		--package-checksums "$(DEBIAN_PACKAGE_CHECKSUMS)" \
		--output-directory "$(DEBIAN_DESKTOP_M5_QEMU_GATE_OUTPUT)" --smp 4 \
		--boot-timeout "$(DEBIAN_DESKTOP_BOOT_TIMEOUT)"

# The DRM desktop gate uses the artifacts built for the virtio-gpu work, which
# are not the ones the other Debian variables point at, so it carries its own
# defaults rather than inheriting the browser tree's.
DEBIAN_DRM_UBOOT ?= $(CURDIR)/target/qemu-uboot/cache/u-boot-build/u-boot
DEBIAN_DRM_KERNEL ?= $(CURDIR)/target/osdk/aster-kernel-osdk-bin.Image
DEBIAN_DRM_DTB ?= $(CURDIR)/target/qemu-uboot/drm-virgl/prepared/qemu-virt.dtb
# The stage1 must be the one built for this rootfs. The xfce variant next to
# it is the same size and also contains an `init`, so it looks interchangeable
# and is not: it mounts the root and hands off to a layout this image does not
# have, failing with a bare `exec /init failed`.
DEBIAN_DRM_STAGE1_INITRAMFS ?= $(CURDIR)/target/debian-riscv/desktop-drm/stage1/initramfs.cpio
DEBIAN_DRM_ROOT_IMAGE ?= $(CURDIR)/target/debian-riscv/desktop-drm/rootfs/debian-root.ext2
DEBIAN_DRM_ROOT_MANIFEST ?= $(CURDIR)/target/debian-riscv/desktop-drm/rootfs/rootfs-manifest.json
DEBIAN_DRM_PACKAGES_LOCK ?= $(CURDIR)/target/debian-riscv/desktop-drm/rootfs/packages.lock
DEBIAN_DRM_PACKAGE_CHECKSUMS ?= $(CURDIR)/target/debian-riscv/desktop-drm/rootfs/source-metadata/package-checksums
DEBIAN_DRM_GATE_OUTPUT ?= $(CURDIR)/target/debian-riscv/desktop-drm/drm-gate
# Asking for the GL device is what selects the 3D path; the gate then also
# requires the guest to prove it reached virgl rather than llvmpipe.
DEBIAN_DRM_GRAPHICS_DEVICE ?= virtio-gpu-gl-device

.PHONY: test_riscv_debian_desktop_drm_gate
test_riscv_debian_desktop_drm_gate:
	@python3 -m tools.riscv.debian.rootfs.desktop_drm_gate \
		--kernel "$(DEBIAN_DRM_KERNEL)" \
		--uboot "$(DEBIAN_DRM_UBOOT)" \
		--dtb "$(DEBIAN_DRM_DTB)" \
		--stage1-initramfs "$(DEBIAN_DRM_STAGE1_INITRAMFS)" \
		--root-image "$(DEBIAN_DRM_ROOT_IMAGE)" \
		--root-manifest "$(DEBIAN_DRM_ROOT_MANIFEST)" \
		--packages-lock "$(DEBIAN_DRM_PACKAGES_LOCK)" \
		--package-checksums "$(DEBIAN_DRM_PACKAGE_CHECKSUMS)" \
		--output-directory "$(DEBIAN_DRM_GATE_OUTPUT)" --smp 4 \
		--graphics-device "$(DEBIAN_DRM_GRAPHICS_DEVICE)" \
		--boot-timeout "$(DEBIAN_DESKTOP_BOOT_TIMEOUT)"

.PHONY: test_riscv_debian_debug_console_qemu_gate
test_riscv_debian_debug_console_qemu_gate:
	@test -n "$(DEBIAN_KERNEL)" || \
		{ echo "DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(DEBIAN_UBOOT)" || \
		{ echo "DEBIAN_UBOOT is required" >&2; exit 2; }
	@test -n "$(DEBIAN_DTB)" || \
		{ echo "DEBIAN_DTB is required" >&2; exit 2; }
	@test -n "$(DEBIAN_STAGE1_INITRAMFS)" || \
		{ echo "DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_IMAGE)" || \
		{ echo "DEBIAN_ROOT_IMAGE is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_MANIFEST)" || \
		{ echo "DEBIAN_ROOT_MANIFEST is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGES_LOCK)" || \
		{ echo "DEBIAN_PACKAGES_LOCK is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGE_CHECKSUMS)" || \
		{ echo "DEBIAN_PACKAGE_CHECKSUMS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_DEBUG_CONSOLE_QEMU_GATE_OUTPUT)" || \
		{ echo "DEBIAN_DEBUG_CONSOLE_QEMU_GATE_OUTPUT is required" >&2; exit 2; }
	@python3 -m tools.riscv.debian.rootfs.debug_console_qemu_gate \
		--kernel "$(DEBIAN_KERNEL)" \
		--uboot "$(DEBIAN_UBOOT)" \
		--dtb "$(DEBIAN_DTB)" \
		--stage1-initramfs "$(DEBIAN_STAGE1_INITRAMFS)" \
		--root-image "$(DEBIAN_ROOT_IMAGE)" \
		--root-manifest "$(DEBIAN_ROOT_MANIFEST)" \
		--packages-lock "$(DEBIAN_PACKAGES_LOCK)" \
		--package-checksums "$(DEBIAN_PACKAGE_CHECKSUMS)" \
		--output-directory "$(DEBIAN_DEBUG_CONSOLE_QEMU_GATE_OUTPUT)" --smp 4 \
		--boot-timeout "$(DEBIAN_DESKTOP_BOOT_TIMEOUT)"

.PHONY: test_riscv_debian_web_network_proxy_qemu
test_riscv_debian_web_network_proxy_qemu:
	@$(MAKE) --no-print-directory test_riscv_debian_desktop_m5_qemu_gate \
		DEBIAN_DESKTOP_M5_QEMU_GATE_TARGET=network \
		DEBIAN_WEB_NETWORK_MODE=proxy

.PHONY: test_riscv_debian_web_network_direct_qemu
test_riscv_debian_web_network_direct_qemu:
	@$(MAKE) --no-print-directory test_riscv_debian_desktop_m5_qemu_gate \
		DEBIAN_DESKTOP_M5_QEMU_GATE_TARGET=network \
		DEBIAN_WEB_NETWORK_MODE=direct

.PHONY: test_riscv_debian_browser_m5_qemu_gate
test_riscv_debian_browser_m5_qemu_gate:
	@test -n "$(DEBIAN_KERNEL)" || \
		{ echo "DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(DEBIAN_UBOOT)" || \
		{ echo "DEBIAN_UBOOT is required" >&2; exit 2; }
	@test -n "$(DEBIAN_DTB)" || \
		{ echo "DEBIAN_DTB is required" >&2; exit 2; }
	@test -n "$(DEBIAN_STAGE1_INITRAMFS)" || \
		{ echo "DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_IMAGE)" || \
		{ echo "DEBIAN_ROOT_IMAGE is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_MANIFEST)" || \
		{ echo "DEBIAN_ROOT_MANIFEST is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGES_LOCK)" || \
		{ echo "DEBIAN_PACKAGES_LOCK is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGE_CHECKSUMS)" || \
		{ echo "DEBIAN_PACKAGE_CHECKSUMS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_BROWSER_M5_QEMU_GATE_OUTPUT)" || \
		{ echo "DEBIAN_BROWSER_M5_QEMU_GATE_OUTPUT is required" >&2; exit 2; }
	@python3 -m tools.riscv.debian.rootfs.browser_m5_qemu_gate \
		--kernel "$(DEBIAN_KERNEL)" \
		--uboot "$(DEBIAN_UBOOT)" \
		--dtb "$(DEBIAN_DTB)" \
		--stage1-initramfs "$(DEBIAN_STAGE1_INITRAMFS)" \
		--root-image "$(DEBIAN_ROOT_IMAGE)" \
		--root-manifest "$(DEBIAN_ROOT_MANIFEST)" \
		--packages-lock "$(DEBIAN_PACKAGES_LOCK)" \
		--package-checksums "$(DEBIAN_PACKAGE_CHECKSUMS)" \
		--output-directory "$(DEBIAN_BROWSER_M5_QEMU_GATE_OUTPUT)" --smp 4 \
		--boot-timeout 7200

.PHONY: test_riscv_debian_browser_web_qemu_gate
test_riscv_debian_browser_web_qemu_gate:
	@test -n "$(DEBIAN_KERNEL)" || { echo "DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(DEBIAN_UBOOT)" || { echo "DEBIAN_UBOOT is required" >&2; exit 2; }
	@test -n "$(DEBIAN_DTB)" || { echo "DEBIAN_DTB is required" >&2; exit 2; }
	@test -n "$(DEBIAN_STAGE1_INITRAMFS)" || { echo "DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_IMAGE)" || { echo "DEBIAN_ROOT_IMAGE is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_MANIFEST)" || { echo "DEBIAN_ROOT_MANIFEST is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGES_LOCK)" || { echo "DEBIAN_PACKAGES_LOCK is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGE_CHECKSUMS)" || { echo "DEBIAN_PACKAGE_CHECKSUMS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_BROWSER_WEB_QEMU_GATE_OUTPUT)" || { echo "DEBIAN_BROWSER_WEB_QEMU_GATE_OUTPUT is required" >&2; exit 2; }
	@python3 -m tools.riscv.debian.rootfs.browser_web_qemu_gate \
		--network-mode "$(DEBIAN_BROWSER_WEB_NETWORK_MODE)" \
		--kernel "$(DEBIAN_KERNEL)" --uboot "$(DEBIAN_UBOOT)" --dtb "$(DEBIAN_DTB)" \
		--stage1-initramfs "$(DEBIAN_STAGE1_INITRAMFS)" \
		--root-image "$(DEBIAN_ROOT_IMAGE)" --root-manifest "$(DEBIAN_ROOT_MANIFEST)" \
		--packages-lock "$(DEBIAN_PACKAGES_LOCK)" --package-checksums "$(DEBIAN_PACKAGE_CHECKSUMS)" \
		--output-directory "$(DEBIAN_BROWSER_WEB_QEMU_GATE_OUTPUT)" --smp 4 --boot-timeout 900

.PHONY: test_riscv_physical_graphics_qemu_gate
test_riscv_physical_graphics_qemu_gate:
	@test -n "$(DEBIAN_KERNEL)" || { echo "DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(DEBIAN_UBOOT)" || { echo "DEBIAN_UBOOT is required" >&2; exit 2; }
	@test -n "$(DEBIAN_DTB)" || { echo "DEBIAN_DTB is required" >&2; exit 2; }
	@test -n "$(DEBIAN_STAGE1_INITRAMFS)" || { echo "DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_IMAGE)" || { echo "DEBIAN_ROOT_IMAGE is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_MANIFEST)" || { echo "DEBIAN_ROOT_MANIFEST is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGES_LOCK)" || { echo "DEBIAN_PACKAGES_LOCK is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGE_CHECKSUMS)" || { echo "DEBIAN_PACKAGE_CHECKSUMS is required" >&2; exit 2; }
	@test -n "$(RISCV_PHYSICAL_GRAPHICS_QEMU_GATE_OUTPUT)" || { echo "RISCV_PHYSICAL_GRAPHICS_QEMU_GATE_OUTPUT is required" >&2; exit 2; }
	@PYTHONPATH="$(CURDIR)/tools/riscv:$(CURDIR)" \
		python3 -m tools.riscv.physical_graphics_qemu_gate \
		--kernel "$(DEBIAN_KERNEL)" --uboot "$(DEBIAN_UBOOT)" \
		--dtb "$(DEBIAN_DTB)" --stage1-initramfs "$(DEBIAN_STAGE1_INITRAMFS)" \
		--root-image "$(DEBIAN_ROOT_IMAGE)" --root-manifest "$(DEBIAN_ROOT_MANIFEST)" \
		--packages-lock "$(DEBIAN_PACKAGES_LOCK)" --package-checksums "$(DEBIAN_PACKAGE_CHECKSUMS)" \
		--output-directory "$(RISCV_PHYSICAL_GRAPHICS_QEMU_GATE_OUTPUT)" \
		--smp 4 --boot-timeout 7200 --command-timeout 300

.PHONY: test_riscv_debian_desktop_m6_browser_gate
test_riscv_debian_desktop_m6_browser_gate:
	@test -n "$(DEBIAN_KERNEL)" || \
		{ echo "DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(DEBIAN_UBOOT)" || \
		{ echo "DEBIAN_UBOOT is required" >&2; exit 2; }
	@test -n "$(DEBIAN_DTB)" || \
		{ echo "DEBIAN_DTB is required" >&2; exit 2; }
	@test -n "$(DEBIAN_STAGE1_INITRAMFS)" || \
		{ echo "DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_IMAGE)" || \
		{ echo "DEBIAN_ROOT_IMAGE is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_MANIFEST)" || \
		{ echo "DEBIAN_ROOT_MANIFEST is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGES_LOCK)" || \
		{ echo "DEBIAN_PACKAGES_LOCK is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGE_CHECKSUMS)" || \
		{ echo "DEBIAN_PACKAGE_CHECKSUMS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_DESKTOP_M6_BROWSER_GATE_OUTPUT)" || \
		{ echo "DEBIAN_DESKTOP_M6_BROWSER_GATE_OUTPUT is required" >&2; exit 2; }
	@python3 -m tools.riscv.debian.rootfs.desktop_m6_browser_gate \
		--kernel "$(DEBIAN_KERNEL)" \
		--uboot "$(DEBIAN_UBOOT)" \
		--dtb "$(DEBIAN_DTB)" \
		--stage1-initramfs "$(DEBIAN_STAGE1_INITRAMFS)" \
		--root-image "$(DEBIAN_ROOT_IMAGE)" \
		--root-manifest "$(DEBIAN_ROOT_MANIFEST)" \
		--packages-lock "$(DEBIAN_PACKAGES_LOCK)" \
		--package-checksums "$(DEBIAN_PACKAGE_CHECKSUMS)" \
		--output-directory "$(DEBIAN_DESKTOP_M6_BROWSER_GATE_OUTPUT)" --smp 4 \
		--boot-timeout "$(DEBIAN_DESKTOP_BOOT_TIMEOUT)"

.PHONY: test_riscv_debian_desktop_m7_baidu_gate
test_riscv_debian_desktop_m7_baidu_gate:
	@test -n "$(DEBIAN_KERNEL)" || \
		{ echo "DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(DEBIAN_UBOOT)" || \
		{ echo "DEBIAN_UBOOT is required" >&2; exit 2; }
	@test -n "$(DEBIAN_DTB)" || \
		{ echo "DEBIAN_DTB is required" >&2; exit 2; }
	@test -n "$(DEBIAN_STAGE1_INITRAMFS)" || \
		{ echo "DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_IMAGE)" || \
		{ echo "DEBIAN_ROOT_IMAGE is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_MANIFEST)" || \
		{ echo "DEBIAN_ROOT_MANIFEST is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGES_LOCK)" || \
		{ echo "DEBIAN_PACKAGES_LOCK is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGE_CHECKSUMS)" || \
		{ echo "DEBIAN_PACKAGE_CHECKSUMS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_DESKTOP_M7_BAIDU_GATE_OUTPUT)" || \
		{ echo "DEBIAN_DESKTOP_M7_BAIDU_GATE_OUTPUT is required" >&2; exit 2; }
	@python3 -m tools.riscv.debian.rootfs.desktop_m7_baidu_gate \
		--kernel "$(DEBIAN_KERNEL)" \
		--uboot "$(DEBIAN_UBOOT)" \
		--dtb "$(DEBIAN_DTB)" \
		--stage1-initramfs "$(DEBIAN_STAGE1_INITRAMFS)" \
		--root-image "$(DEBIAN_ROOT_IMAGE)" \
		--root-manifest "$(DEBIAN_ROOT_MANIFEST)" \
		--packages-lock "$(DEBIAN_PACKAGES_LOCK)" \
		--package-checksums "$(DEBIAN_PACKAGE_CHECKSUMS)" \
		--output-directory "$(DEBIAN_DESKTOP_M7_BAIDU_GATE_OUTPUT)" --smp 4 \
		--boot-timeout "$(DEBIAN_DESKTOP_BOOT_TIMEOUT)"

.PHONY: test_riscv_debian_desktop_m9_software_gate
test_riscv_debian_desktop_m9_software_gate:
	@test -n "$(DEBIAN_KERNEL)" || \
		{ echo "DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(DEBIAN_UBOOT)" || \
		{ echo "DEBIAN_UBOOT is required" >&2; exit 2; }
	@test -n "$(DEBIAN_DTB)" || \
		{ echo "DEBIAN_DTB is required" >&2; exit 2; }
	@test -n "$(DEBIAN_STAGE1_INITRAMFS)" || \
		{ echo "DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_IMAGE)" || \
		{ echo "DEBIAN_ROOT_IMAGE is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_MANIFEST)" || \
		{ echo "DEBIAN_ROOT_MANIFEST is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGES_LOCK)" || \
		{ echo "DEBIAN_PACKAGES_LOCK is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGE_CHECKSUMS)" || \
		{ echo "DEBIAN_PACKAGE_CHECKSUMS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_DESKTOP_M9_SOFTWARE_GATE_OUTPUT)" || \
		{ echo "DEBIAN_DESKTOP_M9_SOFTWARE_GATE_OUTPUT is required" >&2; exit 2; }
	@python3 -m tools.riscv.debian.rootfs.desktop_m9_software_gate \
		--kernel "$(DEBIAN_KERNEL)" \
		--uboot "$(DEBIAN_UBOOT)" \
		--dtb "$(DEBIAN_DTB)" \
		--stage1-initramfs "$(DEBIAN_STAGE1_INITRAMFS)" \
		--root-image "$(DEBIAN_ROOT_IMAGE)" \
		--root-manifest "$(DEBIAN_ROOT_MANIFEST)" \
		--packages-lock "$(DEBIAN_PACKAGES_LOCK)" \
		--package-checksums "$(DEBIAN_PACKAGE_CHECKSUMS)" \
		--output-directory "$(DEBIAN_DESKTOP_M9_SOFTWARE_GATE_OUTPUT)" --smp 4 \
		--boot-timeout "$(DEBIAN_DESKTOP_BOOT_TIMEOUT)"

.PHONY: test_riscv_ltp
test_riscv_ltp: test_riscv_ltp_unit
	@test -n "$(ASTERINAS_RISCV_BOOTI)" || \
		{ echo "ASTERINAS_RISCV_BOOTI is required" >&2; exit 2; }
	@python3 tools/riscv/ltp_gate.py run \
		--kernel "$(ASTERINAS_RISCV_BOOTI)" --smp "$(RISCV_LTP_SMP)" \
		--suite "$(RISCV_LTP_SUITE)"

.PHONY: test_riscv_xhci_input_unit
test_riscv_xhci_input_unit:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_xhci_input_gate -v

.PHONY: test_riscv_drm_cursor_unit
test_riscv_drm_cursor_unit:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_drm_cursor_gate -v

.PHONY: test_riscv_drm_cursor
test_riscv_drm_cursor: test_riscv_drm_cursor_unit
	@test -n "$(DRM_CURSOR_UBOOT)" || \
		{ echo "DRM_CURSOR_UBOOT is required" >&2; exit 2; }
	@test -n "$(DRM_CURSOR_BOOT_DISK)" || \
		{ echo "DRM_CURSOR_BOOT_DISK is required" >&2; exit 2; }
	@test -n "$(DRM_CURSOR_MANIFEST)" || \
		{ echo "DRM_CURSOR_MANIFEST is required" >&2; exit 2; }
	@test -n "$(DRM_CURSOR_GATE_OUTPUT)" || \
		{ echo "DRM_CURSOR_GATE_OUTPUT is required" >&2; exit 2; }
	@PYTHONPATH=tools/riscv python3 -m drm.cursor_gate \
		--uboot "$(DRM_CURSOR_UBOOT)" \
		--boot-disk "$(DRM_CURSOR_BOOT_DISK)" \
		--manifest "$(DRM_CURSOR_MANIFEST)" \
		--output-directory "$(DRM_CURSOR_GATE_OUTPUT)"

# The DRM ioctl command numbers the driver declares, held against the numbers
# the Linux uapi headers define. An ioctl number is `direction | size | type |
# number` packed into 32 bits and `ioc!` never reads its first argument, so a
# declaration can name one ioctl and encode another; libdrm then gets ENOTTY,
# which reads as "not implemented". Four debugging cycles went into that class
# of defect. This needs no kernel build and finishes in well under a second.
.PHONY: test_riscv_drm_uapi_contract
test_riscv_drm_uapi_contract:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_drm_uapi_contract -v

.PHONY: test_riscv_drm_gem_unit
test_riscv_drm_gem_unit:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_drm_gem_gate -v

.PHONY: test_riscv_drm_gem
test_riscv_drm_gem: test_riscv_drm_gem_unit
	@test -n "$(DRM_GEM_UBOOT)" || \
		{ echo "DRM_GEM_UBOOT is required" >&2; exit 2; }
	@test -n "$(DRM_GEM_BOOT_DISK)" || \
		{ echo "DRM_GEM_BOOT_DISK is required" >&2; exit 2; }
	@test -n "$(DRM_GEM_MANIFEST)" || \
		{ echo "DRM_GEM_MANIFEST is required" >&2; exit 2; }
	@test -n "$(DRM_GEM_GATE_OUTPUT)" || \
		{ echo "DRM_GEM_GATE_OUTPUT is required" >&2; exit 2; }
	@PYTHONPATH=tools/riscv python3 -m drm.gem_gate \
		--uboot "$(DRM_GEM_UBOOT)" \
		--boot-disk "$(DRM_GEM_BOOT_DISK)" \
		--manifest "$(DRM_GEM_MANIFEST)" \
		--output-directory "$(DRM_GEM_GATE_OUTPUT)"

.PHONY: test_riscv_drm_firmware_unit
test_riscv_drm_firmware_unit:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_drm_firmware_gate -v

.PHONY: test_riscv_drm_firmware
test_riscv_drm_firmware: test_riscv_drm_firmware_unit
	@test -n "$(DRM_FIRMWARE_UBOOT)" || \
		{ echo "DRM_FIRMWARE_UBOOT is required" >&2; exit 2; }
	@test -n "$(DRM_FIRMWARE_BOOT_DISK)" || \
		{ echo "DRM_FIRMWARE_BOOT_DISK is required" >&2; exit 2; }
	@test -n "$(DRM_FIRMWARE_MANIFEST)" || \
		{ echo "DRM_FIRMWARE_MANIFEST is required" >&2; exit 2; }
	@test -n "$(DRM_FIRMWARE_GATE_OUTPUT)" || \
		{ echo "DRM_FIRMWARE_GATE_OUTPUT is required" >&2; exit 2; }
	@PYTHONPATH=tools/riscv python3 -m drm.firmware_gate \
		--uboot "$(DRM_FIRMWARE_UBOOT)" \
		--boot-disk "$(DRM_FIRMWARE_BOOT_DISK)" \
		--manifest "$(DRM_FIRMWARE_MANIFEST)" \
		--output-directory "$(DRM_FIRMWARE_GATE_OUTPUT)"

.PHONY: test_riscv_drm_render_node_unit
test_riscv_drm_render_node_unit:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_drm_render_node_gate -v

.PHONY: test_riscv_drm_render_node
test_riscv_drm_render_node: test_riscv_drm_render_node_unit
	@test -n "$(DRM_RENDER_NODE_UBOOT)" || \
		{ echo "DRM_RENDER_NODE_UBOOT is required" >&2; exit 2; }
	@test -n "$(DRM_RENDER_NODE_BOOT_DISK)" || \
		{ echo "DRM_RENDER_NODE_BOOT_DISK is required" >&2; exit 2; }
	@test -n "$(DRM_RENDER_NODE_MANIFEST)" || \
		{ echo "DRM_RENDER_NODE_MANIFEST is required" >&2; exit 2; }
	@test -n "$(DRM_RENDER_NODE_GATE_OUTPUT)" || \
		{ echo "DRM_RENDER_NODE_GATE_OUTPUT is required" >&2; exit 2; }
	@PYTHONPATH=tools/riscv python3 -m drm.render_node_gate \
		--uboot "$(DRM_RENDER_NODE_UBOOT)" \
		--boot-disk "$(DRM_RENDER_NODE_BOOT_DISK)" \
		--manifest "$(DRM_RENDER_NODE_MANIFEST)" \
		--output-directory "$(DRM_RENDER_NODE_GATE_OUTPUT)"

.PHONY: test_riscv_drm_virgl_param_unit
test_riscv_drm_virgl_param_unit:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_drm_virgl_param_gate -v

# Run with `DRM_VIRGL_DEVICE_SET=drm-gem` and the same boot disk for the
# control: the same probe on a device without GL has to report no 3D.
.PHONY: test_riscv_drm_virgl_param
test_riscv_drm_virgl_param: test_riscv_drm_virgl_param_unit
	@test -n "$(DRM_VIRGL_UBOOT)" || \
		{ echo "DRM_VIRGL_UBOOT is required" >&2; exit 2; }
	@test -n "$(DRM_VIRGL_BOOT_DISK)" || \
		{ echo "DRM_VIRGL_BOOT_DISK is required" >&2; exit 2; }
	@test -n "$(DRM_VIRGL_MANIFEST)" || \
		{ echo "DRM_VIRGL_MANIFEST is required" >&2; exit 2; }
	@test -n "$(DRM_VIRGL_GATE_OUTPUT)" || \
		{ echo "DRM_VIRGL_GATE_OUTPUT is required" >&2; exit 2; }
	@PYTHONPATH=tools/riscv python3 -m drm.virgl_param_gate \
		--uboot "$(DRM_VIRGL_UBOOT)" \
		--boot-disk "$(DRM_VIRGL_BOOT_DISK)" \
		--manifest "$(DRM_VIRGL_MANIFEST)" \
		--output-directory "$(DRM_VIRGL_GATE_OUTPUT)" \
		--device-set "$(if $(DRM_VIRGL_DEVICE_SET),$(DRM_VIRGL_DEVICE_SET),drm-virgl)"

.PHONY: test_riscv_uboot_booti_unit
test_riscv_uboot_booti_unit:
	@python3 -m unittest \
		tools.riscv.tests.test_qemu_uboot_contracts \
		tools.riscv.tests.test_qemu_uboot_booti -v

.PHONY: test_riscv_uboot_booti
test_riscv_uboot_booti: test_riscv_uboot_booti_unit
	@ASTERINAS_RISCV_BOOTI="$(ASTERINAS_RISCV_BOOTI)" \
		ASTERINAS_INITRAMFS="$(ASTERINAS_INITRAMFS)" \
		QEMU_UBOOT_PROFILE="generic-sv39" \
		QEMU_UBOOT_OUT_DIR="$(QEMU_UBOOT_OUT_DIR_EFFECTIVE)" \
		QEMU_UBOOT_BUILD_DIR="$(QEMU_UBOOT_BUILD_DIR_EFFECTIVE)" \
		tools/riscv/prepare_qemu_uboot_booti.sh prepare
	@python3 tools/riscv/qemu_uboot_booti.py run \
		--profile "generic-sv39" \
		--uboot "$(QEMU_UBOOT_BUILD_DIR_EFFECTIVE)/u-boot" \
		--boot-disk "$(QEMU_UBOOT_OUT_DIR_EFFECTIVE)/boot.ext4" \
		--manifest "$(QEMU_UBOOT_OUT_DIR_EFFECTIVE)/artifacts.json" \
		--serial-log "$(QEMU_UBOOT_OUT_DIR_EFFECTIVE)/serial.log" \
		--marker-event "$(QEMU_UBOOT_OUT_DIR_EFFECTIVE)/marker-event.txt" \
		--result "$(QEMU_UBOOT_OUT_DIR_EFFECTIVE)/result.json"

.PHONY: test_riscv_sifive_u test_riscv_sifive_u_linux_reference
test_riscv_sifive_u: SIFIVE_U_PROFILE := sifive-u-asterinas-smoke
test_riscv_sifive_u: SIFIVE_U_KERNEL := $(ASTERINAS_RISCV_BOOTI)
test_riscv_sifive_u: SIFIVE_U_INITRAMFS := $(ASTERINAS_INITRAMFS)
test_riscv_sifive_u: SIFIVE_U_KERNEL_LABEL := ASTERINAS_RISCV_BOOTI
test_riscv_sifive_u: SIFIVE_U_INITRAMFS_LABEL := ASTERINAS_INITRAMFS
test_riscv_sifive_u: SIFIVE_U_OUT := $(RISCV_SIFIVE_U_OUT_DIR_EFFECTIVE)

test_riscv_sifive_u_linux_reference: SIFIVE_U_PROFILE := sifive-u-linux-reference
test_riscv_sifive_u_linux_reference: SIFIVE_U_KERNEL := $(RISCV_LINUX_IMAGE)
test_riscv_sifive_u_linux_reference: SIFIVE_U_INITRAMFS := $(RISCV_LINUX_INITRAMFS)
test_riscv_sifive_u_linux_reference: SIFIVE_U_KERNEL_LABEL := RISCV_LINUX_IMAGE
test_riscv_sifive_u_linux_reference: SIFIVE_U_INITRAMFS_LABEL := RISCV_LINUX_INITRAMFS
test_riscv_sifive_u_linux_reference: SIFIVE_U_OUT := $(RISCV_SIFIVE_U_LINUX_OUT_DIR_EFFECTIVE)

test_riscv_sifive_u test_riscv_sifive_u_linux_reference: test_riscv_uboot_booti_unit
	@test -s "$(SIFIVE_U_KERNEL)" || \
		(echo "$(SIFIVE_U_KERNEL_LABEL) is required and must be non-empty" >&2; exit 2)
	@test -s "$(SIFIVE_U_INITRAMFS)" || \
		(echo "$(SIFIVE_U_INITRAMFS_LABEL) is required and must be non-empty" >&2; exit 2)
	@ASTERINAS_RISCV_BOOTI="$(SIFIVE_U_KERNEL)" \
		ASTERINAS_INITRAMFS="$(SIFIVE_U_INITRAMFS)" \
		QEMU_UBOOT_PROFILE="$(SIFIVE_U_PROFILE)" \
		QEMU_UBOOT_OUT_DIR="$(SIFIVE_U_OUT)" \
		QEMU_UBOOT_BUILD_DIR="$(RISCV_SIFIVE_U_BUILD_DIR_EFFECTIVE)" \
		tools/riscv/prepare_qemu_uboot_booti.sh prepare
	@python3 tools/riscv/qemu_uboot_booti.py run \
		--profile "$(SIFIVE_U_PROFILE)" \
		--uboot "$(RISCV_SIFIVE_U_BUILD_DIR_EFFECTIVE)/u-boot.bin" \
		--boot-disk "$(SIFIVE_U_OUT)/boot.ext4" \
		--manifest "$(SIFIVE_U_OUT)/artifacts.json" \
		--dtb-audit "$(SIFIVE_U_OUT)/qemu-dtb-audit.json" \
		--serial-log "$(SIFIVE_U_OUT)/serial.log" \
		--marker-event "$(SIFIVE_U_OUT)/marker-event.txt" \
		--result "$(SIFIVE_U_OUT)/result.json"

RISCV_THIRD_BOARD_OUT_DIR ?= $(CURDIR)/target/qemu-uboot/third-board
RISCV_THIRD_BOARD_OUT_DIR_EFFECTIVE := $(call effective_path,$(RISCV_THIRD_BOARD_OUT_DIR),$(CURDIR)/target/qemu-uboot/third-board)

.PHONY: test_riscv_third_board
test_riscv_third_board: THIRD_BOARD_PROFILE := third-board-asterinas-smoke
test_riscv_third_board: THIRD_BOARD_KERNEL := $(ASTERINAS_RISCV_BOOTI)
test_riscv_third_board: THIRD_BOARD_INITRAMFS := $(ASTERINAS_INITRAMFS)
test_riscv_third_board: THIRD_BOARD_KERNEL_LABEL := ASTERINAS_RISCV_BOOTI
test_riscv_third_board: THIRD_BOARD_INITRAMFS_LABEL := ASTERINAS_INITRAMFS
test_riscv_third_board: THIRD_BOARD_OUT := $(RISCV_THIRD_BOARD_OUT_DIR_EFFECTIVE)

test_riscv_third_board: test_riscv_uboot_booti_unit
	@test -s "$(THIRD_BOARD_KERNEL)" || \
		(echo "$(THIRD_BOARD_KERNEL_LABEL) is required and must be non-empty" >&2; exit 2)
	@test -s "$(THIRD_BOARD_INITRAMFS)" || \
		(echo "$(THIRD_BOARD_INITRAMFS_LABEL) is required and must be non-empty" >&2; exit 2)
	@ASTERINAS_RISCV_BOOTI="$(THIRD_BOARD_KERNEL)" \
		ASTERINAS_INITRAMFS="$(THIRD_BOARD_INITRAMFS)" \
		QEMU_UBOOT_PROFILE="$(THIRD_BOARD_PROFILE)" \
		QEMU_UBOOT_OUT_DIR="$(THIRD_BOARD_OUT)" \
		QEMU_UBOOT_BUILD_DIR="$(QEMU_UBOOT_BUILD_DIR_EFFECTIVE)" \
		tools/riscv/prepare_qemu_uboot_booti.sh prepare
	@python3 tools/riscv/qemu_uboot_booti.py run \
		--profile "$(THIRD_BOARD_PROFILE)" \
		--uboot "$(QEMU_UBOOT_BUILD_DIR_EFFECTIVE)/u-boot.bin" \
		--boot-disk "$(THIRD_BOARD_OUT)/boot.ext4" \
		--manifest "$(THIRD_BOARD_OUT)/artifacts.json" \
		--dtb-audit "$(THIRD_BOARD_OUT)/qemu-dtb-audit.json" \
		--serial-log "$(THIRD_BOARD_OUT)/serial.log" \
		--marker-event "$(THIRD_BOARD_OUT)/marker-event.txt" \
		--result "$(THIRD_BOARD_OUT)/result.json"

.PHONY: check_vdso
check_vdso:
	@# Checking `VDSO_LIBRARY_DIR` environment variable
	@if [ -z "$(VDSO_LIBRARY_DIR)" ]; then \
		echo "Error: the VDSO_LIBRARY_DIR environment variable must be given."; \
		echo "    This variable points to a directory that provides Linux's vDSO files,"; \
		echo "    which is required to build Asterinas. Search for VDSO_LIBRARY_DIR"; \
		echo "    in Asterinas's Dockerfile for more information."; \
		exit 1; \
	fi

.PHONY: initramfs
initramfs: check_vdso
	@$(MAKE) --no-print-directory -C test/initramfs

# Build the kernel with an initramfs
.PHONY: kernel
kernel: initramfs $(CARGO_OSDK)
	@cd kernel && cargo osdk build $(CARGO_OSDK_BUILD_ARGS)

# Build the kernel with an initramfs and then run it
.PHONY: run_kernel
run_kernel: initramfs $(CARGO_OSDK)
	$(DEBIAN_APT_SMOKE_PRECHECK)
	@if [ "$(AUTO_TEST)" = debian_apt_smoke ]; then \
		set -eu; \
		run_image="$${TMPDIR:-/tmp}/asterinas-debian-apt-smoke.$$$$.ext2"; \
		cp --reflink=auto --sparse=always -- "$(DEBIAN_ROOT_IMAGE)" "$$run_image"; \
		cleanup() { rm -f -- "$$run_image"; }; \
		trap cleanup EXIT INT TERM; \
		(cd kernel && ASTERINAS_EXT2_DRIVE_FILE="$$run_image" cargo osdk run $(CARGO_OSDK_BUILD_ARGS)); \
	else \
		(cd kernel && cargo osdk run $(CARGO_OSDK_BUILD_ARGS)); \
	fi
# Validate the complete QEMU transcript, including fatal output after success.
ifneq ($(filter $(AUTO_TEST),conformance regression boot vsock),)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "$(AUTO_TEST)" $(if $(filter 1,$(RISCV_ICACHE_REQUIRE_SMP4)),--require-riscv-icache-smp4,)
else ifeq ($(AUTO_TEST), ext2_firefox_recovery)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "ext2-firefox-recovery" \
		--ext2-image "$${ASTERINAS_TEST_BUILD_DIR:-$(CURDIR)/test/initramfs/build}/ext2.img"
else ifeq ($(AUTO_TEST), exfat_syncfs_eio)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "exfat-syncfs-eio"
else ifeq ($(AUTO_TEST), exfat_fsync_eio)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "exfat-fsync-eio"
else ifeq ($(AUTO_TEST), exfat_sync_clean)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "exfat-sync-clean"
else ifeq ($(AUTO_TEST), exfat_sync_verify)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "exfat-sync-verify"
else ifeq ($(AUTO_TEST), ext2_rename_same_inode)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "ext2-rename-same-inode" \
		--ext2-image "$${ASTERINAS_TEST_BUILD_DIR:-$(CURDIR)/test/initramfs/build}/ext2.img"
else ifeq ($(AUTO_TEST), fs_syscall_compat)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "fs-syscall-compat"
else ifeq ($(AUTO_TEST), ipv6_dual_stack)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "ipv6-dual-stack"
else ifeq ($(AUTO_TEST), ipv6_dual_stack_udp)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "ipv6-dual-stack-udp"
else ifeq ($(AUTO_TEST), ipv6_udp)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "ipv6-udp"
else ifeq ($(AUTO_TEST), ifconf)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "ifconf"
else ifeq ($(AUTO_TEST), proc_net_dev)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "proc-net-dev"
else ifeq ($(AUTO_TEST), ifreq)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "ifreq"
else ifeq ($(AUTO_TEST), ip_socket_netns)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "ip-socket-netns"
else ifeq ($(AUTO_TEST), netlink_route_netns)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "netlink-route-netns"
else ifeq ($(AUTO_TEST), ifconf_gvisor)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "ifconf-gvisor"
else ifeq ($(AUTO_TEST), udp_user_buffer_prefault)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "udp-user-buffer-prefault"
else ifeq ($(AUTO_TEST), udp_msg_dontwait)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "udp-msg-dontwait"
else ifeq ($(AUTO_TEST), tcp_msg_dontwait_send)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "tcp-msg-dontwait-send"
else ifeq ($(AUTO_TEST), dynamic_clock)
	@tail --lines 100 "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" | tr -d '\r' | \
		grep -Fxq "Dynamic clock regression passed." \
		|| (echo "Dynamic clock regression failed" && exit 1)
else ifeq ($(AUTO_TEST), pty)
	@tail --lines 100 "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" | tr -d '\r' | \
		grep -Fxq "PTY regression passed." \
		|| (echo "PTY regression failed" && exit 1)
else ifeq ($(AUTO_TEST), memfd_exec)
	@tail --lines 100 "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" | tr -d '\r' | \
		grep -Fxq "memfd exec regression passed." \
		|| (echo "memfd exec regression failed" && exit 1)
else ifeq ($(AUTO_TEST), shebang_argv)
	@tail --lines 100 "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" | tr -d '\r' | \
		grep -Fxq "shebang argv regression passed." \
		|| (echo "shebang argv regression failed" && exit 1)
else ifeq ($(AUTO_TEST), sched_policy)
	@tail --lines 100 "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" | tr -d '\r' | \
		grep -Fxq "Scheduler policy regression passed." \
		|| (echo "Scheduler policy regression failed" && exit 1)
else ifeq ($(AUTO_TEST), kcmp)
	@tail --lines 100 "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" | tr -d '\r' | \
		grep -Fxq "kcmp regression passed." \
		|| (echo "kcmp regression failed" && exit 1)
else ifeq ($(AUTO_TEST), riscv_ptrace_regset)
	@tail --lines 100 "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" | tr -d '\r' | \
		grep -Fxq "RISC-V ptrace regset regression passed." \
		|| (echo "RISC-V ptrace regset regression failed" && exit 1)
else ifeq ($(AUTO_TEST), riscv_icache_smp4)
	@tail --lines 100 "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" | tr -d '\r' | \
		grep -Fxq "RISC-V SMP4 icache regression passed." \
		|| (echo "RISC-V SMP4 icache regression failed" && exit 1)
else ifeq ($(AUTO_TEST), tcp_user_buffer_prefault)
	@tail --lines 100 "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" | tr -d '\r' | \
		grep -Fxq "TCP user buffer prefault regression passed." \
		|| (echo "TCP user buffer prefault regression failed" && exit 1)
else ifeq ($(AUTO_TEST), tcp_ppoll_wakeup)
	@tail --lines 200 "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" | tr -d '\r' | \
		grep -Fxq "TCP ppoll wakeup regression passed." \
		|| (echo "TCP ppoll wakeup regression failed" && exit 1)
else ifeq ($(AUTO_TEST), tcp_event_handoff)
	@tail --lines 200 "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" | tr -d '\r' | \
		grep -Fxq "tcp_event_handoff: passed 4096 socket-to-queue-to-pipe handoffs" \
		|| (echo "TCP event handoff regression failed" && exit 1)
else ifeq ($(AUTO_TEST), pthread_cond_handoff)
	@tail --lines 200 "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" | tr -d '\r' | \
		grep -Fxq "pthread condition handoff regression passed." \
		|| (echo "pthread condition handoff regression failed" && exit 1)
else ifeq ($(AUTO_TEST), sysv_shm)
	@tail --lines 200 "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" | tr -d '\r' | \
		grep -Fxq "System V shared memory regression passed." \
		|| (echo "System V shared memory regression failed" && exit 1)
else ifeq ($(AUTO_TEST), ext4_directory_journal)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "ext4-directory-journal"
else ifeq ($(AUTO_TEST), ext4_concurrency)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "ext4-concurrency"
else ifeq ($(AUTO_TEST), ext4_semantics)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "ext4-semantics"
else ifeq ($(AUTO_TEST), debian_apt_smoke)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "debian-apt-smoke"
else ifeq ($(AUTO_TEST), xfstests_ext4)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "xfstests-ext4"
else ifeq ($(AUTO_TEST), ext4_regression)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "ext4-regression"
else ifeq ($(AUTO_TEST), ext4_recovery_verify)
	@python3 tools/riscv/validate_run_kernel_log.py \
		--log "$${ASTERINAS_QEMU_LOG_DIR:-$(CURDIR)}/qemu.log" \
		--mode "ext4-recovery-verify"
endif

# Build the Asterinas NixOS ISO installer image
iso: BOOT_PROTOCOL = linux-efi-handover64
iso:
	@make kernel
	@if [ -n "$(NIXOS_TEST_SUITE)" ]; then \
        $(MAKE) --no-print-directory -C test/nixos iso; \
    else \
        ./tools/nixos/build_iso.sh; \
    fi

# Build the Asterinas NixOS ISO installer image and then do installation
run_iso: OVMF = off
run_iso:
	@./tools/nixos/run.sh iso

# Create an Asterinas NixOS installation on host
nixos: BOOT_PROTOCOL = linux-efi-handover64
nixos:
	@make kernel
	@if [ -n "$(NIXOS_TEST_SUITE)" ]; then \
        $(MAKE) --no-print-directory -C test/nixos nixos; \
    else \
        ./tools/nixos/build_nixos.sh; \
    fi

# After creating a Asterinas NixOS installation (via either the `run_iso` or `nixos` target),
# run the NixOS
run_nixos: OVMF = off
run_nixos:
	@if [ -n "$(NIXOS_TEST_SUITE)" ]; then \
        $(MAKE) --no-print-directory -C test/nixos run_nixos; \
    else \
        ./tools/nixos/run.sh nixos; \
    fi

# Build the Asterinas NixOS patched packages
cachix:
	@nix-build distro/cachix \
		--option extra-substituters "${RELEASE_SUBSTITUTER} ${DEV_SUBSTITUTER}" \
		--option extra-trusted-public-keys "${RELEASE_TRUSTED_PUBLIC_KEY} ${DEV_TRUSTED_PUBLIC_KEY}" \
		--out-link cachix.list

# Push the Asterinas NixOS patched packages to Cachix
.PHONY: push_cachix
push_cachix: USE_RELEASE_CACHE ?= 0
push_cachix: cachix
ifeq ($(USE_RELEASE_CACHE), 1)
	@cachix push $(RELEASE_CACHIX_NAME) < cachix.list
else
	@cachix push $(DEV_CACHIX_NAME) < cachix.list
endif

.PHONY: gdb_server
gdb_server: initramfs $(CARGO_OSDK)
	@cd kernel && cargo osdk run $(CARGO_OSDK_BUILD_ARGS) --gdb-server wait-client,vscode,addr=:$(GDB_TCP_PORT)

.PHONY: gdb_client
gdb_client: initramfs $(CARGO_OSDK)
	@cd kernel && cargo osdk debug $(CARGO_OSDK_BUILD_ARGS) --remote :$(GDB_TCP_PORT)

.PHONY: profile_server
profile_server: initramfs $(CARGO_OSDK)
	@cd kernel && cargo osdk run $(CARGO_OSDK_BUILD_ARGS) --gdb-server addr=:$(GDB_TCP_PORT)

.PHONY: profile_client
profile_client: initramfs $(CARGO_OSDK)
	@cd kernel && cargo osdk profile $(CARGO_OSDK_BUILD_ARGS) --remote :$(GDB_TCP_PORT) \
		--samples $(GDB_PROFILE_COUNT) --interval $(GDB_PROFILE_INTERVAL) --format $(GDB_PROFILE_FORMAT)

.PHONY: test_klog_store
test_klog_store:
	@mkdir -p target
	@rustc --edition=2024 --test -D warnings kernel/comps/logger/src/klog/store_tests.rs -o target/klog-store-tests
	@target/klog-store-tests
	@rustc --edition=2024 --test -D warnings kernel/comps/logger/src/diagnostics_tests.rs -o target/log-diagnostics-tests
	@target/log-diagnostics-tests

.PHONY: test
test: NON_DEFAULT_PACKAGE_NAMES = \
    $(shell ./tools/print_workspace_members.sh --non-default-ones --package-names)
test: TEST_PACKAGE_NAMES = \
    $(filter-out linux-bzimage-setup,$(NON_DEFAULT_PACKAGE_NAMES))
test: test_klog_store
	@if [ -n "$(TEST_PACKAGE_NAMES)" ]; then \
		cargo test $(addprefix -p ,$(TEST_PACKAGE_NAMES)); \
	fi

.PHONY: ktest
ktest: CONSOLE = ttyS0
ktest: initramfs $(CARGO_OSDK)
	@# cargo-osdk tests default workspace members.
	@# `linux-bzimage-setup` is left out of `default-members`
	@# because it is hard to unit test.
	@cargo osdk test $(CARGO_OSDK_TEST_ARGS)

.PHONY: docs
docs: private DEFAULT_PACKAGE_NAMES = \
    $(shell ./tools/print_workspace_members.sh --default-ones --package-names)
docs: private DEFAULT_NON_KERNEL_PACKAGE_NAMES = \
    $(filter-out aster-kernel,$(DEFAULT_PACKAGE_NAMES))
docs: private NON_DEFAULT_PACKAGE_NAMES = \
    $(shell ./tools/print_workspace_members.sh --non-default-ones --package-names)
docs: private DOC_NON_DEFAULT_PACKAGE_NAMES = \
    $(filter-out linux-bzimage-setup,$(NON_DEFAULT_PACKAGE_NAMES))
docs: $(CARGO_OSDK)
	@if [ -n "$(DEFAULT_NON_KERNEL_PACKAGE_NAMES)" ]; then \
		RUSTDOCFLAGS="-Dwarnings" cargo osdk doc $(addprefix -p ,$(DEFAULT_NON_KERNEL_PACKAGE_NAMES)) --no-deps; \
	fi
	@if [ -n "$(DOC_NON_DEFAULT_PACKAGE_NAMES)" ]; then \
		RUSTDOCFLAGS="-Dwarnings" cargo doc $(addprefix -p ,$(DOC_NON_DEFAULT_PACKAGE_NAMES)) --no-deps; \
	fi
	@# The kernel crate is primarily composed of private items.
	@# Include --document-private-items to fully check internal documentation.
	@RUSTDOCFLAGS="-Dwarnings --document-private-items -Arustdoc::private_intra_doc_links" \
		cargo osdk doc -p aster-kernel --no-deps
	@if [ "$(TARGET_ARCH)" = "x86_64" ]; then \
		cd ostd/libs/linux-bzimage/setup && RUSTDOCFLAGS="-Dwarnings" cargo osdk doc --no-deps; \
	fi

.PHONY: book
book: book/mermaid.min.js book/mermaid-init.js
	@cd book && mdbook build

book/mermaid.min.js book/mermaid-init.js:
	@mdbook-mermaid install book/

.PHONY: format
format:
	@# Trim trailing whitespace from all git-tracked, non-patch files
	@# NOTE: `--git-dir` will suppress "detected dubious ownership in repository" errors
	@git --git-dir=$$PWD/.git ls-files --no-directory | \
		grep -v '[.]patch$$' | \
		xargs sed -i 's/ *$$//'
	@
	@# Format the code using various tools
	@./tools/format_all.sh
	@nixfmt ./distro
	@$(MAKE) --no-print-directory -C test/initramfs format
	@$(MAKE) --no-print-directory -C test/nixos format

.PHONY: check
check: private WORKSPACE_MEMBER_DIRS = \
    $(shell ./tools/print_workspace_members.sh)
check: $(CARGO_OSDK)
	@# Check if any git-tracked, non-patch files contain trailing whitespace
	@# NOTE: `--git-dir` will suppress "detected dubious ownership in repository" errors
	@if git --git-dir=$$PWD/.git ls-files | grep -v '[.]patch$$' | xargs grep -I -d skip ' $$' ; then \
		echo "Error: Files (as listed above) contain trailing whitespaces"; \
		exit 1; \
	fi
	@
	@# Check if all workspace members enable workspace lints
	@for dir in $(WORKSPACE_MEMBER_DIRS); do \
		if [[ "$$(tail -2 $$dir/Cargo.toml)" != "[lints]"$$'\n'"workspace = true" ]]; then \
			echo "Error: Workspace lints in $$dir are not enabled"; \
			exit 1; \
		fi; \
	done
	@
	@# Check formatting issues of the Rust code
	@./tools/format_all.sh --check
	@
	@# Check compilation of the Rust code
	@./tools/clippy_check.sh workspace
	@
	@# Check formatting issues of Nix files under distro directory
	@nixfmt --check ./distro
	@
	@# Check formatting issues of the C code and Nix files (regression tests)
	@$(MAKE) --no-print-directory -C test/initramfs check
	@
	@# Check formatting issues of the Rust code in NixOS tests
	@$(MAKE) --no-print-directory -C test/nixos check
	@
	@# Check typos
	@typos

.PHONY: clean
clean:
	@echo "Cleaning up Asterinas workspace target files"
	@cargo clean
	@echo "Cleaning up OSDK workspace target files"
	@cd osdk && cargo clean
	@echo "Cleaning up mdBook output files"
	@cd book && mdbook clean
	@echo "Cleaning up test target files"
	@$(MAKE) --no-print-directory -C test/initramfs clean
	@echo "Uninstalling OSDK"
	@rm -f $(CARGO_OSDK)
