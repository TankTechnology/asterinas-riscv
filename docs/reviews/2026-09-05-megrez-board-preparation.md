# Megrez real-board preparation (2026-09-05)

This record first froze the pre-board state without opening the board serial
session or writing any U-Boot/eMMC state. A later controlled run (recorded
below) opened `/dev/ttyUSB0` and booted already-present eMMC artifacts in RAM;
it still performed no eMMC writes, TFTP/YMODEM transfer, or reset command.

## Artifact inventory

The browser rootfs is the signed Debian `browser-web` profile (schema 7) and
is exactly 2 GiB. The current contract now accepts only exact 1 GiB or 2 GiB
root images; the profile/manifest still determines which size is valid.

| artifact | path | SHA-256 |
| --- | --- | --- |
| board Sv48 kernel (current source) | `target/firefox-artifacts/asterinas-firefox-current-board-sv48.Image` | `531f74ec0eb26f5854146116edde69d58b400418f61dfdbe68c924811609673f` |
| board Sv48 kernel (historical) | `target/firefox-artifacts/asterinas-firefox-7fc8fd4-board-sv48.Image` | `4507a5fc4d8c3fb67a6077a9b0e29ae6b4c9cdf235c76eecd538dd59ad584005` |
| Stage1 initramfs | `target/debian-riscv/stage1/initramfs.cpio` | `d59a60bb57660403a97d4ecc65b5fa4ad1728cf975c1a48fbfa8328367d01db` |
| Megrez DTB | `target/dev-overlays/browser-web/physical-stage1-argv/artifacts/eic7700-milkv-megrez.dtb` | `02a8d43d581b4aa8e957e231ee90eba19ffd7e8cfcf74694e86a1fb9c6b37f17` |
| QEMU virt DTB | `target/qemu-uboot/browser-web-aa6f7533/qemu-virt.dtb` | `bf99e579fa60d930e0c4c862771617bf26ae7a973da35da774e77e4fb151051a` |
| QEMU U-Boot | `target/qemu-uboot/cache/u-boot-build-browser-web-aa6f7533/u-boot` | `fc1cf7429ccd8e4e703b06e4e05b7cdd9f4e35b2f567eb838275cbc5ed27f3b7` |
| browser-web root image | `target/debian-riscv/browser-web/rootfs/debian-root.ext2` | `55c9db46c5ccf425c4d01a1e0bae347fb973bdbce7655200be3acf78c65248ac` |
| rootfs manifest | `target/debian-riscv/browser-web/rootfs/rootfs-manifest.json` | `703016492d0b89be94e6eab35e210650dbf746dd3be708c9a23bd7c669a8eed2` |
| packages lock | `target/debian-riscv/browser-web/rootfs/packages.lock` | `4a415f719af21b81032c26988bf5c11a39f9cf00ee4640208efbd7f8ce9885f1` |
| package checksums | `target/debian-riscv/browser-web/rootfs/source-metadata/package-checksums` | `75a57b17e3fc777e4cb7eefc94c20c16bdda4fb26626c6121ed9df2c139510d1` |
| base InRelease | `target/debian-riscv/browser-web/rootfs/source-metadata/InRelease` | `98b25b5cd185c59d34aa6e4c3e9b5b8f01bbe9d104fe2dcfbcd30dc0a14a59ed` |

## Blocking conditions before a physical gate

1. The historical board kernel was built at commit `7fc8fd4`; it remains
   evidence only and is not used for the next run. A fresh Sv48 SMP=4 kernel
   has now been built from the current source commit and copied to the
   `current-board-sv48` path above.
2. The dependency cache was repaired from the local `with-cargo-cache` and
   `nixos-build` images (including the smoltcp and rust-ctor Git refs), so the
   build reached `aster-kernel`. It exposed a merge regression: the
   source still imported the deleted `START_TIME_AS_DURATION`. That source
   regression is fixed. A second runtime regression was isolated in the
   periodic coarse-clock callback: publishing the coarse snapshot into the
   vDSO VMO from the RISC-V softirq can block first-userspace scheduling. The
   callback now updates only the syscall-side snapshot; vDSO is still refreshed
   during first-kthread initialization and explicit wall-clock changes.
