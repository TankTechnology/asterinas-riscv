# Renode prototype: a board-like RISC-V platform

Status: prototype. Not part of the default gates yet. `renode_smoke.py` boots the
release kernel on a small Renode platform whose serial port is presented as a
DesignWare APB UART, which QEMU cannot do: its RISC-V machines have no
DesignWare models at all, and no faithful reset/watchdog semantics.

## What it proves

`renode_smoke.py` reaches `ASTERINAS_BOOT_PHASE phase=banner-done` with:

- OpenSBI 1.9 (`fw_jump`, next address `0x80200000`),
- the release kernel `target/osdk/aster-kernel-osdk-bin.Image`,
- a one-hart platform: RAM at `0x80000000`, CLINT at `0x2000000`, PLIC at
  `0xc000000`, and `UART.NS16550 { wideRegisters: true }` at `0x10000000`,
- a device tree whose serial node is `snps,dw-apb-uart` with
  `reg-shift = <2>`, `reg-io-width = <4>`.

The wide-register 16550 uses the same register offsets as the DW APB UART
(RBR/THR `0x00`, IER `0x04`, IIR `0x08`, LCR `0x0c`, MCR `0x10`, LSR `0x14`), so
the kernel's real `dw_apb` driver binds and prints. This is the first
end-to-end exercise of that driver without the board.

## Two details that are easy to get wrong

1. **ISA extensions come from the device tree.** `ostd` reads
   `riscv,isa` / `riscv,isa-extensions` from `/cpus/cpu@0`, not from the CPU
   model. `riscv,isa` must advertise `zicbom`; without it the non-coherent DMA
   path takes an uncached alias that Renode's page-table backend refuses
   (`AccessDenied` while initializing the network DMA pools).
2. **Advertising `zicbom` without the instructions is safe here.** OpenSBI only
   sets menvcfg bits for it during boot, and this platform has no DMA device, so
   no `cbo.*` instruction is ever issued. Do not copy this onto a platform with
   a modeled NIC.

The CPU model itself stays on the extensions Renode's RISC-V core accepts
(`rv64gc_zicsr_zifencei`); its parser rejects `svpbmt`/`zicbom` in `cpuType`.

## Running

Build OpenSBI (the binary is not vendored):

```sh
git clone --depth 1 --branch v1.9 https://github.com/riscv-software-src/opensbi
cd opensbi
make PLATFORM=generic CROSS_COMPILE=riscv64-linux-gnu- \
     FW_JUMP=y FW_JUMP_ADDR=0x80200000 -j"$(nproc)"
```

Then:

```sh
tools/riscv/renode/renode_smoke.py \
  --opensbi /path/to/opensbi/build/platform/generic/firmware/fw_jump.bin
```

Renode must be installed and its launcher must find `dotnet` (a per-user
`~/.dotnet` is picked up automatically). The UART transcript is written to
`$HOME/renode-megrez-lite/uart.log`.

Serial SysRq (break + key) end to end, in the register and interrupt order the
board's hardware uses:

```sh
tools/riscv/renode/renode_sysrq.py --opensbi /path/to/fw_jump.bin
tools/riscv/renode/renode_sysrq.py --opensbi /path/to/fw_jump.bin \
    --cause rlsi --key o
```

It checks, in one boot: plain input is delivered and does nothing; break plus
an unbound key is delivered and does nothing; break plus the command key
takes the action, the platform records the reset/shutdown write OpenSBI
performs, and the guest stops echoing. `--cause rdi` injects the break as a
receive-data interrupt instead of a line-status one; `--key o` exercises the
shutdown path (`SYSCON write 0xdead`) instead of reboot (`0xbad`).

## Operational notes

- `uart CreateFileBackend @file true` (immediate flush) is required if anything
  polls the transcript while Renode runs. Without the second argument the
  backend buffers and the file only reaches disk when the emulator exits, which
  looks exactly like a guest that stopped printing mid-banner.
- `/usr/bin/renode` is a shell launcher that spawns `dotnet` as a child.
  Killing the launcher leaves the emulator running and eating a core; start it
  in its own process group and kill the group (`renode_smoke.py` does).

## Known gaps (next steps)

- **Userspace needs `init=/init`.** Without it the kernel probes
  `/sbin/init`, `/etc/init`, `/bin/init` and `/bin/sh`, none of which exist in
  the initramfs; `spawn_init_process` fails, the panic path calls
  `emergency_restart`, and with no reset device on this platform the guest
  halts at `ostd::power::emergency_restart` with no message (the PC is the
  evidence). With `bootargs = "init=/init"` the platform reaches
  `ASTERINAS_BOOT_PHASE phase=init-spawned` and runs an initramfs init.
