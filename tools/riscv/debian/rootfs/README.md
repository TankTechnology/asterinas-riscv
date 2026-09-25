# Debian RISC-V persistent-root gate

This workflow builds one signed Debian Trixie `riscv64` ext2 image and then
boots it twice on current Asterinas. The first boot writes and syncs a random
nonce; the second boot must read the same nonce from the same writable root
disk. The runtime is headless, has four harts, and uses `-nic none`.

Run all commands from the repository root. Build and use the dedicated rootfs
image described in `tools/docker/riscv-rootfs/README.md`; its default
explicit-QEMU/proot path does not modify host binfmt state.

## Proxy and container setup

### binfmt safety boundary

The supported default is `ASTERINAS_EXPLICIT_QEMU=1`: `proot` dispatches every
target-side exec through `qemu-riscv64-static`, including maintainer-script
children. It neither needs nor changes a host `binfmt_misc` registration.

**Do not enable or register a handler on the host** with `update-binfmts`,
`tonistiigi/binfmt`, or a write to `/proc/sys/fs/binfmt_misc/register`.
Docker's privileged mount can propagate the registration back to the host and
leave a persistent global interpreter. A pre-existing isolated binfmt boundary
may be used only as an explicit compatibility mode after read-only audit.

Check Clash without changing Docker, apt, Cargo, or Git configuration:

```bash
export ASTERINAS_PROXY=http://127.0.0.1:17892
curl --proxy "$ASTERINAS_PROXY" --fail --head \
  https://mirrors.tuna.tsinghua.edu.cn/debian/dists/trixie/InRelease
```

Pass the proxy only to this container invocation:

```bash
docker run --rm -it --network=host \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  -e http_proxy="$ASTERINAS_PROXY" -e https_proxy="$ASTERINAS_PROXY" \
  -e HTTP_PROXY="$ASTERINAS_PROXY" -e HTTPS_PROXY="$ASTERINAS_PROXY" \
  asterinas/asterinas:0.18.0-20260702-riscv-rootfs --check
```

Do not install dependencies interactively. If `--check` does not report
`execution=explicit-proot` and `host_binfmt=unchanged`, rebuild the pinned
Dockerfile instead of changing the running container or host.

## Build the frozen root once

The default HTTPS mirror is TUNA:

```bash
tools/riscv/debian/rootfs/build_rootfs.sh
```

If TUNA fails, retry explicitly with USTC, then official Debian. Do not persist
either URL in system apt configuration.

```bash
tools/riscv/debian/rootfs/build_rootfs.sh \
  --mirror https://mirrors.ustc.edu.cn/debian
tools/riscv/debian/rootfs/build_rootfs.sh \
  --mirror https://deb.debian.org/debian
```

The signed output is under `target/debian-riscv/rootfs/`. Reuse it while this
verification succeeds; do not rebuild merely to repeat a test:

```bash
python3 -m tools.riscv.debian.rootfs.contract verify \
  --image target/debian-riscv/rootfs/debian-root.ext2 \
  --manifest target/debian-riscv/rootfs/rootfs-manifest.json \
  --packages-lock target/debian-riscv/rootfs/packages.lock
```

### Build the frozen Firefox 143 RISC-V JIT root

The `browser-web` profile normally retains Debian's signed Firefox ESR base.
The complete WebAssembly-capable image is an explicit opt-in and never
downloads an unpinned browser during the rootfs build. Put the three packages
named by `firefox_jit_overlay.py` in one persistent directory; the installer
requires their exact filenames and SHA-256 identities before extracting them.

Reuse both that directory and the content-addressed Debian cache across builds:

```bash
tools/riscv/debian/rootfs/build_rootfs.sh \
  --profile browser-web \
  --output-dir target/debian-riscv/browser-web-jit/rootfs \
  --cache-dir target/debian-riscv/cache \
  --firefox-jit-package-dir target/debian-riscv/firefox-jit-packages
```

The resulting schema-seven manifest records the overlay marker digest as
`tool_versions.firefox-jit-overlay`. The build also runs the static RISC-V ELF,
NSS, CA, launcher, and online-root checks against the overlaid tree before it
publishes the ext2 image. Keep the package and Debian cache directories; do not
delete them between QEMU or physical-board experiments. Omitting
`--firefox-jit-package-dir` preserves the existing ESR-only build.

