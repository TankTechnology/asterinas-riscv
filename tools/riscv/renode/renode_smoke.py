#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Status: prototype -- Renode boot smoke test; not part of the default gates.
"""Boot the release kernel under Renode on a board-like platform.

Why Renode: QEMU's RISC-V machines have no DesignWare models at all, so the
board's DW APB UART driver cannot be exercised end to end there, and QEMU has
no faithful reset/watchdog semantics. This platform gives the kernel:

* the DW APB UART register layout (via ``UART.NS16550 { wideRegisters: true }``)
  presented with the ``snps,dw-apb-uart`` compatible, so the real ``dw_apb``
  driver binds and prints;
* a CLINT/PLIC pair that OpenSBI and the kernel both understand;
* a device tree written by the kernel's own parsers.

Two details are easy to miss:

* The kernel takes ISA extensions from the device tree, not from the CPU, so
  ``riscv,isa`` must advertise ``zicbom``. Without it the DMA path for the
  non-coherent virtio buffers needs an uncached alias that Renode's page-table
  backend refuses (``AccessDenied`` during component init).
* OpenSBI only uses ``zicbom`` to set menvcfg bits during boot, so advertising
  it without a CPU that implements cbo instructions is safe as long as the
  guest never issues them. This platform has no DMA device, so it does not.

Requires a built OpenSBI ``fw_jump.bin`` for the generic platform. Build it
with:

    git clone --depth 1 --branch v1.9 https://github.com/riscv-software-src/opensbi
    cd opensbi
    make PLATFORM=generic CROSS_COMPILE=riscv64-linux-gnu- \\
         FW_JUMP=y FW_JUMP_ADDR=0x80200000 -j"$(nproc)"

Usage:

    tools/riscv/renode/renode_smoke.py --opensbi /path/to/fw_jump.bin
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DEFAULT_KERNEL = REPO / "target/osdk/aster-kernel-osdk-bin.Image"
DEFAULT_WORKDIR = Path.home() / "renode-megrez-lite"
BANNER_MARKERS = (
    "ASTERINAS_BOOT_PHASE phase=banner-done",
    "ASTERINAS_BOOT_PHASE phase=init-spawned",
)

# The BootROM at 0x1000 is where the CPU lands after a reset (Renode's RISC-V
# reset vector). It stands in for the SoC boot ROM: set a0/a1 the way firmware
# does and jump to the OpenSBI entry at 0x80000000, so a guest-initiated reset
# boots the whole stack again.
# Assembled and checked with:
#   li a0, 0; li a1, 0x88000000; li t0, 0x80000000; jr t0
# The `li` sequences must not use a bare `lui` for 0x80000000: on RV64 `lui`
# sign-extends, which would jump to the kernel's direct-map alias.
BOOTROM = bytes(
    [
        0x01, 0x45,              # c.li a0, 0
        0x9B, 0x05, 0x10, 0x01,  # addiw a1, zero, 17
        0xEE, 0x05,              # c.slli a1, 27    ; a1 = 0x88000000 (DTB)
        0x9B, 0x02, 0x10, 0x00,  # addiw t0, zero, 1
        0xFE, 0x02,              # c.slli t0, 31    ; t0 = 0x80000000
        0x82, 0x82,              # c.jr t0
    ]
)

# `syscon-reboot` / `syscon-poweroff` are what OpenSBI's generic reset driver
# uses; the board's own firmware reports an ESWIN reset device, and the point
# of this model is the same contract: a reset request from the guest resets
# the machine. The Python peripheral records the write and acts on it.
SYSCON_SCRIPT = """if request.IsInit:
    value = 0
elif request.IsWrite:
    value = request.Value & 0xFFFFFFFF
    f = open(r'{status}', 'a')
    f.write("SYSCON write 0x%x\\n" % value)
    f.close()
    if value == 0xBAD:
        self.GetMachine().RequestReset()
    elif value == 0xDEAD:
        self.GetMachine().Pause()
elif request.IsRead:
    request.Value = value
"""

PLATFORM = """rom: Memory.MappedMemory @ sysbus 0x1000
    size: 0x1000

mem: Memory.MappedMemory @ sysbus 0x80000000
    size: 0x40000000

cpu: CPU.RiscV64 @ sysbus
    cpuType: "rv64gc_zicsr_zifencei"
    timeProvider: clint

