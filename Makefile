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
RISCV_LTP_SMP ?= 4
RISCV_LTP_SUITE ?= syscalls
OSTD_TASK_STACK_SIZE_IN_PAGES ?= 64
FEATURES ?=
NO_DEFAULT_FEATURES ?= 0
COVERAGE ?= 0

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
# End of auto test features.

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
CARGO_OSDK_TEST_ARGS :=

ifeq ($(AUTO_TEST), conformance)
ENABLE_CONFORMANCE_TEST := true
CARGO_OSDK_BUILD_ARGS += --kcmd-args="CONFORMANCE_TEST_SUITE=$(CONFORMANCE_TEST_SUITE)"
CARGO_OSDK_BUILD_ARGS += --kcmd-args="CONFORMANCE_TEST_WORKDIR=$(CONFORMANCE_TEST_WORKDIR)"
CARGO_OSDK_BUILD_ARGS += --kcmd-args="EXTRA_BLOCKLISTS=$(EXTRA_BLOCKLISTS)"
ifeq ($(CONFORMANCE_TEST_SUITE), xfstests)
CARGO_OSDK_BUILD_ARGS += --kcmd-args="XFSTESTS_RUNLIST=$(XFSTESTS_RUNLIST)"
CARGO_OSDK_BUILD_ARGS += --kcmd-args="XFSTESTS_TEST_DEV=$(XFSTESTS_TEST_DEV)"
CARGO_OSDK_BUILD_ARGS += --kcmd-args="XFSTESTS_SCRATCH_DEV=$(XFSTESTS_SCRATCH_DEV)"
endif
CARGO_OSDK_BUILD_ARGS += --init-args="/opt/run_conformance_test.sh"
else ifeq ($(AUTO_TEST), regression)
ENABLE_REGRESSION_TEST := true
CARGO_OSDK_BUILD_ARGS += --kcmd-args="INTEL_TDX=$(INTEL_TDX)"
CARGO_OSDK_BUILD_ARGS += --init-args="/test/run_regression_test.sh"
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

# Skip GZIP to make encoding and decoding of initramfs faster
ifeq ($(INITRAMFS_SKIP_GZIP),1)
CARGO_OSDK_INITRAMFS_OPTION := --initramfs=$(abspath test/initramfs/build/initramfs.cpio)
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
	@OSDK_LOCAL_DEV=1 cargo install cargo-osdk --path osdk

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
DEBIAN_DESKTOP_BOOT_TIMEOUT ?= 420
DEBIAN_DESKTOP_M5_QEMU_GATE_TARGET ?= browser

effective_path = $(abspath $(or $(strip $(1)),$(2)))
QEMU_UBOOT_OUT_DIR_EFFECTIVE := $(call effective_path,$(QEMU_UBOOT_OUT_DIR),$(CURDIR)/target/qemu-uboot/current)
QEMU_UBOOT_BUILD_DIR_EFFECTIVE := $(call effective_path,$(QEMU_UBOOT_BUILD_DIR),$(CURDIR)/target/qemu-uboot/cache/u-boot-build)
RISCV_SIFIVE_U_OUT_DIR_EFFECTIVE := $(call effective_path,$(RISCV_SIFIVE_U_OUT_DIR),$(CURDIR)/target/qemu-uboot/sifive-u)
RISCV_SIFIVE_U_LINUX_OUT_DIR_EFFECTIVE := $(call effective_path,$(RISCV_SIFIVE_U_LINUX_OUT_DIR),$(CURDIR)/target/qemu-uboot/sifive-u-linux)
RISCV_SIFIVE_U_BUILD_DIR_EFFECTIVE := $(call effective_path,$(RISCV_SIFIVE_U_BUILD_DIR),$(CURDIR)/target/qemu-uboot/cache/sifive-u-uboot-build)
MEGREZ_DEBUG_FAST_OUT_DIR_EFFECTIVE := $(call effective_path,$(MEGREZ_DEBUG_FAST_OUT_DIR),$(CURDIR)/target/qemu-uboot/megrez-debug/fast)
MEGREZ_DEBUG_UBOOT_BUILD_DIR_EFFECTIVE := $(call effective_path,$(MEGREZ_DEBUG_UBOOT_BUILD_DIR),$(CURDIR)/target/qemu-uboot/megrez-debug/uboot)
MEGREZ_DEBUG_BOARD_OUT_DIR_EFFECTIVE := $(call effective_path,$(MEGREZ_DEBUG_BOARD_OUT_DIR),$(CURDIR)/target/megrez-debug/board)

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
		tools.riscv.tests.test_debian_rootfs \
		tools.riscv.tests.test_debian_m5_network \
		tools.riscv.tests.test_debian_m6_browser \
		tools.riscv.tests.test_debian_m7_baidu \
		tools.riscv.tests.test_debian_m8_browser_quality -v

.PHONY: test_riscv_megrez_debian_shell
test_riscv_megrez_debian_shell:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_megrez_debian_shell -v

.PHONY: test_riscv_megrez_gmac_unit
test_riscv_megrez_gmac_unit:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_megrez_gmac_contract \
		tools.riscv.tests.test_megrez_gmac_gate \
		tools.riscv.tests.test_megrez_network_fixture \
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

.PHONY: test_riscv_megrez_debug_desktop
test_riscv_megrez_debug_desktop:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_megrez_debug_desktop -v

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