For a root already installed on Megrez partition 2, normal desktop startup is
the separate bounded workflow documented in
[the RISC-V operator guide](../../README.md#bounded-megrez-desktop-startup).
`make run_riscv_megrez_desktop` does not rebuild, transfer, mount, hash, or
rewrite the root image. Its frozen plan carries the expected root SHA-256 as an
identity assertion while Stage1 mounts the existing filesystem. If that
identity or the boot artifacts change, run the explicit one-time
`make prepare_riscv_megrez_desktop_boot` publication first.

## Fast browser-web development overlay

Do not rerun debootstrap or apt for changes limited to the browser-web guest
scripts and systemd units. Build the signed `browser-web` rootfs once, keep it
immutable, and materialize a copy-on-write development derivative:

```bash
make build_riscv_debian_browser_web_dev_overlay
```

The default input is
`target/debian-riscv/browser-web/rootfs/`; the disposable output is
`target/dev-overlays/browser-web/rootfs/`. The separate output tree remains
writable even when a privileged rootfs builder created `target/debian-riscv`.
Override both paths without changing
the repository or host configuration:

```bash
make build_riscv_debian_browser_web_dev_overlay \
  DEBIAN_BROWSER_WEB_BASE_ROOTFS=/absolute/path/to/frozen/rootfs \
  DEBIAN_BROWSER_WEB_DEV_ROOTFS=/absolute/path/to/development/rootfs
```

The command has no network or package-install phase.
It verifies the frozen
base manifest and package checksums, reflink-copies the ext2 image when the
filesystem supports it, and updates the regular files listed
in `browser_web_dev_overlay.json`.
Entries replace existing files by default.
An entry with the optional boolean `"create": true` may also add a regular file
under an existing directory;
this permits adding a runtime script to an older frozen base.
Every destination ancestor must already be a directory without symlink traversal.
The command reads every updated file back through `debugfs`.
A missing destination without the creation opt-in, symlinked source,
unsafe path, byte mismatch, mode mismatch, non-root ownership,
or nonzero timestamp fails closed
without replacing the previous development output.
Unexpected `debugfs` diagnostics also fail closed, regardless of exit status.
Images with the `metadata_csum` feature are rejected before editing,
because the overlay restores the superblock write time without recalculating checksums.

The output directory is a drop-in gate input containing
`debian-root.ext2`, `rootfs-manifest.json`, `packages.lock`, and
`source-metadata/`.
Point the existing QEMU gate variables at those files.
The additional `dev-overlay-manifest.json` records the frozen base image and
manifest hashes, overlay specification hash, and final derived image hash.
Each file records its source hash, mode, and effective `create` flag.
The compatibility rootfs manifest also records
the derivation digest as `tool_versions.asterinas-dev-overlay`; it must never
be confused with a newly signed package build.

Use this fast path only for listed scripts and service configuration. Run the
full `build_rootfs.sh --profile browser-web` workflow whenever package names or
versions, signed apt metadata, filesystem size/layout, users/groups, generated
caches, directories, symlinks, or device nodes change. QEMU run disks and
physical-board installation artifacts remain separate from both the frozen
base and this disposable derivative.

### Browser performance evidence and display-provider boundary

The browser-web guest defaults to `ASTERINAS_DISPLAY_PROVIDER=fbdev`. A full
rootfs build resolves its Xorg configuration from
`/etc/asterinas/display-providers/fbdev/xorg.conf.d`. For compatibility with
an older frozen browser-web image, the development overlay may fall back to
the existing `/etc/X11/xorg.conf.d`, but only for `fbdev`. A future DRM image
must install
`/etc/asterinas/display-providers/drm/xorg.conf.d/20-asterinas.conf` and set
`ASTERINAS_DISPLAY_PROVIDER=drm`; a missing provider-specific configuration
fails before Xorg starts. This boundary does not implement DRM or change a DRM
kernel path.

Each browser-web gate now collects `runtime-provenance.json` in the guest and
publishes `browser-performance-provenance.json` after binding it to the exact
host-side rootfs manifest SHA-256. The record identifies the display provider,
framebuffer width, height, stride and pixel depth, installed Xorg server and
fbdev driver versions, and whether Firefox uses the frozen RISC-V JIT overlay.
The interaction gate also reports bounded trusted-input-to-frame samples and
their nearest-rank p50/p95 summary. These records make fbdev and a future DRM
provider directly comparable without changing the Firefox workload.

The host-local network fixture now serves a separate
`/browser-quality/perf.html` timing page and `/browser-quality/perf-second.html`
navigation page. They leave the existing capability gate unchanged. The timing
page exposes bounded keyboard, pointer, and scroll samples from trusted events
and from a separately marked synthetic browser-only sequence. Each sample
distinguishes the first and next `requestAnimationFrame` callback. Those are
browser scheduling boundaries, not proof that Xorg copied pixels to `/dev/fb0`
or that the HDMI monitor scanned them out. The second page records Navigation
Timing's first byte, response end, DOM completion, and load completion within
the browser clock domain. `browser_latency_contract.py` validates samples and
calculates disjoint local waterfall intervals; it will reject missing or
reordered navigation rather than report an artificial zero.

Treat 100 ms p95 as the first admission target for physical interaction, not
as an assumed baseline. Gather multiple samples during one desktop boot; do
not reset the board or rewrite the root image between samples. The development
overlay includes all of these runtime collectors, so script-only measurement
changes do not rerun debootstrap, apt, or package downloads.

The Stage1 archive also carries a read-only process/system CPU sampler into
`/run/asterinas-tools/browser_system_time.py` after root handoff. Once Firefox
and Xorg are running, collect short intervals without restarting either process:

```bash
python3 /run/asterinas-tools/browser_system_time.py \
  --pid "$XORG_PID" --pid "$FIREFOX_PID" \
  --interval-seconds 1 --samples 20 \
  --output /run/browser-system-time.json
```

Use exact, independently validated PIDs from the graphical-readiness probe;
the executable name is not a stable Firefox identity and `pidof` may return
zero or multiple processes. The exclusive JSON artifact has mode `0600` and reports guest-monotonic
wall intervals, per-process user/kernel CPU ticks, global and per-core CPU
tick deltas, per-core busy fractions, context-switch deltas, and runnable counts.
When procfs supplies them, it also records per-process minor/major-fault deltas,
RSS after each interval, and system `MemAvailable` before and after the interval.
CPU time is *not* input latency or
HDMI scanout latency. This Asterinas image does not yet expose reliable
per-process I/O or physical scanout timestamps; the sampler marks them
unsupported instead of filling them with zeros. Thread mode obtains runnable
wait from the Linux-compatible `/proc/<pid>/task/<tid>/schedstat` interface.
Keep syscall profiling disabled for this baseline, since detailed logs perturb
timing.

#### Deterministic composite Firefox workload

Use the local composite workload when the lightweight interaction page is too
small to attribute a repeated cost. It keeps one Firefox session alive and runs
seven ordered phases: warm-up, DOM/layout interaction, canvas and image work,
bounded concurrent resources, local navigation and history, up to three local
browsing contexts, and cleanup. The resource phase performs a `no-store` pass,
then fills and reuses cache-eligible URLs. It never accepts an arbitrary target
URL from the page.

The reviewed modes are `smoke` (scale 1, at most 30 seconds), `profile` (scale
4, at most 120 seconds), and `stress` (scale 12, at most 300 seconds). `smoke`
is the QEMU/regression mode; use repeated `profile` runs for normal physical
attribution. `stress` is for bounded tail and stability investigation, not the
default benchmark.

Create a new empty evidence directory for every run, then invoke the Stage1
copy of the collector with the exact PIDs established by graphical readiness:

```bash
install -d -m 0700 /run/asterinas-browser-composite-smoke
python3 /run/asterinas-tools/browser_composite_capture.py \
  --firefox-pid "$FIREFOX_PID" --xorg-pid "$XORG_PID" \
  --mode smoke --timeout-seconds 30 \
  --fixture-index-url http://10.0.2.2:17894/browser-quality/index.html \
  --evidence-dir /run/asterinas-browser-composite-smoke
```

The capture writes private, exclusive phase/checkpoint, process, and thread
artifacts without deleting the Marionette session or restarting Firefox. The
process and thread samplers complete an initial snapshot before the workload
starts, stop with a final snapshot after the terminal phase, and must cover the
entire guest-monotonic workload observation window. Completed phase objects are
immutable, and successful phases must report the exact mode-specific operation,
request, context, and frame-sample counts. A checkpoint survives a later phase
failure. Browser phase/rAF timings use the
browser `performance.now()` clock; procfs CPU, faults, RSS and schedstat use the
guest monotonic sampling clock; human USB-to-HDMI latency is a third quantity
and is not inferred from either. An unavailable procfs counter is reported as
unsupported, never as zero. Public Baidu browsing remains an end-to-end
acceptance check and is deliberately excluded from bottleneck attribution.

After copying each physical run into its own host directory without renaming
the four canonical JSON files, bind the guest evidence to the exact fixture
request segments. Each nonce-bound run includes one cache-fill request per
warm resource and proves the repeated fetch from its browser-side count. The
verifier recomputes every file SHA-256, checks
the per-run fixture boundaries, requires stable Firefox/Xorg identities, and
revalidates sampler coverage:

```bash
python3 -m tools.riscv.browser_composite_manifest \
  --artifact-dir /path/to/evidence \
  --fixture-summary /path/to/evidence/physical-fixture.json \
  --run-dir /path/to/evidence/run-1 \
  --run-dir /path/to/evidence/run-2 \
  --run-dir /path/to/evidence/run-3 \
  --mode profile --physical \
  --output /path/to/evidence/manifest.json
python3 -m tools.riscv.browser_composite_manifest \
  --verify /path/to/evidence/manifest.json
```

The manifest is deterministic and deliberately contains no wall-clock field.
Its host-monotonic fixture ranges and guest-monotonic sampling ranges remain
separate clock domains; their file hashes and run ordering provide the binding.

Earlier fixed-duration physical Firefox samples on the kernel identified by SHA-256
`5444c9eb40e10d26278affb00f69bb8c212ce94091204cf94a6209899d2f588c`
measured Firefox for about 10 seconds each. Firefox consumed 9.59, 10.05,
and 10.10 CPU-seconds, including 3.50, 3.52, and 3.64 seconds in kernel mode.
Across all Firefox threads, `/proc/<pid>/task/<tid>/schedstat` reported only
326, 320, and 269 ms of runnable wait. The main thread's corresponding waits
were 171, 121, and 109 ms. Average system busy fractions were 35.3%, 37.5%,
and 37.4% across four CPUs, while the average runnable count stayed below two.
Those samples excluded the later phase-labelled composite workload. They did
not justify CPU-affinity tuning; use the bound composite manifest and its
phase/thread evidence before changing a kernel hot path.

The same runs recorded synthetic event-handler-to-first-rAF p95 values below
100 ms in two runs. A third run had 57 ms keyboard, 175 ms pointer, and 131 ms
scroll tails, consistent with intermittent execution spikes. The Firefox
Navigation Timing sample still rejects an out-of-bounds negative `fetchStart`;
retain and label the remaining timing fields rather than clamping that value.
The opt-in syscall profiler is also unsuitable for this baseline because its
periodic full serial snapshots prevent the desktop from reaching readiness.

Build the separate schema-v2 systemd profile only when the M1 artifact is not
the intended input. It has a distinct label, UUID, and output directory, so it
cannot alias the interactive root:

```bash
tools/riscv/debian/rootfs/build_rootfs.sh --profile systemd-m2
python3 -m tools.riscv.debian.rootfs.contract verify \
  --image target/debian-riscv/systemd-m2/rootfs/debian-root.ext2 \
  --manifest target/debian-riscv/systemd-m2/rootfs/rootfs-manifest.json \
  --packages-lock target/debian-riscv/systemd-m2/rootfs/packages.lock
```

The M2 profile installs Debian's packaged systemd as PID 1, requires
`systemd-logind` to reach its active state on the first cold boot, provides a
deterministic serial evidence service, and contains no `qemu-riscv64-static`
guest binary. The logind check is the first desktop-session foundation gate:
it verifies the service responsible for seats, sessions, and device ownership.
The second boot remains a persistence and normal-reboot check; it intentionally
does not duplicate the logind check. Neither marker claims that a display
server or desktop session has started.
Stage1 must receive the exact init argument `--root-init=systemd`; the gate
places it after the kernel command-line `--` separator so Asterinas forwards it
as init argv.

#### Firefox daily-use gate

`browser-daily-use-gate` is the bounded acceptance and diagnostic wrapper for
one already running Firefox and Xorg pair.
It uses the local browser-quality fixture only; it is not a public-web gate.
Obtain the two stable PIDs from graphical readiness, create a new private
directory, and run the Stage1-bound command:

```bash
install -d -m 0700 /run/asterinas-browser-daily-use-smoke
/run/asterinas-tools/browser-daily-use-gate \
  --firefox-pid "$FIREFOX_PID" \
  --xorg-pid "$XORG_PID" \
  --fixture-index-url http://10.0.2.2:17894/browser-quality/index.html \
  --evidence-dir /run/asterinas-browser-daily-use-smoke \
  --mode smoke
```

For a physical-board profile, use the fixture address reachable from that
board and record the hardware flag explicitly:

```bash
install -d -m 0700 /run/asterinas-browser-daily-use-profile
/run/asterinas-tools/browser-daily-use-gate \
  --firefox-pid "$FIREFOX_PID" \
  --xorg-pid "$XORG_PID" \
  --fixture-index-url http://10.100.19.216:17894/browser-quality/index.html \
  --evidence-dir /run/asterinas-browser-daily-use-profile \
  --mode profile \
  --physical
```

`--firefox-pid`, `--xorg-pid`, and `--evidence-dir` are parser-required.
Supply `--fixture-index-url` as shown for every operator invocation, rather
than relying on the fixture URL resolved from the guest environment.
With a persistent HOME, a prior passing run can leave the test fixture's fixed
download filename in `Downloads`. Before repeating the gate, it removes that
file only when its owner, regular-file type, size, and SHA-256 match the known
fixture. Unexpected content or a symlink fails closed and is retained.
`--mode` accepts only `smoke` and `profile`.
When it is omitted, the gate selects `smoke` without `--physical` and `profile`
with it; their default timeouts are respectively 30 and 120 seconds.
`--physical` records physical provenance for the relevant sampler only;
it does not prove display scanout or force `profile` when `--mode smoke` is
explicitly selected.
An optional `--timeout-seconds` must remain positive and no greater than
120 seconds.
For a separate attribution run, `--context-cpu-diagnostic` adds an `openCpu`
object to `browser-context-switch.json`. It brackets only
`WebDriver:NewWindow` with `/proc` snapshots for the Firefox parent and Xorg,
recording user/kernel CPU, faults, system CPU and context-switch deltas. It
also records user/kernel CPU and runqueue-wait deltas for the parent's threads
present in both snapshots, plus snapshot-read overhead and thread churn.
It does not include Firefox child processes or CPU consumed by threads that
start and finish wholly inside the interval. It does not turn wall time or
blocking waits into kernel CPU time. Run the normal gate again without this
flag as the latency control; the diagnostic run alone cannot establish a
speedup.

The terminal contract is exactly one verdict line:

```text
ASTERINAS_BROWSER_DAILY_USE_PASS functions=7/7 slow=<count> evidence_dir=<absolute-path>
ASTERINAS_BROWSER_DAILY_USE_FAIL reason=<canonical-reason>
```

The PASS line is written to standard output and the FAIL line to standard
error; a failure exits nonzero.
A successful run publishes six private JSON artifacts:
`browser-fixture-capture.json`, `browser-local-capture.json`,
`browser-context-switch.json`, `browser-composite-capture.json`,
`browser-system-time.json`, and `browser-thread-time.json`, followed by
`browser-daily-use-result.json`.
On a run failure, it makes a best-effort attempt to retract files it published
and to publish only `browser-daily-use-checkpoint.json`.
Retraction or checkpoint persistence can itself fail, so failure evidence is
invalid and must be discarded; use a new evidence directory for the next run.
All files are exclusive, no-follow, mode-0600 publications with an fsync.

Treat the evidence directory as single-use.
The gate rejects an existing artifact, checkpoint, private staging path, or
reservation, retains its `.browser-daily-use-<run-id>` staging directory, and
keeps `.browser-daily-use-reservation`; it never overwrites or resumes a run.
Use a fresh empty directory after either outcome.
The gate creates exactly one Marionette session and requires its initial
window-handle set to contain exactly the selected original window.
Pre-existing extra windows fail closed before any workload phase runs.
It passes a restricted session handle to workload phases and forbids phase
calls to `WebDriver:NewSession`,
`WebDriver:DeleteSession`, and `Marionette:Quit`.
It closes its transport but does not send `DeleteSession`; it also verifies
unchanged Firefox and Xorg PID/start-time identities, and closes only windows
that were not in the captured baseline (that is, gate-created windows).
It does not restart Firefox or Xorg, reboot the guest,
rewrite partition 2, or change the boot menu.
The exercised browser can still update its profile and leave the validated
download under `/home/asterinas/Downloads`; use the separate Stage1
`--volatile-home` handoff or a disposable image when those writes must not
persist.

The result has seven functional groups: `document`, `storage`, `execution`,
`rendering-media`, `navigation`, `download`, and `contexts`.
The performance qualification requires `document`, `storage`, `navigation`,
`download`, and `contexts` to pass.
`execution` and `rendering-media` are capability-coverage groups: each may be
`pass` or `unsupported`, but never `fail`, in a passing performance result.
`execution` owns the WebAssembly, Web Worker, and `fetch` fixture checks;
`rendering-media` owns canvas and audio; and `storage` continues to require
local storage, session storage, cookies, and IndexedDB.
An optional group is `unsupported` only after an exact terminal fixture report
contains a false owned check, and the result then includes
`fixture-capabilities-incomplete`.
That limitation is rejected when both optional groups pass and is required
when either is unsupported.
The standalone browser Web gate remains stricter and still requires every
fixture capability to be true.
Accordingly, `functions=7/7` means that all seven bounded group verdicts are
present; it does not turn an `unsupported` group into a functionality claim.
Its five performance categories are `startup`, `input`, `scroll`,
`navigation`, and `context-switch`.
Input keyboard/pointer and scroll first/next-rAF p95 values above 100 ms are
`slow`; navigation is `slow` only when browser response-to-DOM exceeds 2 s;
and context switching is `slow` when any open/select/return/close operation
or their total exceeds 500 ms.
`slow` is diagnostic evidence, not a functional failure, so a PASS may report
a nonzero slow count.
Startup is a guest-monotonic interval from the persisted `BOOT_FIREFOX_EXEC`
record through the persisted `BOOT_FIRST_WINDOW_READY` record for this Firefox
PID.
The timeline must contain exactly one strictly ordered, positive pair; it is
not substituted with the current gate session or a fresh browser-launch
measurement.

Do not subtract timestamps across the browser and guest clocks.
Input and scroll use browser `performance.now()`, startup/local-command/context
and sampler coverage use guest monotonic time, and browser Navigation Timing
is kept as its own domain.
A negative Navigation Timing `fetchStart` remains in evidence with
`fetchStartValid=false`; it is never clamped or rebased.
Physical HDMI scanout is unsupported, public-network browsing is excluded, and
synthetic browser input is not USB/Xorg/display latency.
Although the procfs system artifact can contain minor/major-fault deltas, the
daily-use result has no fault-based category or kernel-attribution claim;
`kernel-diagnostics-unavailable` remains an explicit placeholder limitation.

This command is a host-qualified interface only.
It does not itself establish a live QEMU or physical-board result, and it
makes no Firefox or kernel speedup claim.

## Build current-main boot artifacts

Build the current Sv39/SMP=4 kernel and deterministic stage-1 handoff archive:

```bash
make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
tools/riscv/debian/rootfs/build_stage1.sh \
  target/debian-riscv/stage1/initramfs.cpio
```

Prepare the pinned U-Boot build and an exact four-hart QEMU DTB. This bootstrap
disk is only a convenient producer of U-Boot and the DTB; the Debian gate
builds and validates its own three-file boot disk.

```bash
ASTERINAS_RISCV_BOOTI="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
ASTERINAS_INITRAMFS="$PWD/target/debian-riscv/stage1/initramfs.cpio" \
QEMU_UBOOT_PROFILE=generic-sv39-ltp-smp4 \
QEMU_UBOOT_OUT_DIR="$PWD/target/qemu-uboot/debian-root" \
QEMU_UBOOT_BUILD_DIR="$PWD/target/qemu-uboot/cache/u-boot-build" \
  tools/riscv/prepare_qemu_uboot_booti.sh prepare
```

## Test and run

The unit gate is local and does not launch QEMU or use the network:

```bash
make test_riscv_debian_rootfs_unit
```

## Short Firefox GDB probe

For startup diagnosis, use the bounded qemu-user probe before attempting the
full Asterinas web gate.  It requires an extracted, writable riscv64 rootfs
and invokes an explicit `qemu-riscv64-static` inside a bwrap rootfs namespace
with `-L /`; it never registers or changes a host `binfmt_misc` handler. This
is important because `-L` alone changes loader lookup, not guest absolute
paths:

```bash
tools/riscv/debian/rootfs/firefox_gdb_probe.sh \
  /path/to/extracted-rootfs \
  "$PWD/../backups/firefox-gdb-probe-$(date +%Y%m%d)" \
  45
```

The output directory contains the exact GDB command file, register and
backtrace evidence, QEMU stderr, and metadata including the binfmt read-only
check. `riscv64-linux-gnu-gdb` is required because the target is RISC-V; the
host-native `gdb` is not a substitute. The host also needs `bwrap` and an
explicit `qemu-riscv64-static` binary (the latter can be supplied via
`QEMU_RISCV64_STATIC`). This probe is intentionally limited
to loader entry and `__libc_start_main@plt`; use the Asterinas QEMU `-S -gdb`
workflow separately for kernel breakpoints.

The kernel-side reset probe is also reusable and does not boot the guest past
the first instruction:

```bash
tools/riscv/qemu_system_gdb_probe.sh \
  target/osdk/aster-kernel/aster-kernel-osdk-bin.Image \
  "$PWD/../backups/asterinas-system-gdb-$(date +%Y%m%d)" \
  15
```

The raw `.Image` is used as the QEMU payload.  To add kernel symbols, point
`ASTERINAS_KERNEL_SYMBOLS` at the matching unstripped ELF
(`target/osdk/aster-kernel/aster-kernel-osdk-bin`) before running the command.
The default remains a reset-only snapshot; set `ASTERINAS_KERNEL_CONTINUE=1`
only when deliberately testing a boot handoff toward `_start`.

For a live Firefox Web gate that is already known to reach the slow page
phase, enable the system-emulation stub explicitly on a loopback port.  The
normal gate does not contain a GDB argument, and this opt-in does not add
`-S`, so the VM continues booting before a debugger connects:

```bash
ASTERINAS_QEMU_GDB_PORT=23456 \
  python3 -m tools.riscv.debian.rootfs.browser_web_qemu_gate \
  --network-mode direct \
  ...the frozen browser-web gate arguments...

tools/riscv/qemu_live_pc_sampler.sh 23456 \
  "$PWD/../backups/firefox-live-pc-$(date +%Y%m%d)" 20 2
```

The sampler uses `riscv64-linux-gnu-gdb`, captures PC/RA/SP for every QEMU
hart, and detaches after each sample.  It stops early after three consecutive
connection failures, which normally means the gate has already terminated.
Use a matching unstripped kernel ELF to symbolize kernel PCs.  User PCs must
be interpreted with the sampled process's `/proc/<pid>/maps`; an address alone
does not distinguish a shared library from JIT/anonymous executable memory.
Neither command registers or modifies a host binfmt handler.

## Firefox Web acceptance semantics

The schema-seven `browser-web` gate separates deterministic browser
functionality from third-party anti-automation policy.  It submits the real
form in the host-served `/browser-quality/` fixture and requires the resulting
URL, DOM, CJK/Latin text, PNG resource timing, and screenshot.  It also
requires strict HTTPS/DOM/screenshots for the Baidu home page, Bilibili home
page, and a live BV detail link selected from that page.  A Baidu search is
recorded as either a validated result page or an exact
`wappass.baidu.com/static/captcha/` challenge whose back URL contains the
submitted query.  The latter is reported as `external-captcha`; it is
observable public-site evidence, not a successful search result, and the
Firefox/Baidu acceptance gate therefore fails closed.

The guest image precreates the ten JSON/PNG evidence inodes.  The gate
overwrites them, performs a whole-filesystem `sync`, uploads the final Baidu
PNG to the owned fixture, and only then emits the mode-qualified
`DEBIAN_FIREFOX_BAIDU_READY`. DNS and curl probes have explicit outer bounds,
and shell/Python timeline producers share the `/proc/uptime` monotonic clock.
The host validates the exact evidence set after QEMU stops, including PNG
structure, trust hashes, process identity, phase pairs, and ordered timeline
markers.

Firefox ESR currently does not enable the Linux sandbox by default for
RISC-V: Mozilla's `sandbox_default` supports Linux x86/x86_64/ARM/AArch64,
and Debian's riscv64 package does not override it with `--enable-sandbox`.
Consequently, a riscv64 content process with `Seccomp: 0` is recorded as
`unavailable-firefox-riscv64-build`, never as `sandbox=normal`.  The gate
still requires uid 1000, zero capabilities, `NoNewPrivileges=1`, stable
service identity, and absence of sandbox-disable arguments/environment.  A
future build that actually enables the sandbox must report `Seccomp: 2` and
is recorded as `enabled`.

The read-only toolchain helper provides a common preflight, evidence summary,
and hash manifest:

```bash
python3 -m tools.riscv.firefox_debug_tool preflight
BACKUP_DIR="$PWD/../backups/firefox-gdb-probe-$(date +%Y%m%d)"
python3 -m tools.riscv.firefox_debug_tool summarize \
  --gdb "$BACKUP_DIR/gdb-entry.txt" \
  --gdb "$BACKUP_DIR/gdb-libc-start.txt" \
  --syscall-log "$BACKUP_DIR/qemu-user-strace.log" \
  --output "$BACKUP_DIR/summary.json"
python3 -m tools.riscv.firefox_debug_tool manifest \
  "$BACKUP_DIR" \
  --output "$BACKUP_DIR/manifest.json"
```

For a bounded guest startup profile, append the opt-in kernel parameters
`asterinas.vm_profile=1 asterinas.vm_pagecache_profile=1
asterinas.futex_profile=1 asterinas.syscall_profile=1
asterinas.epoll_profile=1` before the `--`
separator in the gate boot arguments.  The syscall profiler records entry,
completion, and elapsed jiffies for `close`, `ppoll`, `futex`,
`clock_gettime`, `sched_yield`, `clone`, and `execve`.  Entry counts are logged
before the handler runs, so a blocked syscall remains visible even when the
guest reaches the hard timeout.  Keep the profiler disabled for normal gates;
it is diagnostic instrumentation only.  `asterinas.epoll_profile=1` adds
bounded counts of timeout classes and return classes for epoll waits; the
epoll-entry evidence hook is compiled disabled and is only useful in a
purpose-built diagnostic image.

Run the explicit two-boot gate as root in the development container:

```bash
make test_riscv_debian_rootfs_gate \
  DEBIAN_KERNEL="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  DEBIAN_UBOOT="$PWD/target/qemu-uboot/cache/u-boot-build/u-boot" \
  DEBIAN_DTB="$PWD/target/qemu-uboot/debian-root/qemu-virt.dtb" \
  DEBIAN_STAGE1_INITRAMFS="$PWD/target/debian-riscv/stage1/initramfs.cpio" \
  DEBIAN_ROOT_IMAGE="$PWD/target/debian-riscv/rootfs/debian-root.ext2" \
  DEBIAN_ROOT_MANIFEST="$PWD/target/debian-riscv/rootfs/rootfs-manifest.json" \
  DEBIAN_PACKAGES_LOCK="$PWD/target/debian-riscv/rootfs/packages.lock" \
  DEBIAN_PACKAGE_CHECKSUMS="$PWD/target/debian-riscv/rootfs/source-metadata/package-checksums" \
  DEBIAN_GATE_OUTPUT="$PWD/target/debian-riscv/gate"
```

For Firefox-only startup profiling, use the bounded one-boot sampler. It waits
for `BOOT_BASIC_TARGET`, X socket readiness, Firefox `exec`, and Marionette,
then exits without running the full web protocol. The sampler adds only
`asterinas.vm_profile=1` and keeps the final transcript in the root-owned
output directory:

```bash
python3 tools/riscv/debian/rootfs/firefox_startup_profile.py \
  --kernel /path/to/kernel.Image \
  --uboot /path/to/u-boot \
  --dtb /path/to/qemu-virt.dtb \
  --stage1-initramfs /path/to/initramfs.cpio \
  --root-image /path/to/debian-root.ext2 \
  --root-manifest /path/to/rootfs-manifest.json \
  --packages-lock /path/to/packages.lock \
  --package-checksums /path/to/package-checksums \
  --output-directory /path/to/root-owned-profile \
  --boot-timeout 360
```

For the online desktop shell, run the short graphical QEMU capture with the
same frozen inputs. It waits for X, PCManFM, LXPanel, and Firefox's first
window; then captures Firefox open, minimized, restored, the Files and
Terminal launcher clicks, and a taskbar switch back to Firefox. The six PPMs
and serial log are retained in a private output directory for visual review.
A successful process or a changed
frame alone does not establish that the intended application window appeared;
inspect the captures before recording desktop acceptance.

```bash
python3 tools/riscv/debian/rootfs/browser_web_desktop_shell_qemu_gate.py \
  --kernel /path/to/kernel.Image \
  --uboot /path/to/u-boot \
  --dtb /path/to/qemu-virt.dtb \
  --stage1-initramfs /path/to/initramfs.cpio \
  --root-image /path/to/debian-root.ext2 \
  --root-manifest /path/to/rootfs-manifest.json \
  --packages-lock /path/to/packages.lock \
  --package-checksums /path/to/package-checksums \
  --output-directory /path/to/private-desktop-capture \
  --boot-timeout 360
```

追加 `--firefox-process-diagnostic` 可在 Firefox exec 后启用有界的 `ps` 和
`/proc` 快照；它会增加少量串口扰动，只用于定位主进程/子进程状态。若要把
高频 epoll 调用归因到具体 caller/fd，可再追加
`--epoll-entry-diagnostic`；它启用 `asterinas.epoll_profile=1` 和
`asterinas.epoll_entry_profile=1`，同样只用于短 probe。guest procfs 的
`wchan` 等文件可能阻塞，因此进程快照不应作为唯一阻塞点证据。
追加 `--timerfd-diagnostic` 可统计 timerfd 的 set/expire/read/EAGAIN 聚合，
用于验证 epoll 伪就绪；追加 `--syscall-diagnostic` 可记录常见 syscall 的
进入/完成次数、累计 jiffies 及 clone/exec 边界。两者都只影响诊断镜像的
bootargs，默认关闭，不改变正常启动语义。

`debug-root-console` 验收是一个独立的低噪声串口 profile。QEMU 与 Megrez
都使用恰好一个 `loglevel=off`，防止异步内核日志在字节层打断固定命令的
nonce 协议。需要分析内核日志时应使用单独的诊断启动，不要扩大 root console
分类器的接受范围。

For the systemd M2 profile, use the M2 root and Stage1 archive. This gate keeps
one QEMU process alive across the guest's normal reboot, interrupts the second
U-Boot autoboot, and launches Asterinas a second time without `saveenv`:

```bash
make test_riscv_debian_systemd_m2_gate \
  DEBIAN_KERNEL="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  DEBIAN_UBOOT="$PWD/target/qemu-uboot/cache/u-boot-build/u-boot" \
  DEBIAN_DTB="$PWD/target/qemu-uboot/debian-root/qemu-virt.dtb" \
  DEBIAN_STAGE1_INITRAMFS="$PWD/target/debian-riscv/systemd-m2/stage1/initramfs.cpio" \
  DEBIAN_ROOT_IMAGE="$PWD/target/debian-riscv/systemd-m2/rootfs/debian-root.ext2" \
  DEBIAN_ROOT_MANIFEST="$PWD/target/debian-riscv/systemd-m2/rootfs/rootfs-manifest.json" \
  DEBIAN_PACKAGES_LOCK="$PWD/target/debian-riscv/systemd-m2/rootfs/packages.lock" \
  DEBIAN_PACKAGE_CHECKSUMS="$PWD/target/debian-riscv/systemd-m2/rootfs/source-metadata/package-checksums" \
  DEBIAN_SYSTEMD_M2_GATE_OUTPUT="$PWD/target/debian-riscv/systemd-m2/gate"
```

The target requires every frozen artifact and never invokes debootstrap, a
mirror, or `build_rootfs.sh`. Network is permitted only while constructing the
signed base root. The M1 and M2 gates are always launched with `-nic none`;
the explicit M5 gate below is the only slirp-enabled exception.

### QEMU DRM desktop gate

The additive `desktop-drm` profile exercises the same Debian desktop session
on Asterinas' virtio-gpu DRM device. It installs Mesa/libdrm, selects Xorg's
`modesetting` driver with DRI3/glamor, and replaces the legacy bochs display
with `virtio-gpu-device`; the existing M3/M4/M5 profiles remain the fbdev
fallback path.

```bash
tools/riscv/debian/rootfs/build_rootfs.sh --profile desktop-drm
PYTHONPATH="$PWD" python3 -m tools.riscv.debian.rootfs.desktop_drm_gate \
  --kernel "$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  --uboot "$PWD/target/qemu-uboot/cache/u-boot-build/u-boot" \
  --dtb "$PWD/target/qemu-uboot/debian-root/qemu-virt.dtb" \
  --stage1-initramfs "$PWD/target/debian-riscv/desktop-drm/stage1/initramfs.cpio" \
  --root-image "$PWD/target/debian-riscv/desktop-drm/rootfs/debian-root.ext2" \
  --root-manifest "$PWD/target/debian-riscv/desktop-drm/rootfs/rootfs-manifest.json" \
  --packages-lock "$PWD/target/debian-riscv/desktop-drm/rootfs/packages.lock" \
  --package-checksums "$PWD/target/debian-riscv/desktop-drm/rootfs/source-metadata/package-checksums" \
  --output-directory "$PWD/target/debian-riscv/desktop-drm/qemu-gate"
```

The gate requires ordered DRM/Xorg/device evidence and a non-blank monitor
screendump. It is deliberately separate from the network/browser gates so a
display regression is diagnosable without conflating DNS or Firefox startup.

### QEMU M5 HTTPS desktop gate

Build the `desktop-m5-network` root when the external-network desktop is the
intended input. This profile adds the `curl` command as a frozen package
identity. The QEMU gate uses slirp only for the guest data path; it does not
inherit host proxy variables, TAP configuration, or a Linux guest kernel.

```bash
tools/riscv/debian/rootfs/build_rootfs.sh --profile desktop-m5-network
make test_riscv_debian_desktop_m5_qemu_gate \
  DEBIAN_KERNEL="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  DEBIAN_UBOOT="$PWD/target/qemu-uboot/cache/u-boot-build/u-boot" \
  DEBIAN_DTB="$PWD/target/qemu-uboot/debian-root/qemu-virt.dtb" \
  DEBIAN_STAGE1_INITRAMFS="$PWD/target/debian-riscv/desktop-m5-network/stage1/initramfs.cpio" \
  DEBIAN_ROOT_IMAGE="$PWD/target/debian-riscv/desktop-m5-network/rootfs/debian-root.ext2" \
  DEBIAN_ROOT_MANIFEST="$PWD/target/debian-riscv/desktop-m5-network/rootfs/rootfs-manifest.json" \
  DEBIAN_PACKAGES_LOCK="$PWD/target/debian-riscv/desktop-m5-network/rootfs/packages.lock" \
  DEBIAN_PACKAGE_CHECKSUMS="$PWD/target/debian-riscv/desktop-m5-network/rootfs/source-metadata/package-checksums" \
  DEBIAN_DESKTOP_M5_QEMU_GATE_OUTPUT="$PWD/target/debian-riscv/desktop-m5-network/qemu-gate"
```

The gate requires ordered DNS and HTTPS evidence for `www.baidu.com`, the
exact slirp local address `10.0.2.15`, all M4 application-window milestones,
and a non-blank framebuffer capture. ICMP ping sockets and `ip` route dumps
are not part of this browser gate because those Linux interfaces remain
separate compatibility work; UDP DNS and TCP/TLS are tested directly.

NetSurf can render ordinary raster and SVG page assets supplied by its Debian
dependencies. Its JavaScript engine is intentionally not a modern Chromium
compatibility claim: JavaScript must be reported by a separate local DOM smoke
test and is not allowed to turn a successful DNS/HTTPS gate into a failure.
The current non-blank framebuffer check proves the desktop, not that the
foreground window has finished rendering Baidu.

### QEMU M6 browser evidence gate

> This is a historical NetSurf milestone gate. Its `limited-pass`, `disabled`,
> and `failed` JavaScript classifications must not be reported as Firefox-ready
> or as modern-browser compatibility. Use the schema-seven Firefox Web gate for
> current Firefox acceptance.

After rebuilding the `desktop-m5-network` root, use the M6 gate to foreground
and capture a Baidu-hosted PNG in NetSurf before navigating the same window to
a fixed local JavaScript fixture. The direct image isolates HTTPS transfer and
image decoding from the modern Baidu homepage's script workload:

```bash
make test_riscv_debian_desktop_m6_browser_gate \
  DEBIAN_KERNEL="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  DEBIAN_UBOOT="$PWD/target/qemu-uboot/cache/u-boot-build/u-boot" \
  DEBIAN_DTB="$PWD/target/qemu-uboot/debian-root/qemu-virt.dtb" \
  DEBIAN_STAGE1_INITRAMFS="$PWD/target/debian-riscv/desktop-m5-network/stage1/initramfs.cpio" \
  DEBIAN_ROOT_IMAGE="$PWD/target/debian-riscv/desktop-m5-network/rootfs/debian-root.ext2" \
  DEBIAN_ROOT_MANIFEST="$PWD/target/debian-riscv/desktop-m5-network/rootfs/rootfs-manifest.json" \
  DEBIAN_PACKAGES_LOCK="$PWD/target/debian-riscv/desktop-m5-network/rootfs/packages.lock" \
  DEBIAN_PACKAGE_CHECKSUMS="$PWD/target/debian-riscv/desktop-m5-network/rootfs/source-metadata/package-checksums" \
  DEBIAN_DESKTOP_M6_BROWSER_GATE_OUTPUT="$PWD/target/debian-riscv/desktop-m5-network/m6-qemu-gate"
```

`desktop-m6-browser.ppm` records the foreground Baidu logo image and
`desktop-m6-javascript.ppm` records the local fixture after classification.
`result.json` reports `limited-pass`, `disabled`, or `failed`. A
`limited-pass` proves only that the packaged NetSurf engine executed a local
script-only navigation from the pending fixture to a fixed pass document; it
is not a claim of Chromium-compatible JavaScript. The changing remote pixels
are inspected as visual evidence and are not compared to a fixed hash.
Rendering the full modern Baidu homepage is a later compatibility target, not
a prerequisite for this foundational gate.

### QEMU M7 real Baidu page evidence

The M7 gate reuses the M6 network, desktop, remote-image, and local-JavaScript
evidence before starting a fresh NetSurf process with JavaScript disabled. It
loads Baidu's official mobile page because the HTTPS desktop endpoint falls
back to an approximately 700-KiB legacy document that NetSurf 3.11 does not
finish laying out. The mobile document is approximately 80 KiB and retains
the real Baidu logo, search form, remote stylesheets, and image requests.

```bash
make test_riscv_debian_desktop_m7_baidu_gate \
  DEBIAN_KERNEL="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  DEBIAN_UBOOT="$PWD/target/qemu-uboot/cache/u-boot-build/u-boot" \
  DEBIAN_DTB="$PWD/target/qemu-uboot/debian-root/qemu-virt.dtb" \
  DEBIAN_STAGE1_INITRAMFS="$PWD/target/debian-riscv/desktop-m5-network/stage1/initramfs.cpio" \
  DEBIAN_ROOT_IMAGE="$PWD/target/debian-riscv/desktop-m7-baidu/rootfs/debian-root.ext2" \
  DEBIAN_ROOT_MANIFEST="$PWD/target/debian-riscv/desktop-m7-baidu/rootfs/rootfs-manifest.json" \
  DEBIAN_PACKAGES_LOCK="$PWD/target/debian-riscv/desktop-m7-baidu/rootfs/packages.lock" \
  DEBIAN_PACKAGE_CHECKSUMS="$PWD/target/debian-riscv/desktop-m7-baidu/rootfs/source-metadata/package-checksums" \
  DEBIAN_DESKTOP_M7_BAIDU_GATE_OUTPUT="$PWD/target/debian-riscv/desktop-m7-baidu/m7-qemu"
```

`desktop-m7-baidu-home.ppm` is published only after the title identifies a
real page from `https://m.baidu.com/`. A search-result frame and a passing
`result.json` additionally require the submitted query to return a result
title. If Baidu sends its `wappass.baidu.com` security challenge, the gate
publishes `desktop-m7-baidu-failure.ppm` and remains failed. The challenge is
evidence that DNS, HTTPS, navigation, and rendering reached Baidu, but it is
not accepted as search-result evidence.

The 2026-08-28 generic-Sv39/SMP=4 run reached the homepage milestone and
captured a visible Baidu logo and search box. The subsequent `asterinas`
query rendered `百度安全验证`, so the final M7 result remained failed with
`search-title-timeout`. The current image also lacks a CJK font, leaving some
Chinese text as missing-glyph boxes. These are user-space/browser and remote
service limitations; the run did not expose a new Asterinas DNS, TCP, TLS,
VirtIO input, Xorg, or framebuffer failure.

### QEMU M9 desktop software smoke gate

The M9 profile is a separate signed rootfs derived from the M5 desktop
package set. It uses Debian's official `riscv64` `netsurf-gtk`, adds `vim` and
`ffmpeg`, and does not list `ffprobe` separately: Debian ships `/usr/bin/ffprobe`
from the `ffmpeg` package. The manifest deliberately keeps schema version 5
for compatibility with existing readers while binding the new profile name,
label, UUID, and package identity.

Build and verify the image once, then reuse it for both QEMU and the bounded
Megrez run:

```bash
tools/riscv/debian/rootfs/build_rootfs.sh --profile desktop-m9-software
python3 -m tools.riscv.debian.rootfs.contract verify \
  --image target/debian-riscv/desktop-m9-software/rootfs/debian-root.ext2 \
  --manifest target/debian-riscv/desktop-m9-software/rootfs/rootfs-manifest.json \
  --packages-lock target/debian-riscv/desktop-m9-software/rootfs/packages.lock
```

Run the complete four-hart QEMU contract with finite timeouts. This single
gate reuses the M5 slirp/DNS/HTTPS, M4 desktop, and M7 NetSurf/Baidu evidence,
then executes the M9 software service. The M8 quality gate remains an
independent optional run because its title assertion is intentionally not a
prerequisite for application smoke evidence:

```bash
make test_riscv_debian_desktop_m9_software_gate \
  DEBIAN_KERNEL="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  DEBIAN_UBOOT="$PWD/target/qemu-uboot/cache/u-boot-build/u-boot" \
  DEBIAN_DTB="$PWD/target/qemu-uboot/debian-root/qemu-virt.dtb" \
  DEBIAN_STAGE1_INITRAMFS="$PWD/target/debian-riscv/stage1/initramfs.cpio" \
  DEBIAN_ROOT_IMAGE="$PWD/target/debian-riscv/desktop-m9-software/rootfs/debian-root.ext2" \
  DEBIAN_ROOT_MANIFEST="$PWD/target/debian-riscv/desktop-m9-software/rootfs/rootfs-manifest.json" \
  DEBIAN_PACKAGES_LOCK="$PWD/target/debian-riscv/desktop-m9-software/rootfs/packages.lock" \
  DEBIAN_PACKAGE_CHECKSUMS="$PWD/target/debian-riscv/desktop-m9-software/rootfs/source-metadata/package-checksums" \
  DEBIAN_DESKTOP_M9_SOFTWARE_GATE_OUTPUT="$PWD/target/debian-riscv/desktop-m9-software/m9-qemu-gate" \
  DEBIAN_DESKTOP_BOOT_TIMEOUT=900
```

The software service is intentionally small and fail-closed. It saves a
marker line with non-interactive Vim, generates a deterministic 16x16 RGB
frame with single-threaded FFmpeg on the ext2-backed `/var/tmp`, and checks
its dimensions with ffprobe. Each command has a 120-second timeout and the
service has a 300-second systemd budget, so a missing package or a hung
process produces `DEBIAN_DESKTOP_M9_FAIL reason=...` instead of leaving a
QEMU or board session running indefinitely. The QEMU result retains the
serial transcript, screenshot, network fixture summary, and immutable
manifest identity.

For an unreliable Chinese-network path, first use the proxy preflight above;
if TUNA cannot provide a complete signed closure, retry the build explicitly
with USTC and then `deb.debian.org`. Do not change the mirror in the guest or
run an unbounded `apt` command on Megrez. The verified `.deb` closure and its
`package-checksums` are the offline installation input for the physical test.

### Megrez static-RJ45 browser gate

The physical browser milestone reuses the signed `desktop-m5-network` root
and the kernel's reviewed static profile. The exact guest identity is:

```text
asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1
interface=eth0
primary DNS=10.2.0.5
fallback DNS=10.2.0.6
```

Prepare the host with the canonical
[Megrez debugging tool list](../../README.md#host-side-megrez-debugging).
Keep QEMU and cross-build dependencies in the pinned container;
the host tools are only for serial, link, packet, throughput, and screenshot observations.

Install a newly built ext2 image with
`tools.riscv.debian.rootfs.megrez_installer`; Asterinas must write and read
back eMMC partition 2. Linux may stage immutable boot files but is not an
accepted runtime or installer kernel. On the currently verified firmware,
U-Boot exposes only `ethernet@50400000`, while the live RJ45 path selected by
Asterinas is the other GMAC. Its TFTP path therefore cannot be the default
recovery transport. Stage the current Image, frozen Megrez DTB, and Stage1
under their basenames on eMMC partition 1, verify the copied hashes, unmount
the partition, and run the bounded gate with the read-only MMC loader:

```bash
crc32_file() {
  python3 -c 'import pathlib,sys,zlib; print(f"{zlib.crc32(pathlib.Path(sys.argv[1]).read_bytes()):08x}")' "$1"
}
BOOTI_CRC32="$(crc32_file target/megrez-browser-network/tftp/asterinas-browser-net.booti)"
STAGE1_CRC32="$(crc32_file target/megrez-browser-network/tftp/debian-browser-stage1.cpio)"
python3 -m tools.riscv.megrez_gmac_gate /dev/ttyUSB0 \
  --booti asterinas-browser-net.booti \
  --dtb eic7700-milkv-megrez.dtb \
  --initrd debian-browser-stage1.cpio \
  --expected-crc32 "booti=$BOOTI_CRC32,dtb=4afcb20e,initrd=$STAGE1_CRC32" \
  --host-interface enp12s0 --load-transport mmc \
  --reboot-after 420 \
  --output-directory target/megrez-browser-network/gate \
  --boot-timeout 360 --drain-timeout 5
```

The strict serial order is selected GMAC, physical M5 link/DNS/HTTPS/PNG
evidence, M5 READY, M4 desktop READY, M6 remote image, one JavaScript status,
and matching M6 READY. `limited-pass`, `disabled`, and `failed` describe only
the packaged NetSurf JavaScript engine; none claims Chromium compatibility.
The gate drains the full serial transcript before publishing `passed: true`.
It deliberately tests UDP DNS and TCP/TLS rather than ICMP: the current
Asterinas network path does not provide the Linux ping-socket contract, and a
ping result would not prove that browser traffic works.

On a physical browser run, one exact input-capability degradation is collected
rather than treated as a reason to release the serial port early. If M4 emits
`DEBIAN_DESKTOP_M4_DIAGNOSTIC missing=pointer-device` followed by
`DEBIAN_DESKTOP_M4_FAIL reason=desktop-timeout`, the collector continues to
require every M6/M7 marker and the fresh automatic U-Boot recovery. A complete
Baidu homepage and search sequence is then published as `passed: false` with
`guest-failure-recovered:browser-pass-input-missing:pointer-device`; it proves
the browser path but deliberately does not claim mouse usability. Missing,
reordered, duplicated, or differently attributed M4 failure evidence remains
a hard failure.

For a desktop plan with `asterinas.reboot_after=600`, invoke
`tools.riscv.megrez_debug board` with `--timeout 900`. The timeout is measured
by the host, while the recovery timer is measured by Asterinas; the guest clock
can advance more slowly on Megrez. Shorter host budgets can therefore publish
`recovery-not-observed` after the browser evidence even though the board later
returns to U-Boot automatically.

This is a bounded useful-network contract, not a general Linux network stack
milestone. DHCP, `RTM_NEWADDR`, `RTM_NEWROUTE`, NetworkManager, cable-replug
recovery, live GMAC failover, USB Ethernet, Wi-Fi, Firefox, and modern
JavaScript remain outside this gate.

## Evidence and cleanup

Inspect the immutable provenance and the current-run evidence:

```bash
python3 -m json.tool target/debian-riscv/rootfs/rootfs-manifest.json
sed -n '1,20p' target/debian-riscv/rootfs/packages.lock
sed -n '1,200p' target/debian-riscv/gate/boot1.serial.log
sed -n '1,200p' target/debian-riscv/gate/boot2.serial.log
python3 -m json.tool target/debian-riscv/gate/result.json
python3 -c 'import json; print(json.load(open("target/debian-riscv/gate/result.json"))["final_root_sha256"])'
```

`debian-root.ext2` is immutable. Only the run-private
`debian-root.run.ext2` is writable. A successful run retains both logs, the
writable root, boot disk, and `result.json`. A failed run retains the complete
available logs and a failing result, never a stale `passed: true`, and never
mutates the base image.

## Verified M1 evidence (2026-08-24)

The documented Make target passed on source commit `d50b17aef` in container
image `sha256:4f054ba7e4d35567cd1b974506ecc6ae4a9e35e52616ca048cf302f8dfca8b23`
with QEMU 10.2.1. The runtime container used `--network=none`.

Frozen inputs:

| Artifact | SHA-256 |
| --- | --- |
| Asterinas Sv39/SMP=4 kernel | `9b0b352bc5f3fb38c7d4ee67f3abc40785f3a26861032944a67db8a38da63b60` |
| stage-1 initramfs | `aef46a338a158dbb9fbe4ed220167eb95c6a392c9f17de15d3877230eb740b08` |
| four-hart QEMU DTB | `3886fd4e5e7f47e3ba1536b3a374f89d4d06cf42f9c3bb5c9038e418ebf9dec9` |
| U-Boot | `cd1f164d4d6c3493bdceec168d2d066aaa218fe516ea9cd8cbc049427f9b55bc` |
| immutable Debian root | `060f613281f2e77fa2232f31322213a310f48b5b18df2991ade9eb2fca7bebae` |
| rootfs manifest | `6f246da49af0759184b47867047bec2e73d0be7228f08d0f723ce248412a14ae` |
| package lock | `fd817c8db7bd71098113b8c2ed4f52c3d70542efaaf97ced2b81637c5528dfff` |
| signed TUNA Trixie `InRelease` | `98b25b5cd185c59d34aa6e4c3e9b5b8f01bbe9d104fe2dcfbcd30dc0a14a59ed` |

The manifest records Trixie `13.6`, `riscv64`, mirror
`https://mirrors.tuna.tsinghua.edu.cn/debian`, and build timestamp
`2024-01-01T00:00:00Z`. The checked identity packages were
`base-files=13.8+deb13u6`, `libc6=2.41-12+deb13u3`,
`bash=5.2.37-2+b9`, `coreutils=9.7-3`, and `util-linux=2.41-5`.

The final result reported `passed: true`, reason `pass`, two QEMU argv vectors,
boot-one duration 14.042 seconds, boot-two duration 13.940 seconds, and final
writable-root SHA-256
`f6300db673c17c038a8bdbca76f092e891d0874b6d74737e3bb766bbe7492262`.
Both logs contain `__DEBIAN_ROOTFS_SHELL_READY__`, zero command statuses, and
the second-boot probe; nonce plaintext is replaced by `<nonce-redacted>`.

This evidence proves the generic QEMU Sv39/SMP=4 two-boot persistence
contract. It does not claim physical Megrez operation, guest networking,
systemd boot, display, USB, or desktop support.

## Megrez persistent Debian shell

Build two distinct current kernels. The generic QEMU artifact requires
`FEATURES=riscv_sv39_mode`; the Megrez artifact is a separate default Sv48
build. The frozen plan rejects swapping them and also records the exact
signed root, Stage1, U-Boot, and four-hart DTBs.

The board sequence is ` inventory ` before ` install-if-needed `. Inventory is
read-only, and a matching result skips installation. Only a measured image
hash mismatch may enter the Asterinas-only installer, which may write only
`/dev/mmcblk0p2`. The operator must not boot Linux to install or validate this
root. Do not arm the short EIC7700X watchdog while hashing the full device or
installing; use the bounded Asterinas timer and require a fresh U-Boot recovery
epoch. The `gate` command performs two bounded boots, and `handoff` is refused
unless their physical result passes.

Inventory, `gate`, and `handoff` transfer the compressed current kernel and
Stage1 over serial YMODEM. They load `eic7700-milkv-megrez.dtb` read-only from
eMMC partition 1 and reject a CRC mismatch. The current U-Boot GMAC probes
`0x50400000`, which is not the RJ45 path selected by Asterinas; consequently
these boot paths do not depend on TFTP. Only the Asterinas network installer
uses the verified board RJ45 path after the kernel has started.

```bash
make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
cp target/osdk/aster-kernel/aster-kernel-osdk-bin.Image /absolute/run/qemu-sv39.booti
make kernel TARGET_ARCH=riscv64 SMP=4
cp target/osdk/aster-kernel/aster-kernel-osdk-bin.Image /absolute/run/megrez-sv48.booti

RUN="$PWD/target/megrez-debian-shell/$(git rev-parse --short=12 HEAD)"
python3 -m tools.riscv.megrez_debian_shell check "$RUN/plan.json"
sudo -E python3 -m tools.riscv.megrez_debian_shell qemu \
  "$RUN/plan.json" --output "$RUN/qemu"
python3 -m tools.riscv.megrez_debian_shell permit \
  "$RUN/plan.json" --qemu-evidence "$RUN/qemu/qemu-evidence.json" \
  --output "$RUN/permit.json"
sudo -E python3 -m tools.riscv.megrez_debian_shell inventory \
  "$RUN/plan.json" /dev/ttyUSB0 --permit "$RUN/permit.json" \
  --output "$RUN/inventory-before" --yes
sudo -E python3 -m tools.riscv.megrez_debian_shell install-if-needed \
  "$RUN/plan.json" /dev/ttyUSB0 --permit "$RUN/permit.json" \
  --inventory "$RUN/inventory-before/result.json" --output "$RUN/install" --yes
if jq -e '.status == "needs-install"' "$RUN/inventory-before/result.json"; then
  sudo -E python3 -m tools.riscv.megrez_debian_shell inventory \
    "$RUN/plan.json" /dev/ttyUSB0 --permit "$RUN/permit.json" \
    --prior-inventory "$RUN/inventory-before/result.json" \
    --install-result "$RUN/install/result.json" \
    --output "$RUN/inventory-after" --yes
  cp "$RUN/inventory-after/result.json" "$RUN/inventory-current.json"
else
  cp "$RUN/inventory-before/result.json" "$RUN/inventory-current.json"
fi
sudo -E python3 -m tools.riscv.megrez_debian_shell gate \
  "$RUN/plan.json" /dev/ttyUSB0 --permit "$RUN/permit.json" \
  --inventory "$RUN/inventory-current.json" --output "$RUN/physical" \
  --host-interface enp12s0 --yes
sudo -E python3 -m tools.riscv.megrez_debian_shell handoff \
  "$RUN/plan.json" /dev/ttyUSB0 --result "$RUN/physical/result.json" \
  --host-interface enp12s0 --yes
picocom --baud 115200 --flow n --parity n --databits 8 /dev/ttyUSB0
```

This stage proves an interactive persistent shell only. The next scope is
systemd, network, and desktop; none is claimed by this milestone.
