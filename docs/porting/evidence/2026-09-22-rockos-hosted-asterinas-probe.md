# RockOS-hosted Asterinas probe

## Result

Both guests passed the two selected probes and exited cleanly.
The remote guest ran successfully while RockOS stayed available over SSH.
Afterward, a fresh serial root handshake also confirmed the unchanged host boot identity.

| Host | QEMU version | Probe result | Session wall time | Exit |
| --- | --- | --- | ---: | ---: |
| Developer persistent container | 10.2.1 | `boot`, `syscall213` pass | 5.530 s | 0 |
| RockOS board | 9.2.0 | `boot`, `syscall213` pass | 102.774 s | 0 |

These are one-shot session durations, not a controlled performance benchmark.
Host hardware and QEMU versions differ.
No native Asterinas speedup or slowdown can be inferred from their ratio.

## Scope

The user requested remote Asterinas simulation on the running RISC-V RockOS board,
and asked whether using the same ISA makes the result more representative.
This experiment keeps RockOS running and uses the existing diskless probe protocol.
It does not boot Asterinas on the physical board or start Firefox.

## Current virtualization capability

Fresh SSH inspection of the current RockOS boot established:

- Host kernel: `6.6.87`; boot ID `6e0c2402-86cf-4fb6-99f4-385bbdc8543d`.
- Installed QEMU: `9.2.0`, Debian package `1:9.2.0+ds-5rockos1`.
  The binary lists both TCG and KVM, which does not prove that KVM is usable.
- CPU ISA string includes `h`; the running configuration has `CONFIG_KVM=m`.
- `/dev/kvm` is absent and no KVM module is loaded.
- `modprobe -n -v kvm` fails because `/lib/modules/6.6.87` is missing.
  The installed module instead has vermagic `6.6.87-win2030 SMP mod_unload riscv`.
- More than 9 GiB RAM was available before launch, with approximately 70 GiB free on `/`.

The existing original kernel and module still match the earlier offline KVM design:

| Board artifact | SHA-256 |
| --- | --- |
| `/boot/vmlinuz-6.6.87-win2030-origin` | `4af973c27aa87679aad75966dd816665466837cb8adf473e3222c523195d1f3c` |
| `/lib/modules/6.6.87-win2030/kernel/arch/riscv/kvm/kvm.ko` | `defc0e79c280b0ebfc705df13fccdc3c927b69d657c3fcb493db085f0397c0df` |

That design and its pure menu qualifier are retained in
`/home/ubuntu/.config/superpowers/worktrees/asterinas/rockos-kvm-once/`,
at commit `c70c24306`.
Its physical one-shot boot was explicitly deferred in the earlier plan.
This run did not load a module, create `/dev/kvm`, change the host kernel,
or publish a KVM boot menu.
KVM availability remains unverified until a compatible kernel/module pair is actually running.

## Experiment identity and method

The inputs are the frozen `gp`-fix kernel from the September 22 validation
and the existing Stage1 archive with the bounded probe protocol:

| Input | Bytes | SHA-256 |
| --- | ---: | --- |
| `.worktrees/megrez-playback-v2/target/riscv-trap-gp/artifacts/kernel.Image` | 15943616 | `ae905fc0f4b78a10175729931ca0d717f2d6a31f5595898b074f74f72d25e8e2` |
| `target/debian-riscv/stage1-dns-fix/initramfs.cpio` | 1044480 | `872e38f947e18c225f2df47a645d419c04c42d536a6c5f518226dbc9d243593d` |

The existing `dual_host_qemu_probe` helpers snapshot and hash both inputs,
run the developer baseline, copy the exact pair into the hash-named RockOS cache,
verify the remote hashes, and run the same nonce-bound protocol remotely.
There is one guest run per host, not a tuning sweep.

Both guests use QEMU `virt`, four vCPUs, 2 GiB RAM, `-accel tcg`,
no disks, `-nic none`, and `-no-reboot`.
The selected tests are `boot` and `syscall213`;
the latter checks the expected ENOSYS response, not implemented readahead support.
Each QEMU process has a 210-second outer limit and a five-second termination allowance.
RockOS QEMU runs as the ordinary `debian` user at nice level 10.
The guest protocol requests a reboot and requires clean QEMU exit;
it does not reboot RockOS.

The first CLI invocation stopped before any QEMU launch because an older
root-owned `target-ubuntu/dual-host-qemu-probe/snapshots` directory was not writable.
No permissions were changed and no prior evidence was overwritten.
The existing helpers were then called with a fresh user-owned snapshot/evidence directory:

```text
target/megrez-debug/rockos-qemu-20260922-k0fyvnkd/
```

Its `developer-qemu/` and `rockos-qemu-tcg/` subdirectories contain independent
`result.json` and `serial.log` outputs.
The capability inventory is `rockos-capabilities.log`.
Results are explicitly marked `physical:false`.

## Host preservation and evidence

After the probe, SSH returned `active` for its service,
`NO_QEMU_REMAINS`, and `P2_UNMOUNTED`.
Both the active selector and vendor menu retained their previous SHA-256 values.
The root UART response contained the fresh standalone marker
`HOST_CONTROL_9850e085ad2f26f9`, UID 0 and host boot ID
`6e0c2402-86cf-4fb6-99f4-385bbdc8543d`.
The serial descriptor was released afterward, leaving the authenticated shell available.
RockOS itself was not rebooted by this experiment.

The only board artifacts created were verified kernel/Stage1 cache files under
`/tmp/asterinas-qemu-probe/ae905fc0f4b78a10-872e38f947e18c22/`.
They are retained for reuse.
No physical partition was exposed to QEMU.

| Local evidence file, relative to the run directory | SHA-256 |
| --- | --- |
| `developer-qemu/result.json` | `08f53ed9454d28c86871325050f85b282bf5bc4c7fc8a92c9be8d321b29e06ee` |
| `developer-qemu/serial.log` | `3c0a41959f8f0c5915a07b640bd3292bc664b02ceea88bb9b7a94af60904e0cf` |
| `rockos-qemu-tcg/result.json` | `0c76e0194c5f08f0384df3ba9d057652986592dac60ac5fbee95e108096d0bee` |
| `rockos-qemu-tcg/serial.log` | `b400b21bcc4d95a0fe1cb026311aaf95c73fe1ea70b1567c9109920d09477da4` |
| `rockos-capabilities.log` | `73296fd9b0eb56273a6fabe82799f3731b8ceee41e1298ac6456b388dcc3f477` |
| `rockos-after-probe.log` | `a9575325815f52a430b589ecd36a7e69a6e25f47f51b63877eee1216a24a6a32` |
| `serial-after-probe.log` | `0e8af15f5e83c0647eb6ed6c225f5f01c32d2e60c0ecb06e4612b06fe64b12eb` |

## Interpretation boundary

[QEMU's accelerator documentation](https://www.qemu.org/docs/master/system/introduction.html)
distinguishes software translation through TCG from hardware virtualization.
Using a RISC-V host does not automatically select KVM or make TCG native execution.
Running another TCG backend is useful for cross-checking functional behavior,
but neither its wall time nor a passing probe establishes physical-board performance or concurrency correctness.

[The RISC-V `virt` platform](https://www.qemu.org/docs/master/system/riscv/virt.html)
is a synthetic machine, not a model of Megrez.
Even with KVM, virtual devices and interrupt handling remain different from bare-metal operation.
Real MMC, Ethernet, USB, HDMI, display latency and browser video playback need physical-board validation.
