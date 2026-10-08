# Firefox startup: optimized kernel candidate

Follow-up: [fault-window and rseq safety validation](2026-09-12-fault-window-and-rseq.md)
records the subsequent kernel fixes, regression tests and physical observations.
The measurements below remain the earlier release-profile baseline.

## Outcome and limits

The existing `release` build removes substantial overhead from the RISC-V
kernel's startup paths. No new scheduler, page-cache algorithm, browser flag,
or recurring boot script was needed. The candidate is built from
`01b6e1eda` in `codex/megrez-boot-main`, with the same Sv39 feature and SMP4
test configuration as the unoptimized comparison kernel.

The component results below have now been followed by physical Desktop tests.
The release canary first detected a visible Firefox window at menu +74.004 s
and +69.906 s in two separate boots.
The same-source dev kernel detected its window at +282.190 s,
an observed approximately fourfold difference for these runs.
The first boot also passed local-page JavaScript and DOM-event checks,
and a verified 1920x1080 framebuffer capture shows the resulting page.
These observations do not qualify Internet browsing or physical USB input.
The installed default menu and frozen integration kernel remain unchanged;
complete promotion qualification and network-source parity are still required.
The separate QEMU headless screenshot experiment remains a failure,
not successful evidence superseded by the physical test.

## Root cause evidence

The installed kernel `485b9079c204...` was built with `profile = "dev"`:
the retained `network-stack-integration/target/osdk/aster-kernel/bundle.toml`
records that profile and a 15,718,208-byte Image, matching the deployed
artifact. The comparison source-built kernel also used the unoptimized dev
profile. `Makefile` defaults `RELEASE` to zero; `RELEASE=1` already selects
OSDK's `--release`. The workspace's release profile uses thin LTO.

The minimal intervention was to select this existing build mode, not to
change default debugging behavior or disable browser security. Comparing
these profiles changes compiler optimizations and profile defaults together;
the experiment does not isolate an individual optimization or debug check.

## Memory/file component probe

[`startup_cost_probe.c`](../../../tools/riscv/diagnostics/startup_cost_probe.c)
performs eight fixed-size phases with real syscalls and memory accesses.
It prints monotonic wall time, this process's CPU time, and CPU time of
reaped children separately. In particular, the parent's CPU time alone must
not be interpreted as the cost of the fork/COW phase.

The probe creates an immediately unlinked temporary file under `/tmp`.
The QEMU test uses the RAM-backed initramfs root, not the Debian ext2 disk.
File reads follow writes and are therefore warm; mapping reads operate on
cached file pages. The anonymous first-write phase has fresh pages. Correctness
checks use page-stride accesses: anonymous/mapping/COW loops touch one byte
per 4 KiB page, whereas buffered file I/O transfers all 32 MiB. Thus 32 MiB
describes the touched mapping/COW footprint, not bytes explicitly stored by
those page-stride loops. Correctness
checks verify sampled data, private-write isolation, successful child exits,
and cleanup. They are not an exhaustive VM conformance suite.

Final probe, same binary/initramfs, four harts, 2 GiB, no disk or NIC:

| Phase | Dev wall seconds | Release wall seconds |
| --- | ---: | ---: |
| Anonymous first write, 32 MiB | 2.456 | 0.152 |
| Anonymous warm write, 32 MiB | 0.0020 | 0.0014 |
| File write, 32 MiB | 0.945 | 0.190 |
| Warm file read, 32 MiB | 0.317 | 0.102 |
| First mapping read, 32 MiB | 0.408 | 0.0084 |
| Private file COW, 32 MiB | 7.092 | 0.323 |
| Six mprotect operations, 32 MiB | 1.239 | 0.0090 |
| Three forks, touching every page of 32 MiB | 21.204 | 0.893 |