clint: IRQControllers.CoreLevelInterruptor @ sysbus 0x2000000
    frequency: 1000000
    numberOfTargets: 1
    [0, 1] -> cpu@[3, 7]

plic: IRQControllers.PlatformLevelInterruptController @ sysbus 0xc000000
    numberOfSources: 96
    numberOfContexts: 2
    0 -> cpu@11
    1 -> cpu@9

uart: UART.NS16550 @ sysbus 0x10000000
    wideRegisters: true
    IRQ -> plic@10

syscon: Python.PythonPeripheral @ sysbus 0x10020000
    size: 0x1000
    initable: true
    filename: "{syscon_script}"
"""


def device_tree(initramfs_size: int) -> str:
    initrd_start = 0x88300000
    return f"""/dts-v1/;
/ {{
    #address-cells = <2>;
    #size-cells = <2>;
    compatible = "megrez-lite";
    chosen {{
        stdout-path = "/soc/serial@10000000";
        // Without init= the kernel probes /sbin/init, /etc/init, /bin/init
        // and /bin/sh; the initramfs only has /init, so the spawn fails and
        // the panic path lands in emergency_restart (there is no reset device
        // on this platform) and the guest halts silently.
        bootargs = "init=/init";
        linux,initrd-start = <0x0 {initrd_start:#x}>;
        linux,initrd-end   = <0x0 {initrd_start + initramfs_size:#x}>;
    }};
    cpus {{
        #address-cells = <1>;
        #size-cells = <0>;
        timebase-frequency = <1000000>;
        cpu0: cpu@0 {{
            device_type = "cpu";
            reg = <0>;
            status = "okay";
            compatible = "riscv";
            // The kernel reads extensions from this property. zicbom keeps
            // the non-coherent DMA path on the direct mapping; OpenSBI only
            // sets menvcfg bits for it and this platform issues no cbo.
            riscv,isa = "rv64imafdc_zicsr_zifencei_zicbom";
            mmu-type = "riscv,sv39";
            clock-frequency = <1000000000>;
            cpu0_intc: interrupt-controller {{
                #interrupt-cells = <1>;
                interrupt-controller;
                compatible = "riscv,cpu-intc";
            }};
        }};
    }};
    memory@80000000 {{
        device_type = "memory";
        reg = <0x0 0x80000000 0x0 0x40000000>;
    }};
    // OpenSBI's generic platform turns these two nodes into its reboot and
    // shutdown devices, the same contract the board's firmware exposes as
    // `Platform Reboot Device : eswin_eic770x_reset`.
    syscon: syscon@10020000 {{
        compatible = "syscon";
        reg = <0x0 0x10020000 0x0 0x1000>;
    }};
    reboot {{
        compatible = "syscon-reboot";
        regmap = <&syscon>;
        offset = <0x0>;
        value = <0xbad>;
    }};
    poweroff {{
        compatible = "syscon-poweroff";
        regmap = <&syscon>;
        offset = <0x0>;
        value = <0xdead>;
    }};
    soc {{
        #address-cells = <2>;
        #size-cells = <2>;
        compatible = "simple-bus";
        ranges;
        clint@2000000 {{
            compatible = "riscv,clint0";
            reg = <0x0 0x2000000 0x0 0x10000>;
            interrupts-extended = <&cpu0_intc 3 &cpu0_intc 7>;
        }};
        plic: interrupt-controller@c000000 {{
            compatible = "riscv,plic0";
            reg = <0x0 0xc000000 0x0 0x4000000>;
            interrupts-extended = <&cpu0_intc 11 &cpu0_intc 9>;
            interrupt-controller;
            #interrupt-cells = <1>;
            riscv,ndev = <96>;
        }};
        serial@10000000 {{
            compatible = "snps,dw-apb-uart";
            reg = <0x0 0x10000000 0x0 0x1000>;
            reg-shift = <2>;
            reg-io-width = <4>;
            clock-frequency = <100000000>;
            interrupt-parent = <&plic>;
            interrupts = <10>;
            status = "okay";
        }};
    }};
}};
"""


def find_renode() -> tuple[str, dict[str, str]]:
    renode = shutil.which("renode")
    if renode is None:
        raise SystemExit("renode is not installed (expected /usr/bin/renode)")
    environment = dict(os.environ)
    dotnet = Path.home() / ".dotnet"
    if dotnet.is_dir() and (dotnet / "dotnet").is_file():
        # Renode's launcher needs `dotnet` on PATH.
        environment["PATH"] = f"{dotnet}:{environment.get('PATH', '')}"
    return renode, environment


def build_fixtures(workdir: Path, kernel: Path, opensbi: Path) -> Path:
    sys.path.insert(0, str(REPO))
    from tools.riscv.qemu.recovery_sim import build_pause_initramfs

    workdir.mkdir(parents=True, exist_ok=True)
    # The fixture builder refuses to reuse a directory; keep its output
    # separate so repeated runs are reproducible.
    build_dir = workdir / "build"
    shutil.rmtree(build_dir, ignore_errors=True)
    build_dir.mkdir(parents=True)
    initramfs = build_pause_initramfs(build_dir)
    (workdir / "bootrom.bin").write_bytes(BOOTROM)
    syscon_script = workdir / "syscon.py"
    syscon_script.write_text(
        SYSCON_SCRIPT.format(status=workdir / "syscon-status.txt"), encoding="utf-8"
    )
    (workdir / "megrez-lite.repl").write_text(
        PLATFORM.format(syscon_script=syscon_script), encoding="utf-8"
    )
    (workdir / "megrez-lite.dts").write_text(
        device_tree(initramfs.stat().st_size), encoding="utf-8"
    )
    dtb = workdir / "megrez-lite.dtb"
    subprocess.run(
        ["dtc", "-I", "dts", "-O", "dtb", "-o", str(dtb), str(workdir / "megrez-lite.dts")],
        check=True,
    )
    if not kernel.is_file():
        raise SystemExit(f"missing kernel image: {kernel}")
    if not opensbi.is_file():
        raise SystemExit(
            f"missing OpenSBI fw_jump: {opensbi}\n"
            "Build it as described in this script's docstring."
        )
    return initramfs


def run(workdir: Path, kernel: Path, opensbi: Path, timeout: float) -> bool:
    renode, environment = find_renode()
    initramfs = build_fixtures(workdir, kernel, opensbi)
    uart_log = workdir / "uart.log"
    if uart_log.exists():
        uart_log.unlink()

    commands = "; ".join(
        [
            'mach create "megrez-lite"',
            f"machine LoadPlatformDescription @{workdir}/megrez-lite.repl",
            f"sysbus LoadBinary @{opensbi} 0x80000000",
            f"sysbus LoadBinary @{kernel} 0x80200000",
            f"sysbus LoadBinary @{workdir}/megrez-lite.dtb 0x88000000",
            f"sysbus LoadBinary @{initramfs} 0x88300000",
            f"sysbus LoadBinary @{workdir}/bootrom.bin 0x1000",
            "cpu PC 0x1000",
            f"uart CreateFileBackend @{uart_log} true",
            "logLevel 3",
            "start",
        ]
    )
    run_log = workdir / "renode.log"
    with open(run_log, "wb") as log:
        # The launcher script spawns dotnet as a child; killing only the
        # launcher leaves the emulator running. Put the whole tree in its own
        # process group and kill the group.
        process = subprocess.Popen(
            [renode, "--disable-gui", "--plain", "-P", "-1", "-e", commands],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if process.poll() is not None:
                break
            if uart_log.exists():
                text = uart_log.read_text(errors="replace")
                if any(marker in text for marker in BANNER_MARKERS):
                    return True
            time.sleep(0.5)
        return False
    finally:
        import signal

        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.wait(timeout=10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opensbi", type=Path, required=True, help="built fw_jump.bin")
    parser.add_argument("--kernel", type=Path, default=DEFAULT_KERNEL)
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)

    # The work directory is kept on purpose: the UART transcript is the
    # evidence, and it lives in a scratch directory that is safe to inspect.
    ok = run(args.workdir, args.kernel, args.opensbi, args.timeout)
    text = ""
    uart_log = args.workdir / "uart.log"
    if uart_log.exists():
        text = uart_log.read_text(errors="replace")
    tail = [line for line in text.splitlines() if line.strip()][-8:]
    if not ok and (args.workdir / "renode.log").exists():
        print("--- renode.log tail ---")
        for line in (args.workdir / "renode.log").read_text(errors="replace").splitlines()[-6:]:
            print("  " + line[:160])
    print("banner reached" if ok else "banner NOT reached")
    for line in tail:
        print("  " + line[:160])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