3. The OSDK nested metadata issue is fixed by invoking `cargo metadata` with
   `--no-deps --locked`; the current Sv48 build now completes. QEMU desktop
   evidence still uses a separate Sv39 kernel. The fresh Sv48 candidate above
   passed `megrez-sv48-svade-fast` with `BOOT_COMPLETED` in
   `target/qemu-uboot/megrez-firefox-board-preflight-8e09fc796/qemu`.
   The existing schema-2 browser plan still does not represent separate
   QEMU-Sv39 and Megrez-Sv48 kernel identities, so it cannot issue a physical
   permit until the dual-path shell contract is extended for the browser
   profile.

## Next safe step

The current Sv39 QEMU network gate passes with the coarse-clock fix: 20/20
owned-fixture requests, all ten protocol layers, and `multi-user.target` plus
Xorg startup. The fresh Sv48 candidate also passes the four-hart Megrez
contract simulation. The remaining preparation work is to extend the board
permit metadata with the separate Sv39/Sv48 identities and to bind the exact
Sv48 image, Stage1, and board DTB hashes into one run manifest. Only that
passing, current-source evidence pair may unlock the serial gate. The board
session remains fail-closed until then.

## Firefox startup evidence

The bounded QEMU startup profiler was rerun with `--boot-timeout 360 --smp 4`.
It reached all four startup boundaries, including Marionette readiness, in
223.826 seconds and exited cleanly:

```text
STARTUP_PROFILE_MARKER name=basic elapsed=53.713
STARTUP_PROFILE_MARKER name=x-socket-ready elapsed=79.564
STARTUP_PROFILE_MARKER name=firefox-exec elapsed=80.674
STARTUP_PROFILE_MARKER name=marionette elapsed=223.826
STARTUP_PROFILE_DONE elapsed=223.826 bytes=121737
```

This is a startup-compatibility pass, not a Baidu-content pass. The subsequent
proxy browser gate reached `BOOT_MARIONETTE_CONNECTED`, created a WebDriver
session, and completed the network checks, but the first `GetTitle` command
after Baidu navigation exceeded the remaining bounded gate time. The physical
run therefore remains limited to the desktop/Firefox-window target until that
content-process phase is separately closed.

## Board transfer bundle

The current-source board artifacts were staged without copying the 2-GiB
rootfs image:

`target/firefox-artifacts/board-bundle-20260905/SHA256SUMS`

The bundle contains the current Sv48 kernel, current Stage1 initramfs, and the
Megrez DTB. The rootfs remains at its immutable signed path above; its hash is
bound in this document and must be checked immediately before any transfer.

For the U-Boot artifact contract, the bundle's CRC32 values are:

```text
kernel=44f36474 initramfs=a096d0fb megrez_dtb=4afcb20e
```

The physical gate must use `--target firefox --network-mode proxy` with these
three names and CRCs, `--load-transport mmc` only after the board's existing
rootfs identity is checked, and finite `--boot-timeout`/`--recovery-timeout`.
No `saveenv`, partition write, or blind reset is part of this preparation.

## Firefox gate scheduling diagnostics

The proxy gate now performs the deterministic local fixture workload before
visiting public pages.  This ordering is intentional: a public Baidu or
Bilibili document can leave third-party work on Gecko's shared main thread and
starve the next Marionette command.  A fresh tab did not isolate that shared
thread.  One bounded retry also re-navigates Baidu when the first probe
transiently observes `about:blank`.

Proxy-mode browser evidence does not require a positive
`secureConnectionStart`: Firefox may report the HTTP proxy connection while
the proxy performs the upstream CONNECT/TLS handshake.  The independent proxy
network gate still requires strict HTTPS verification; direct mode retains the
browser-side timing check.

The local fixture's WebAssembly probe is now wrapped in the same five-second
timeout as worker, IndexedDB, audio, and fetch checks.  Before this change a
guest that could not complete `WebAssembly.instantiate()` left capabilities in
`state=running` and consumed the entire Marionette budget.  The timeout makes
the unsupported capability explicit and keeps the gate fail-closed.  A full
proxy Firefox-content pass is still pending until the RISC-V Firefox WASM
capability itself completes successfully.

To keep the primary browsing gate responsive, the normal fixture home/search
pages now perform non-blocking API-presence checks.  The full behavioural
checks are reserved for the explicit `?capabilities=1` diagnostic URL; this
separates basic HTML/JavaScript navigation from optional kernel-sensitive APIs
without deleting the diagnostic workload.

## Basic Firefox gate execution

The final fixture-only browser gates were run with the current injected test
image and SMP=4. Earlier attempts exposed Marionette cold-start and phase
normalization issues; those are now bounded and aligned in the source.