Both runs completed all phases and exited zero. The final C probe was compiled
with `-O2 -DNDEBUG -Wall -Wextra -Werror` for both host Linux and RISC-V;
its `REQUIRE` checks deliberately remain active under `-DNDEBUG`.
An additional host run with a 16,000 KiB address-space limit failed at the
initial mmap with `errno=12`, emitted `STARTUP_COST_FAIL`, and exited one,
confirming that allocation failures do not silently become timing results.
Earlier exploratory release runs independently observed private COW at
0.249/0.262 s and fork/COW at 0.810/0.797 s. These are observations, not
confidence intervals or CI speed thresholds. The host was not CPU-pinned.

## Real Firefox executable loading

The component harness mounts the existing Debian browser-web image read-only,
adds proc/sys/dev mounts and RAM-backed writable temporary directories, and
executes the installed Firefox ESR 140.15.0 as UID/GID 1000. No package or
root-image rebuild is involved. Each fresh QEMU guest runs `firefox --version`
twice; the first has a fresh guest page cache, and the second is guest-cache
warm. The host's disk cache is not flushed, so this is not a cold-storage test.

Two final alternating pairs used a version-only initramfs, avoiding overlap
with the earlier headless diagnostic:

| Run order | Build | First execution (s) | Second execution (s) |
| --- | --- | ---: | ---: |
| 1 | Dev | 21.918 | 2.496 |
| 2 | Release | 0.616 | 0.169 |
| 3 | Dev | 20.957 | 2.402 |
| 4 | Release | 0.549 | 0.169 |

All eight version commands returned zero and printed the expected ESR version;
all four guests reached the explicit end marker with no panic. The initial
paired observations were 22.012/2.543 seconds in dev versus 0.823/0.167 seconds
in release (first/second execution). These agree in direction but are not
pooled with the final version-only runs. The numbers measure loading and
version reporting, **not** window creation,
Marionette readiness, first paint, or web navigation.

A subsequent headless `about:blank` screenshot attempt in each profile
reached its 90-second timeout (exit 124). The release run reported
`RenderCompositorSWGL failed mapping default framebuffer, no dt`.
The harness has no X server or framebuffer; the log is not sufficient to
attribute that failure to one missing device or kernel interface. This
headless setup is not the installed Xorg Desktop path. No sandbox workaround
was added, and the failure is not counted as successful startup.

## Build and reuse

The initial optimized build finished offline in 39.31 seconds using four
build jobs and existing caches. It retained the same 12 baseline warnings.
No Docker image, compiler, OSDK installation or dependency download was needed.
The source-built OSDK executable from the preceding investigation was reused:

```sh
# Inside the persistent development container, from kernel/:
OSDK_TARGET_ARCH=riscv64 OSDK_LOCAL_DEV=1 CARGO_BUILD_JOBS=4 \
  ../osdk/target/debug/cargo-osdk osdk build --release --scheme riscv \
  --features riscv_sv39_mode \
  --initramfs ../target/startup-clock-units/cost-before-1/initramfs.cpio
```

For the usual complete kernel build, select `RELEASE=1` on `make kernel`.
For focused experiments, reuse an existing initramfs as above to avoid a
full rootfs build. Preserve the output `bundle.toml` before another build
overwrites it; artifact size alone is not proof of the selected profile.
The deployment workflow must still use a new candidate generation, not
overwrite a frozen kernel path in the active menu.

Compile the standalone component probe with the existing target C compiler:

```sh
cc -O2 -DNDEBUG -Wall -Wextra -Werror \
  tools/riscv/diagnostics/startup_cost_probe.c -o /tmp/startup-cost-probe
timeout -k 5 120 /tmp/startup-cost-probe
```

Use a RISC-V cross compiler and cached static libc for a diskless RISC-V
initramfs. The one-off local `run_cost.py` reuses the existing Stage1 archive
utilities and `qemu_probe_argv`, has a 90-second host timeout, and records
argv, hashes, serial output and an explicit completion marker. It is not a
new production boot command. The probe requires 4 KiB pages, approximately
128 MiB of transient memory for the fork/COW phase plus runtime overhead,
and an external timeout; do not run it as PID 1.

