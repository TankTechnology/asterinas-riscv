# RISC-V network foundation replay (2026-09-14)

This note records the qualification boundary for the first network-foundation
integration on the Megrez board.  It deliberately separates kernel/network
evidence from rootfs, recovery, and browser-package failures so that later
Firefox work does not reopen an already-qualified data path without evidence.

## Qualified artifacts

The release kernel was built in the persistent project container with:

```text
make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
```

The resulting artifact identities were:

| Artifact | Size | SHA-256 | CRC32 |
|---|---:|---|---|
| Asterinas `Image` | 5,970,904 | `3cc3f1dcc2a2fff2ece24f2d24fa4f6a79483e9a390be432e7d7e41fa080e719` | `c674fdbb` |
| compressed kernel | 1,845,657 | `483030e919656a8cef36ad123e952a01a50697f6d8ba1d6abda597b7374e8c9a` | `98ec6b3c` |
| stage-1 initramfs | 572,928 | `f4d9b349094bf2e4368de6edd5131c4e492cafe5905ffdd05a09c94b867e4401` | `15a88909` |

The kernel now emits `ASTERINAS_GMAC_READY` only after the selected GMAC has
been registered with the network stack and its interrupt has been rearmed.
This is the physical-device readiness boundary; the earlier selection marker
only proved that a device-tree candidate had been chosen.

## QEMU network replay

The proxy and direct schema-seven network-only gates both passed with SMP=4.
Each run completed all ten guest layers and exactly 20 owned-fixture transfers
of 65,536 bytes.  Both runs used the kernel and stage-1 identities above and
the same frozen root input:
`28d0bfc489dc3c38f5556ff559a200bf149d5230a9d602ca0fc0f0e7349d24e6`.

| Mode | Result | Fixture requests | Serial SHA-256 |
|---|---|---:|---|
| proxy | pass | 20 | `12f63913084971a93aa77518689d9fceacf18dcfc1da9018045f52efe35442a0` |
| direct | pass | 20 | `313c56f04a946c7fd5fbf3dabcb46e30c15346a4dc049c5058ea50e4cf03b866` |

Evidence is retained under
`target/network-foundation-main/physical/qemu/{proxy,direct}/`.

## Megrez replay and recovery boundary

An earlier candidate completed the complete physical network transaction,
including automatic recovery, under
`target/network-foundation-main/physical/network-isolated/`.

The exact release kernel above was then replayed with quiet console logging.
It emitted all ten proxy-mode network layers and the final
`DEBIAN_WEB_NETWORK_READY mode=proxy layers=10` marker.  The host fixture
independently recorded exactly 20 transfers of 65,536 bytes.  The gate result
was false only because recovery was not observed inside the gate deadline;
the board watchdog subsequently returned to RockOS.  Its evidence is under
`target/network-foundation-main/physical/network-error-final/` and binds the
boot payload through CRC32 `c674fdbb`.

Partition 2 did not contain the rootfs associated with this worktree.  It
contained uncommitted browser and safe-reboot scripts from another worktree.
In particular, that reboot script subtracts a 180-second guard from the
kernel deadline.  This explains both immediate quiesce with a 180-second
deadline and premature quiesce with a 300-second deadline.  Rewriting the
partition merely to turn this recovery-only failure green was intentionally
avoided.

A diagnostic replay at `loglevel=notice` reached `ASTERINAS_GMAC_READY` but
changed systemd timing enough to stall around `systemd-udev-trigger`, while
printing repeated compatibility warnings.  Production network boots therefore
use `loglevel=error asterinas.klog_capture=info`: the serial path stays quiet,
while the information-level kernel ring buffer remains available through
`dmesg`.

## Full Firefox probe

A clean proxy-mode browser-web run used the same release kernel and completed:

- all ten network layers;
- 20/20 owned-fixture transfers;
- strict HTTPS requests to Baidu and Bilibili;
- Firefox launch and Marionette session creation; and
- Baidu home navigation, DOM validation, and screenshot capture.

It then failed closed at the deterministic browser-quality page because the
Firefox ESR 140.15 binary in that root exposed no `WebAssembly` object.  The
serial evidence reports only `wasm=false`; worker, IndexedDB, audio, fetch,
canvas, storage, and cookies passed.  This is a browser-image capability
boundary, not evidence of a network or kernel crash.  The result is retained
under `target/network-foundation-main/physical/qemu/firefox-proxy-valid/`.

The repository already defines a frozen Firefox 143 RISC-V JIT overlay.  A
new immutable schema-seven root image must be built with that overlay and a
matching manifest before the full browser gate is replayed.  The Wasm check
must not be relaxed: doing so would turn a real missing browser capability
into a false pass.

When running the root-owned gate inside the persistent Docker container, do
not invoke it through a second `sudo -E`.  The container process is already
root, and the configured sudo secure path omits `/usr/local/qemu/bin`; the
extra sudo layer therefore prevents `qemu-system-riscv64` from launching.

## Regression coverage

The host-side regression suite covers:

- monotonic guest network deadlines and the bounded HTTPS retry;
- batched repeated fixture transfers with exact size and digest validation;
- proxy/direct boot arguments and neighbor minimization;
- expanded FDT command headroom, preventing silent U-Boot truncation;
- command-echo rejection in FDT error detection; and
- physical network classification independent of a serial-only driver marker.

The final source qualification commands and their results are recorded in the
associated commit and handoff; generated images and physical transcripts stay
under `target/` and are not committed.