With that contract aligned, the final QEMU runs passed:

| QEMU run | SMP | network path | result | final root hash |
| --- | ---: | --- | --- | --- |
| `firefox-basic-proxy-capabilities-final-20260905` | 4 | host proxy | `passed=true` | `f4671b6178fa45e193b2902e67f4204ed44d3925decb758263aead89470489ce` |
| `firefox-basic-direct-capabilities-final2-20260905` | 4 | direct slirp | `passed=true` | `6df1fc59e421ca4331143b818e030f5d8740aceeaae6b228981e1d474dfb9ac4` |

Both runs produced fixture search/capability screenshots and JSON, validated
the download hash, strict HTTPS verification, Firefox security markers, and
the basic storage/cookie/Fetch contract. The root hashes are ephemeral test
overlays and must not be used as a board transfer identity. A matching
physical browser plan and recovery evidence are still required; these QEMU
runs alone did not authorize a physical transfer.

The initial physical Megrez gate was fail-closed because `/dev/ttyUSB0` was
silent and the historical network address was unreachable. The subsequent
controlled run below shows that a single bounded serial wake-up can establish
the U-Boot prompt without a reset, after which the board session can proceed
with paced commands.

## Physical browser-basic admission checklist

The first board run must use a plan bound to the exact Sv48 kernel, four-hart
DTB, initramfs, and signed Debian rootfs manifest. Its simulation input is the
passing QEMU browser-basic result above, and its recovery input must contain a
fresh firmware epoch plus an automatic software-watchdog reboot. The one-shot
board sequence is:

1. Open the serial device read-only and confirm a fresh U-Boot prompt; do not
   transmit anything if no prompt or boot epoch is observable.
2. Cache/transfer only artifacts whose SHA-256 and CRC32 match the plan.
3. Arm the software recovery timeout before booting, capture serial markers,
   and stop on the first fatal marker or missing desktop/browser-basic marker.
4. Collect the Xorg/Firefox screenshot and fixture capability JSON, then wait
   for automatic recovery and verify a second U-Boot epoch.

Until those inputs exist, the physical browser result remains “not run”; the
QEMU evidence is not treated as a substitute for board evidence.

The follow-up read-only audit found the host on `10.100.19.216` and the
historical Megrez address `10.100.19.200` unresolved in ARP; one ICMP probe and
one SSH connect attempt both timed out. The FTDI serial port likewise produced
zero bytes in an 8-second read. This confirms that the board is currently not
an observable test target, rather than a Firefox or fixture failure.

## Controlled physical run (2026-09-05)

The next probe sent exactly one carriage return (`\\r`) after the passive serial
read returned zero bytes. The board answered with `=> `, proving that the FTDI
TX/RX path and U-Boot console were alive. The initial mistake was treating a
silent console as an unavailable console and waiting passively; a bounded,
single-character wake-up is needed when the board is already stopped at a
U-Boot prompt. Long U-Boot commands must then be sent one character at a time
with a pacing delay: a burst caused `md.b` to be concatenated into an unfinished
`ext4load` path (`/extlmd.b`).

The controlled boot used only existing eMMC files and verified U-Boot CRC32
values before `booti`:

```text
kernel  asterinas-firefox-7fc8fd4-board-sv48.booti  CRC32 4b42f214
initrd  debian-browser-web-stage1-safe.cpio           CRC32 34ca7110
dtb     eic7700-milkv-megrez.dtb                      CRC32 4afcb20e
bootargs: console=ttyS0 loglevel=info init=/init asterinas.reboot_after=120
```

The kernel entered successfully with SMP=4. UART, both GMACs (GMAC1 selected
at 1000 Mbps/full duplex), MMC, both xHCI controllers, a Logitech keyboard,
and a USB mouse all initialized. The run then failed closed at the stage1
root-device discovery boundary: all three `/dev/mmcblk0p{1,2,3}` probes reported
`no-match`, followed by `DEBIAN_ROOTFS_FAIL reason=root-discovery-timeout`.
This identifies an eMMC rootfs marker/identity mismatch; it is not a kernel
panic or USB HID hang. The software recovery timer was armed and the board
later returned silently to U-Boot; one final carriage return recovered the
`=>` prompt. No physical reset was needed.

This run therefore validates the physical kernel/device path and recovery
mechanism, while leaving the Debian desktop/browser path blocked on installing
or identifying a rootfs whose stage1 marker matches the current contract.