## Acceptance evidence and provenance

The optimized kernel passed the existing diskless Basic shell/API-filesystem
check, automatic Probe/clean-reboot check, and all 11 assertions of the
procfs/auxv/CPU-clock regression. Basic took 0.556 s and Probe 0.487 s for
these QEMU check runs, not physical boot times. No fresh full kernel unit-test
suite, network test or board hardware acceptance is claimed.

Local raw artifacts are retained under `target/startup-clock-units/`:

- `cost-final-dev-1/`, `cost-final-release-1/`: final probe runs.
- `cost-before-1/`, `cost-frozen-1/`, `cost-release-1/`, `cost-release-2/`:
  earlier exploratory runs, including the frozen deployed kernel copy.
- `basic-release/`, `probe-release/`, `clock-release/`: compatibility checks.
- `build-cost-release.log`: successful optimized build.
- `release-bundle.toml`, `frozen-dev-bundle.toml`: profile provenance.
- `firefox-load-evidence/`: guest command harness and real Firefox logs.

Kernel SHA-256:

- Dev source baseline: `03786f0bfaadd016b230d24d797e8e1b5972b58475eb8a90ee2886dd3c695404`.
- Release candidate: `be9c00f58ba0ce7dee681964bfc787ffffdcbd2e1ede1d59fbc2a2b9135090ba`.
- Frozen board baseline: `485b9079c204bf6b34055f5e1061f3011381557d8cc4b4bf2d1e4831922058c1`.
- Final component-test initramfs: `bad170e145c4b359d2cb5c6fa1fb8fa4a9ec70cd9a5e71f30280cc5d9808ba41`.
- Version-only Firefox initramfs: `8c87ba5b9d4dcd8e735479b2d04708d5f67ef31e8aaa2c1772f7a76e009ec6cc`.
- Read-only Debian root image: `acfce6b25ca27aedba7a80aabcd243bbaced95e4771ed84fc3ce7210213b9997`.

The first Firefox harness attempt incorrectly requested Linux `devtmpfs`,
which Asterinas does not expose under that filesystem name. Its init exited
before Firefox ran. The corrected harness uses the existing Stage1 pattern
of bind-mounting `/dev`. That attempt is a harness failure, not a release
kernel regression or a performance data point. QEMU exit zero alone is not
success: the headless timeout also returned the guest cleanly to QEMU.

## Next qualification boundary

Use the release candidate with the existing prepared DTB, Stage1, Debian root
and normal Xorg Desktop launch. Measure Firefox exec-to-visible-window with
the same fresh-profile policy and compare it with the dev kernel on the
same board. Record menu time separately from browser time. Only promote
after the established recovery and mode checks pass; retain the current menu
until then. Do not infer a 20-fold desktop speedup from these components.

## Physical Desktop follow-up

The September 12 follow-up reused the installed Debian partition,
including the previously repaired font caches, Firefox ESR 140.15.0,
prepared DTB, Stage1, and volatile-home policy.
For the release tests, only a separate 5,895,368-byte release kernel and
content-addressed canary menu were staged using the existing RockOS tool.
The subsequent same-source dev comparison staged its own 15,483,248-byte
kernel and separate menu without replacing either installed generation.
No root-image transfer, package download, Docker rebuild, manual CRC sequence,
or firmware-environment change was needed.
The preflight read-only `e2fsck -fn /dev/mmcblk1p2` returned zero.

| Check | Observed result |
| --- | --- |
| Basic | Menu to shell 2.146 s; recovered to fresh firmware |
| Probe | Menu to completion 2.029 s; recovered to fresh firmware |
| Desktop, first boot | Console 10.587 s; visible window detected at 74.004 s |
| Desktop, second boot | Menu to readiness marker 69.906 s; recovered to fresh firmware |
| Same-source dev Desktop | Console 60.527 s; visible window detected at 282.190 s |
| First-boot content check | Local HTML, page JavaScript, DOM button handler, framebuffer capture; exit 0 |
| Same-boot browser restart, retained profile | Window detected 9.807 s after service start |
| Same-boot browser restart, fresh profile | Window detected 17.339 s after service start |