- **RX input works; two false alarms are worth knowing.** `uart
  CreateFileBackend` is output-only, but Renode also has a PTY terminal
  (`emulation CreateUartPtyTerminal "term" "<path>" true` followed by
  `connector Connect sysbus.uart term`). Guest output flows through it and the
  host writes to the symlinked pty. Injected bytes do reach the DW APB driver's
  IRQ path and do wake a blocked `read()` on `/dev/ttyS0` (6/6 bytes in the
  original probe). Two things looked like a wakeup failure:
  - `/dev/console` is `tty0` unless the kernel command line has `console=`.
    This platform's bootargs only has `init=/init`, so a reader on `/dev/console`
    waits on the VT, which has no serial input source. Read `/dev/ttyS0` (which
    is what the board's debug console service uses) or pass `console=ttyS0`.
  - A probe that splits the transcript on the *last* `SYS_READ` reports a false
    negative: the guest loops back into `read()` as soon as it consumes a byte,
    so the write it performed is no longer after the newest marker. Count
    `GOT:` markers instead.
- **Break injection without a Renode build.** The monitor's `python` command
  runs IronPython with `self.Machine['sysbus.uart']`, so the model's private
  fields are reachable by reflection. `renode_sysrq.py` injects a break by
  queueing the break's zero byte (`WriteChar`), setting `lineStatus` to
  `DataReady | BreakIrqIndicator | TransmitHoldEmpty | TransmitterEmpty`,
  selecting `ReceiverLineStatusIrq`, and forcing the IRQ GPIO. Pause the
  machine around the poke (`pause` / `start`): the monitor's `python` does not
  exclude the emulation thread, and an unpaused guest services the IRQ that
  `WriteChar` raises before the line-status write lands.
- **The SysRq harness found a real driver bug.** The DW APB driver's RLSI ack
  reads `LSR`, which clears `LSR.BI` on real hardware; the receive path then
  saw the break's zero byte as ordinary data, delivered a NUL to the tty, and
  the SysRq filter used that NUL as the batch's first byte, cancelling the
  window. `sysrq b` would have failed on the board. `dw_apb` now remembers the
  pending break byte (`break_byte_pending`) and drops it in the receive path
  (`dw_apb_line_status_ack_consumes_the_breaks_zero_byte` ktest).
- **The C# peripheral in `plugin/` is still not loadable.** Renode 1.17 does
  not resolve externally built managed peripheral types (`E04`), and the
  `plugins/` directory holds native integration libraries. It is no longer
  needed for break injection, but a real DW register file (including the sync
  registers) would still require a Renode source build or the plugin SDK.
- **Reset semantics are modeled; the watchdog is not.** The CPU's reset
  vector is 0x1000, so the platform puts a 16-byte BootROM there (set a0/a1,
  jump to the OpenSBI entry at 0x80000000), and the device tree carries
  `syscon` + `syscon-reboot` + `syscon-poweroff` nodes that OpenSBI's generic
  reset driver consumes. A `Python.PythonPeripheral` at 0x10020000 records the
  write in `syscon-status.txt` and requests a reset (`0xbad`) or pauses
  (`0xdead`). This is the same contract the board's firmware provides
  (`Platform Reboot Device : eswin_eic770x_reset` in its OpenSBI 1.5 banner),
  so `renode_sysrq.py` can assert that a kernel-initiated restart actually
  reaches the platform's reset register. A full second boot does not complete
  here: Renode resets the CPU to the BootROM and OpenSBI starts again, but the
  generic firmware then stalls in `sbi_hsm_init` before printing. The
  assertion stops at the platform write.
- **Timer interrupts do not reach S-mode in this platform.** The kernel's
  high-resolution timers (the `asterinas.reboot_after` deadline and the
  watchdog heartbeat) never fire here. Evidence from the model, not the
  kernel: `sbi_set_timer` returns success and OpenSBI stops the CLINT
  (`mtimecmp = 0xFFFF...FF`), `MIP.STIP` stays pending, `MIE.STIE` and
  `MIDELEG.STIP` are set, yet no trap is taken; forcing `sstatus.SIE` from the
  monitor does not help. Page faults and PLIC (UART) interrupts do work, so
  trap entry itself is fine. This looks like Renode's CPU not delivering the
  internal S-timer interrupt; it is why the watchdog-handoff test
  (`DW WDT` heartbeat) and a full second boot are not part of this prototype.
  Do not use this platform for timer-dependent tests until it is resolved.
- **Peripherals.** eMMC, GMAC, USB and DRM models are absent; the kernel sees
  no such devices and skips their drivers.
