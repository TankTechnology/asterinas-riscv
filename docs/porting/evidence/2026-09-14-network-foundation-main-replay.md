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

The rootfs builder now exposes that frozen Firefox 143 RISC-V JIT overlay as
an explicit `browser-web`-only input.  It verifies the three pinned package
filenames and SHA-256 identities, records the installed overlay marker in the
schema-seven manifest, and reuses the existing package and Debian caches.  It
does not download an unpinned browser as part of the build.

The final cache-isolated root and current network kernel have these identities:

| Artifact | SHA-256 |
|---|---|
| Asterinas `Image` | `0c2da50816abea7d6be6aefc092acccd31b64f2421aceeed8047f2a5b97f956b` |
| Firefox JIT root image | `179403de54e757093e4135a228d33e075a20fb8dea6b24b5687f0bcfc45ea505` |
| schema-seven manifest | `7f3c9001942dcfeb4b858bc620fc99d4dd0aafc70473e4d0feb1481eb9ddf33a` |
| package lock | `a984d41af358a990685f1f77cd0561d035dc1f9fc3e8686623e639e2d8f4aaba` |
| package checksums | `200d5c468b1b8dfbb936be09a2670b4bb08d1dece2a00efe8d377ee22000f83a` |
| Stage-1 initramfs | `2a1da2995176a935be89ea4dccf5cb1a4f6db3cf800a2dcc37b6d8f5f02caed3` |

The proxy-mode replay with these exact inputs again completed all 20 fixture
transfers.  Firefox reported `wasm=true`, as well as working workers, IndexedDB,
canvas, local/session storage, and cookies.  It loaded and validated the live
Baidu home page, Bilibili home page, and a selected Bilibili detail page over
verified TLS before emitting `DEBIAN_BROWSER_WEB_PLATFORM_READY`.  The final
strict transaction remained fail-closed because the automated Baidu search
was redirected into Baidu's `wappass`/fingerprint path instead of producing a
search-result page.  This is an external-site transaction result after the
browser platform boundary passed; it is not evidence of a kernel, network,
TLS, or WebAssembly failure.  Evidence is retained under
`target/network-foundation-main/physical/qemu/firefox-jit-proxy-final/`.

The final bounded proxy and direct replays used those exact inputs.  Both
completed all ten network layers, exactly 20 owned-fixture transfers, strict
HTTPS checks, Firefox session creation, the deterministic text/search/download
fixture, and the public Baidu and Bilibili page checks before emitting
`DEBIAN_BROWSER_WEB_PLATFORM_READY`.  Neither mode accepted the final Baidu
automated-search result, so both remained fail-closed with
`DEBIAN_BROWSER_WEB_FAIL reason=baidu-search-not-pass`; the browser platform
boundary had already passed.  The serial identities are:

| Mode | Platform boundary | Fixture requests | Serial SHA-256 | Result SHA-256 |
|---|---|---:|---|---|
| proxy | pass | 20 | `737e5bd5011d8248d4a77269a251dd74cd6075d072548597c7c6ed4717d6d532` | `2af06e4d2c48b7eb4991d6baae0153e89cadee14ba9485966955480feed224c5` |
| direct | pass | 20 | `0566d3c1781e65c63772358e366df66a64d277d41ba68b10e2a70fa743a1daf6` | `f8336c93a7917f3f9bd3410bb2779c44586dd6bf6c74b7580ee713d069255c95` |

Evidence is retained under
`target/network-foundation-main/physical/qemu/firefox-network-final-{proxy,direct}/`.
The runner now publishes each completed structured phase atomically to
`browser-web-progress.json` and stderr, and shares one 900-second deadline
across U-Boot, kernel boot, network qualification, and Firefox.  A timeout is
classified by the last completed phase instead of being left to an unbounded
outer command.

One pre-final proxy run reported Python `bad marshal data` while importing a
standard-library module.  The immutable root imported the same module under
RISC-V user-mode emulation, and the on-disk source and bytecode hashes matched;
an unchanged retry crossed the import and reached the browser platform.  This
does not establish an ext2 allocator defect.  The evidence service now uses an
empty cache prefix under `/run` and disables bytecode writes, removing package
`.pyc` validation and rewriting from the boot-time observer's critical path.

One direct replay also stopped at `DEBIAN_ROOTFS_FAIL reason=dev-bind`, before
any network work.  Because the old browser runner did not treat the generic
Stage-1 failure as terminal, it waited for the 900-second deadline and
misclassified the result as a direct-network timeout.  The runner now stops on
that marker and preserves the specific Stage-1 reason.  An immediate retry
completed the direct network and Firefox platform boundaries above; the single
`dev-bind` observation is retained separately under
`firefox-network-final-direct-stage1-failure/` rather than being presented as
a proven filesystem root cause.

Proxy mode remains the deterministic physical-board path.  Neither the
WebAssembly check nor the live-site search criterion was relaxed to manufacture
a pass.

When running the root-owned gate inside the persistent Docker container, do
not invoke it through a second `sudo -E`.  The container process is already
root, and the configured sudo secure path omits `/usr/local/qemu/bin`; the
extra sudo layer therefore prevents `qemu-system-riscv64` from launching.

## Regression coverage

The host-side regression suite covers:

- a single browser protocol deadline, live structured progress, and
  phase-qualified timeout reasons;
- immediate, reason-preserving termination on generic Stage-1 failures;
- an ephemeral, non-writing Python import cache for the boot-time browser
  observer;
- monotonic guest network deadlines and the bounded HTTPS retry;
- batched repeated fixture transfers with exact size and digest validation;
- proxy/direct boot arguments and neighbor minimization;
- expanded FDT command headroom, preventing silent U-Boot truncation;
- command-echo rejection in FDT error detection; and
- physical network classification independent of a serial-only driver marker.

The final source qualification commands and their results are recorded in the
associated commit and handoff; generated images and physical transcripts stay
under `target/` and are not committed.