Desktop timing starts at host menu selection, excluding the preceding firmware
delay and human selection time.
The first observer used a ten-second polling delay plus query execution time.
Only its final host-clock observation is retained in the result JSON;
the serial log does not timestamp individual samples.
The dev comparison used that same observer and retained its host stdout.
Run order was release, release, dev, using the same Debian partition,
font caches, DTB, Stage1, boot arguments, and fresh volatile HOME on each boot.
Both kernels were built from `01b6e1eda`; their hashes are recorded above.
The approximately 3.8–4.0x ratio describes these sampled window observations,
not a randomized benchmark, a confidence interval, or an Internet-page speedup.
No electrical power cycle or storage-device cache flush was performed.
The second used the existing qualification runner's two-second poll delay,
plus the cost of three short serial queries per sample.
These are detection upper bounds, not exact first-paint timestamps.
The same-boot restart measurements use the guest monotonic clock and a
two-second polling delay; they are not fresh-boot measurements.
The fresh-profile test moved the original private runtime profile aside,
preserved its owner/mode in the new directory, and retained the warm system
page cache and per-user caches.
It therefore does not isolate disk I/O alone from all other cached state.

The content check attached to the existing loopback Marionette endpoint,
created a session, navigated to a small local HTML file under `/run`,
asserted the page's own JavaScript result, dispatched a DOM button click,
and asserted the changed result.
It then captured the live framebuffer through the existing fbdev helper.
The screenshot was transferred over paced serial output;
length and SHA-256 were verified before visual inspection.
The decoded PNG is 41,508 bytes, SHA-256
`8ee06cdcc3e088ba707c87589b2ca9eccc963a4ae2b53533d014b15cdb44fb74`.
The page-and-capture script completed in 6.990 s after attachment began;
this is not an exec-to-page startup measurement because it ran after the
first-window observation and host-side preparation.
A DOM click does not prove USB mouse/keyboard delivery or trusted X input.
The existing basic-only browser settings were retained for the offline test;
they must not be represented as normal Internet-mode acceptance.

Two warm standalone `glxtest` executions took 0.100 and 0.077 s,
both exiting one with the already configured
`MOZ_AVOID_OPENGL_ALTOGETHER` diagnostic.
Warm `firefox --version` took 0.224 and 0.249 s, both exiting zero.
Thus the graphics warning is reproducible without a long helper wait;
its Firefox-relative timestamp of 37.429 s does not establish that the helper
itself blocked for that duration.
The cold/warm gap supports investigating first-load work next,
but does not yet identify an individual ext2, MMC, VM, or scheduler defect.
No DMA coherency rules, SD clock limits, or browser sandbox settings were
relaxed to produce these results.

Recovery after the first boot and two controlled Firefox restarts succeeded
through one `sync; systemctl --force reboot` command.
The second complete Desktop cycle took 179.443 s including recovery,
versus 72.303 s from cycle entry to readiness;
the remaining approximately 107 s includes shutdown and firmware initialization.
Recovery passed its 120-second bound, but this is still a latency issue.
There was no second force flag, repeated reboot command, or physical reset.
The first custom observation record intentionally says `recovered: false`
because it left the live guest for content checks;
the separate `recovery-1.log` records its later successful recovery.
Do not silently convert that custom record into a promotion-gate result.

