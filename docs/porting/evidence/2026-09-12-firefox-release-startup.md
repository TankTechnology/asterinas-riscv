# Firefox startup: optimized kernel candidate

## Outcome and limits

The existing `release` build removes substantial overhead from the RISC-V
kernel's startup paths. No new scheduler, page-cache algorithm, browser flag,
or recurring boot script was needed. The candidate is built from
`01b6e1eda` in `codex/megrez-boot-main`, with the same Sv39 feature and SMP4
test configuration as the unoptimized comparison kernel.

This is a **QEMU component result**, not a measured physical Firefox-window
speedup. The board remains in RockOS; neither its qualified kernel nor its
menu was replaced. Complete Desktop qualification remains required before
promotion. In particular, the separate headless screenshot experiment did
not produce a screenshot and must not be reported as passing Firefox.

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