.PHONY: test_riscv_debian_browser_m5_startup_probe
test_riscv_debian_browser_m5_startup_probe:
	@test -n "$(DEBIAN_KERNEL)" || { echo "DEBIAN_KERNEL is required" >&2; exit 2; }
	@test -n "$(DEBIAN_UBOOT)" || { echo "DEBIAN_UBOOT is required" >&2; exit 2; }
	@test -n "$(DEBIAN_DTB)" || { echo "DEBIAN_DTB is required" >&2; exit 2; }
	@test -n "$(DEBIAN_STAGE1_INITRAMFS)" || { echo "DEBIAN_STAGE1_INITRAMFS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_IMAGE)" || { echo "DEBIAN_ROOT_IMAGE is required" >&2; exit 2; }
	@test -n "$(DEBIAN_ROOT_MANIFEST)" || { echo "DEBIAN_ROOT_MANIFEST is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGES_LOCK)" || { echo "DEBIAN_PACKAGES_LOCK is required" >&2; exit 2; }
	@test -n "$(DEBIAN_PACKAGE_CHECKSUMS)" || { echo "DEBIAN_PACKAGE_CHECKSUMS is required" >&2; exit 2; }
	@test -n "$(DEBIAN_BROWSER_M5_STARTUP_PROBE_OUTPUT)" || \
		{ echo "DEBIAN_BROWSER_M5_STARTUP_PROBE_OUTPUT is required" >&2; exit 2; }
	@python3 -m tools.riscv.debian.rootfs.browser_m5_startup_probe \
		--kernel "$(DEBIAN_KERNEL)" \
		--uboot "$(DEBIAN_UBOOT)" \
		--dtb "$(DEBIAN_DTB)" \
		--stage1-initramfs "$(DEBIAN_STAGE1_INITRAMFS)" \
		--root-image "$(DEBIAN_ROOT_IMAGE)" \
		--root-manifest "$(DEBIAN_ROOT_MANIFEST)" \
		--packages-lock "$(DEBIAN_PACKAGES_LOCK)" \
		--package-checksums "$(DEBIAN_PACKAGE_CHECKSUMS)" \
		--output-directory "$(DEBIAN_BROWSER_M5_STARTUP_PROBE_OUTPUT)" \
		--smp 4 --boot-timeout 600

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
		--kernel "$(DEBIAN_KERNEL)" --uboot "$(DEBIAN_UBOOT)" --dtb "$(DEBIAN_DTB)" \
		--stage1-initramfs "$(DEBIAN_STAGE1_INITRAMFS)" \
		--root-image "$(DEBIAN_ROOT_IMAGE)" --root-manifest "$(DEBIAN_ROOT_MANIFEST)" \
		--packages-lock "$(DEBIAN_PACKAGES_LOCK)" --package-checksums "$(DEBIAN_PACKAGE_CHECKSUMS)" \
		--output-directory "$(DEBIAN_BROWSER_WEB_QEMU_GATE_OUTPUT)" --smp 4 --boot-timeout 7200

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

.PHONY: test_riscv_debian_desktop_m8_browser_quality_gate
test_riscv_debian_desktop_m8_browser_quality_gate:
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
	@test -n "$(DEBIAN_DESKTOP_M8_BROWSER_QUALITY_GATE_OUTPUT)" || \
		{ echo "DEBIAN_DESKTOP_M8_BROWSER_QUALITY_GATE_OUTPUT is required" >&2; exit 2; }
	@python3 -m tools.riscv.debian.rootfs.desktop_m8_browser_quality_gate \
		--kernel "$(DEBIAN_KERNEL)" \
		--uboot "$(DEBIAN_UBOOT)" \
		--dtb "$(DEBIAN_DTB)" \
		--stage1-initramfs "$(DEBIAN_STAGE1_INITRAMFS)" \
		--root-image "$(DEBIAN_ROOT_IMAGE)" \
		--root-manifest "$(DEBIAN_ROOT_MANIFEST)" \
		--packages-lock "$(DEBIAN_PACKAGES_LOCK)" \
		--package-checksums "$(DEBIAN_PACKAGE_CHECKSUMS)" \
		--output-directory "$(DEBIAN_DESKTOP_M8_BROWSER_QUALITY_GATE_OUTPUT)" \
		--smp 4 --boot-timeout "$(DEBIAN_DESKTOP_BOOT_TIMEOUT)"

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
	@cd kernel && cargo osdk run $(CARGO_OSDK_BUILD_ARGS)
# Check the running status of auto tests from the QEMU log
ifeq ($(AUTO_TEST), conformance)
	@tail --lines 100 qemu.log | grep -q "^All conformance tests passed." \
		|| (echo "Conformance test failed" && exit 1)
else ifeq ($(AUTO_TEST), regression)
	@tail --lines 100 qemu.log | grep -q "^All regression tests passed." \
		|| (echo "Regression test failed" && exit 1)
else ifeq ($(AUTO_TEST), boot)
	@tail --lines 100 qemu.log | grep -q "^Successfully booted." \
		|| (echo "Boot test failed" && exit 1)
else ifeq ($(AUTO_TEST), vsock)
	@tail --lines 100 qemu.log | grep -q "^Vsock test passed." \
		|| (echo "Vsock test failed" && exit 1)
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

.PHONY: test
test: NON_DEFAULT_PACKAGE_NAMES = \
    $(shell ./tools/print_workspace_members.sh --non-default-ones --package-names)
test: TEST_PACKAGE_NAMES = \
    $(filter-out linux-bzimage-setup,$(NON_DEFAULT_PACKAGE_NAMES))
test:
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
		grep -v '^.claude/skills/aster-code-review$$' `# This is a symbolic link` | \
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