The later dev comparison did **not** complete the recovery gate.
Its explicit `SYNC_DONE` acknowledgement arrived 3.804 s after host command
transmission began (including paced transmission, not pure syscall time).
Fresh OpenSBI and U-Boot output followed, but the 120-second firmware wait
expired at `scanning bus usb1@50490000 for devices...`.
Later passive observation and one Ctrl-C produced no prompt;
SSH reported no route to the board.
No second reboot, forced power operation, or firmware write was attempted.
This is a failed bounded recovery check, not evidence of an Asterinas `sync`
hang or proof that the optimized candidate causes it.
Later SSH access confirmed RockOS `Linux 6.6.87` on `/dev/mmcblk1p3`.
No additional host-issued reboot or reset preceded that observation;
the exact late-recovery time and cause were not measured.
A fresh, unmounted read-only `e2fsck -fn /dev/mmcblk1p2` returned zero,
with 19,505 files and 231,205 used blocks.
The installed and vendor menu hashes and frozen kernel hash still matched
their preflight identities.
Late recovery does not turn the original 120-second gate into a pass.
See `recovery-dev-1.log`, `recovery-dev-host-1.log`, `final-ssh.log`, and
`final-root-check.log`.
The passive/Ctrl-C checks were observed through host tools;
the empty `late-firmware-observation.log` records no received output,
but does not itself record or prove the transmitted Ctrl-C.

Raw follow-up evidence is retained in `target/firefox-desktop-release/`:

- `candidate/manifest.json` and `stage/result.json`: canary identity/publication.
- `basic-1/`, `probe-1/`, `desktop-release-1/`, `desktop-release-2/`: boot observations.
- `dev-candidate/`, `stage-dev/`, `desktop-dev-1/`, `desktop-dev-1-host.log`: same-source physical dev comparison.
- `content-1.log`, `desktop-release-1.png`, `screenshot-transfer.log`: functional and visual evidence.
- `components-1.log`, `warm-restart-1.log`, `fresh-profile-1.log`: bounded component comparisons.
- `recovery-1.log`: first Desktop recovery after the content/restart experiments.

The release canary menu SHA-256 is
`4239d4381cf389d8a7bf0de9768a2e354e73ebed57a8c0a26898d1a18e9f2bdf`.
It does not replace the installed menu
`02280720efe7a1ad0ac084cdc20429406631e12d2e16f05638544bab0883fb26`.
The candidate source branch lacks separate network-integration changes present
in the frozen deployed kernel, so replacing the default kernel merely on the
basis of these offline results would risk discarding that work.

The unchanged menu/serial host test suites passed 73 tests in the persistent
container with `PYTHONPATH=.:tools/riscv`; see `unit-tests.log`.
Initial invocations used a nonexistent test-module name and then omitted
the required import path; those were harness import errors, not passing tests
or guest regressions.
An independent evidence review checked the physical figures and decoded
screenshot against the raw logs.
Its request to remove an unretained exact negative-poll timestamp was applied.

## Newly identified fault-around boundary defect

After the physical comparison, source inspection found a concrete unit mismatch
in `kernel/src/vm/vmar/vm_mapping.rs`, introduced by local batch-fault change
`0024b7c8dd`:

```rust
let end_idx = vmo.offset() + end_offset.div_ceil(PAGE_SIZE);
```

`MappedVmo::offset()` is a byte offset, whereas `commit_range_for_fault`
takes page indices.
For example, a mapping starting eight pages into a 128-page file with a
16-page fault window should request pages 8 through 23.
The expression instead produces end index `32768 + 16 = 32784`;
the backend clamps that to 128 and can populate pages 8 through 127.
Clamping to the file size does not restore the intended 16-page bound.
This is a static counterexample, not an executed regression or a measured
attribution of the remaining Firefox seconds.

The page-cache diagnostic flag also changes this path:
`Vmo::commit_range` falls back to committing only the first page when
`PAGECACHE_PROFILE` is enabled.
Consequently, a diagnostic boot using that flag can hide the over-read
mechanism and is not an equivalent performance workload.
The proposed next step is a bounded backend-read-count regression with a
nonzero mapping offset, followed by a minimal unit-conversion correction
while preserving the 16-page policy.
No production change for this newly discovered defect has been applied yet.
Implementation is awaiting the requested minimal-fix design confirmation;
the board is now back in RockOS for subsequent candidate validation.
