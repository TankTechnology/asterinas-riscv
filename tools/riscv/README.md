# RISC-V firmware boot validation

> **Current product path:** the Megrez Debian `browser-web` image documented in
> [`docs/porting/board-image-current.md`](../../docs/porting/board-image-current.md).
> This page lists the current QEMU/board commands. Historical milestone
> material lives in [`archive/README.md`](archive/README.md). For the host-test baseline and cleanup
> decisions, see [`MAINTAINABILITY.md`](MAINTAINABILITY.md).

This directory validates the firmware-to-kernel handoff without modeling a single development board in QEMU.
The same runner describes a machine contract, a boot flow, and the observable milestones expected from the guest.
The registered profiles currently cover QEMU `virt`, SiFive U, Sv39, and the Sv48/Svade/Svadu envelope used by the Megrez integration.

The checks are evidence, not hardware emulation.
A passing profile proves the declared CPU, MMU, DTB, U-Boot `booti`, and userspace contracts.
It does not claim that QEMU reproduces unmodeled clocks, resets, cache controllers, or board peripherals.

## QEMU reboot writeback gate

The isolated gate below writes a new file to a scratch ext2 image, calls
`reboot(2)` directly without a userspace `sync(2)`, and reads the file after a
second boot in the same QEMU process. It then checks the disk with `e2fsck -fn`.
The `--watchdog` variant waits for `asterinas.reboot_after=3` instead of calling
`reboot(2)`. A responsive kernel syncs from a worker thread and reboots; if that
work cannot finish, the emergency reboot remains available after a ten-second
grace period.
The output directory must be new; the gate does not touch a board or an installed
root filesystem. This verifies orderly software reboot writeback, not power-loss
durability or SD-card cache behavior.

```bash
tools/docker/run_dev_container.sh -- make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
tools/docker/run_dev_container.sh -- python3 tools/riscv/reboot_sync_gate.py \
  --output-dir target/reboot-sync-gate-$(date +%Y%m%d-%H%M%S)
tools/docker/run_dev_container.sh -- python3 tools/riscv/reboot_sync_gate.py \
  --watchdog --output-dir target/reboot-sync-watchdog-$(date +%Y%m%d-%H%M%S)
```

The output contains `qemu-serial.log`, `e2fsck.log`, `result.json`, and the
scratch image for review.

For a short, clean-exit check of block flush errors, run one selected ext2
case on a fresh QEMU image. `blkdebug` injects one `EIO`; the guest must report
it from `fsync` or `msync(MS_SYNC)` and then shut down normally. These selected
cases do not run the separate directory-fsync cut-point experiment.

```bash
python3 tools/riscv/ext2_durability_gate.py --case flush-eio \
  --output-dir .local-test/ext2-flush-eio-$(date +%Y%m%d-%H%M%S)
python3 tools/riscv/ext2_durability_gate.py --case msync-eio \
  --output-dir .local-test/ext2-msync-eio-$(date +%Y%m%d-%H%M%S)
```

## Megrez selectable boot menu

The schema-v3 selector provides RockOS, Asterinas Basic, Probe, and Desktop.
It is qualified as a versioned canary before replacing `/extlinux/asterinas.conf`.
See the [design](../../docs/superpowers/specs/2026-09-12-megrez-selectable-boot-menu-design.md)
and [implementation status](../../docs/superpowers/plans/2026-09-12-megrez-selectable-boot-menu.md).

After promotion, routine startup needs only a reboot/reset and menu selection:

| Entry | Purpose | Recovery policy |
| --- | --- | --- |
| RockOS (default) | Maintenance and network file transfer | Normal RockOS shutdown |
| Basic | BusyBox shell in the small initramfs; no Debian disk required | `sync; reboot -f` |
| Probe | Automatic, non-writing boot check | Requests reboot; 90-second software deadline |
| Desktop | Existing Debian, X and Firefox | Explicit shutdown; no automatic deadline |

On a root image that includes the current `megrez-safe-reboot`, an operator
with the isolated root console can request a planned desktop reboot after the
current-boot desktop-ready marker has disarmed the kernel timer:

```bash
ASTERINAS_SAFE_REBOOT_MANUAL=1 ASTERINAS_SAFE_REBOOT_AFTER=1 \
  ASTERINAS_SAFE_REBOOT_CONSOLE=/dev/ttyS0 \
  /usr/lib/asterinas/megrez-safe-reboot
```

The manual mode refuses a stale marker or an armed watchdog. It stops known
writers, waits for the desktop user to exit, syncs the ext2 root, and requests
the software reboot. The automatic timer still exits without rebooting after
desktop handoff. Older installed root images do not have this manual mode;
update the root image before using the command above.

The menu defaults to RockOS after ten seconds. The board's preceding 30-second
firmware delay is unchanged. No routine build, upload, CRC command, manual DTB
patch, or `booti` sequence is required. A hard-locked kernel still requires a
physical reset; the software timer is not a hardware watchdog. Basic's shell
does not currently provide job control.

Desktop shares the kernel, prepared DTB and Stage1 with Basic/Probe. It mounts
the existing Debian partition and uses a temporary, UID-1000 desktop HOME;
browser profiles and caches do not persist across reboot. Other Debian rootfs
writes remain possible. Qualification checks X, framebuffer and Firefox process
readiness and a visible Firefox window, not webpage rendering, Internet access
or input responsiveness. The installed cold-profile ESR image still takes
minutes to expose its window. The test allows up to 300 seconds after the root
console appears; this is a timeout bound, not a performance target.

### Build profile for desktop performance

Use the existing optimized build for desktop performance qualification:

```bash
tools/docker/run_dev_container.sh --offline -- make kernel \
  TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
```

This is a build-time choice, not an additional step on every boot. Keep the
default `RELEASE=0` for unoptimized debugging when needed; do not use it as the
desktop performance baseline. The installed September 12 menu kernel was built
with `profile = "dev"`. Controlled QEMU measurements show substantial VM and
Firefox executable-loading overhead compared with the same source in release
mode. They do not yet establish full-window startup time on the board.

The subsequent release canary reached a visible Firefox window at menu +70–74 s
in two physical boots and passed a local-page JavaScript/DOM-event check with
a verified framebuffer screenshot.
The same-source dev comparison took approximately 282 s to expose its window.
The installed default kernel is still unchanged.
These offline observations do not qualify Internet browsing, USB input,
or the candidate's network-source parity with the deployed integration kernel.

Retain `target/osdk/aster-kernel/bundle.toml` with each candidate to record its
build profile, alongside the existing artifact hashes. The optimized candidate
must still pass the normal qualification before replacing the installed kernel;
do not overwrite the menu's frozen kernel in place. Reuse the existing Debian
image and Stage1. See the [measurements and short probe](../../docs/porting/evidence/2026-09-12-firefox-release-startup.md).

Maintenance commands below run in the persistent development container. They
are for changing/qualifying a generation, not commands to repeat each boot:

```bash
tools/docker/run_dev_container.sh -- make test_riscv_megrez_boot_menu_unit
tools/docker/run_dev_container.sh -- python3 -m tools.riscv.megrez_boot_menu --help
tools/docker/run_dev_container.sh -- python3 -m tools.riscv.megrez_menu_board --help
```

`megrez_boot_menu prepare-dtb` moves the existing framebuffer/USB fixups to
publication time. `prepare` now rejects a DTB unless those exact framebuffer
geometry and USB-host properties are present, so a Desktop selector cannot be
published with the raw board DTB. It records frozen local artifacts and the exact
vendor configuration. `megrez_menu_board stage --from-uboot` boots RockOS and transfers
only missing artifacts, verifies sizes/SHA-256, then publishes a canary. A
Desktop candidate may use `--root-init=systemd --debug-console=isolated-root`
with persistent HOME, matching the physically tested desktop control mode;
the selector rejects pairing that isolated console with `--volatile-home`.
This only prepares a candidate and does not change the installed menu.

A `cycle --mode basic|probe|desktop|rockos|fallback` records one physical test and
returns to U-Boot. `--check-root` optionally runs read-only `e2fsck -fn` from
RockOS, refusing a mounted Debian filesystem.

`promote --from-uboot --evidence DIRECTORY` requires three successful cycles
each for Basic, Probe, RockOS and fallback, and two for Desktop, all for the
same menu identity. It verifies installed files again, backs up the previous
selector, atomically replaces it, and observes an uninterrupted software reboot
through the persistent menu to RockOS. It never modifies the vendor extlinux
file, uses `saveenv`, or deploys a rootfs image. Cold power-on qualification is
separate and must be recorded from a real operator power cycle.

Supply RockOS credentials through `--password-fd`, or explicitly select
`--factory-login` only for the documented unchanged factory account. The board
must be in the state named by the command; failure does not trigger speculative
serial commands or forced resets. A failed cycle retains its `serial.log` and
`result.json` and does not count toward promotion.

## Bounded Megrez desktop startup

Use the fast desktop workflow for normal development boots. Publishing and
starting are deliberately separate: publish only after the kernel, Stage1,
prepared DTB, or expected root identity changes, then reuse that immutable
generation for daily starts.

```bash
make prepare_riscv_megrez_desktop_boot
make run_riscv_megrez_desktop
```

The defaults select
`target/megrez-desktop-boot/build/plan.json`, the stable FTDI
`/dev/serial/by-id/` path, and a timestamped evidence directory below
`target/megrez-desktop-boot/`. Every variable has a
`MEGREZ_DESKTOP_BOOT_*` override for another plan, serial device, host address,
port, or output directory. An existing output directory is rejected rather
than overwritten.

The plan defaults to a disposable home (`--volatile-home`) for repeatable
bring-up. Set `"persistent_home": true` in the plan to use the provisioned
Debian home, including its wallpaper and launcher settings. This changes the
boot arguments and therefore creates a distinct immutable generation; publish
that generation before starting it. The value must be a JSON boolean, not a
string. Keep the default for isolated tests that must not modify the user's
home.

The guest reaches the host and nothing beyond it, and glibc's resolver only
ever asks port 53, so the guest cannot be given a nameserver directly. A host
bridge carries its queries instead; start it beside the proxy and leave it
running:

```bash
python3 -m tools.riscv.megrez_proxy_bridge --dns
```

It listens on `10.100.19.216:15354` and forwards each query to a resolver that
speaks DNS over TCP. `start` warns, but does not refuse to boot, when nothing
answers there: the guest-side shim leaves `/etc/resolv.conf` alone unless its
tunnel has already answered a query, so a missing bridge costs the guest name
resolution without pointing it at a port nothing answers.

`run_riscv_megrez_desktop` performs only the bounded `start` action. It never
boots RockOS, transfers or installs artifacts, rewrites the root partition, or
runs a browser performance workload. Before `booti`, it verifies the byte count
and CRC32 of the immutable partition-3 kernel, Stage1, and DTB. During boot it
prints each observed phase with host elapsed time. Success requires the debug
console, X11/fbdev/Openbox, a visible UID-1000 Firefox window, and guest-local
watchdog disarm within 300 seconds; a successful guest stays running.

After a failed kernel entry, leave the command running: the armed watchdog
reboots the board and the host requires a fresh OpenSBI, U-Boot, and prompt
epoch within the bounded recovery allowance. Diagnose from the last printed
phase and the retained `serial.log`/`result.json`; do not physically reset an
ordinary failed boot. End a successful session from the debug console with
`sync; reboot -f`, which returns the board to U-Boot for the next start.

## SMP4 cross-hart instruction-cache regression

The formal RISC-V regression job runs with exactly four guest CPUs and sets
`RISCV_ICACHE_REQUIRE_SMP4=1`. The guest test pins its writer to one CPU, warms
and rewrites executable code on each of the other three CPUs for 1024
generations, and rejects any topology other than four available CPUs.

The host validates the complete `qemu.log`, not only its tail. A pass requires
one overall regression terminal marker, one exact four-distinct-CPU icache
marker, no skip, and no panic, unexpected exception, or failed SBI remote
`fence.i` marker anywhere in the transcript:

```bash
make run_kernel AUTO_TEST=regression TARGET_ARCH=riscv64 SMP=4 \
  RISCV_ICACHE_REQUIRE_SMP4=1
```

This QEMU test validates the syscall and cross-hart software protocol. Physical
instruction-cache behavior still requires a result from the exact board binary
and configuration being claimed.

## IP socket network namespace regression

Run the short RISC-V QEMU gate when changing IP interface selection or socket
network namespace behavior:

```sh
make run_kernel AUTO_TEST=ip_socket_netns TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
```

It checks inherited and newly created UDP/TCP sockets after `unshare(CLONE_NEWNET)`,
UDP automatic local-address selection, and TCP/UDP privileged-port binding.
[The red/green QEMU evidence](../../docs/porting/evidence/2026-09-24-ip-socket-netns.md)
records the observed behavior and separate dual-stack checks.

## Netlink socket namespace regression

Run this short RISC-V QEMU gate when changing netlink handling or network
namespace behavior:

```sh
make run_kernel AUTO_TEST=netlink_route_netns TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
```

It checks route link and address dumps from sockets created before and after
`unshare(CLONE_NEWNET)`, reuse of the same route and UEVENT port IDs across
namespaces, and rejection of duplicates within one namespace. It then runs
the existing `netlink_route`, `rtnl_err`, and `uevent_err` regressions.
[The request-namespace evidence](../../docs/porting/evidence/2026-09-24-netlink-route-netns.md)
and [the port-table evidence](../../docs/porting/evidence/2026-09-24-netlink-table-netns.md)
record the red/green runs and current multicast-test limit.

## Unit tests

Run the complete host-side suite in isolated workers before launching QEMU:

```bash
make test_riscv_host HOST_TEST_JOBS=32
```

The runner checks `tools/riscv/tests/expected_host_tests.json` for vanished
tests and unexpected skips. Use `make test_riscv_host_list` to print the exact
collected test IDs.

Run the repository-contract tests before launching QEMU:

```bash
make test_riscv_uboot_booti_unit
```

These tests validate immutable profile definitions, address ranges, generated commands, DTB policy, milestone accounting, result classification, cleanup, and artifact identity checks.

## Basic LMBench compatibility smoke

Build the repository-pinned LMBench package alone in the persistent dev container:

```sh
tools/docker/run_dev_container.sh -- nix-build test/initramfs/nix/default.nix \
  --argstr target riscv64 -A benchmark.lmbench \
  --out-link /root/asterinas/target/lmbench-riscv64
```

Install that package and its runtime closure in the guest.
Nix-built executables use an absolute `/nix/store/...` ELF interpreter;
copying only their `bin/` directory into a Debian guest is insufficient.
Use `nix-store -qR` on the package output to list the required store paths.
The existing benchmark initramfs already includes its Nix dependencies.

Copy `tools/riscv/lmbench_smoke.py` to a guest with Python 3.10 or newer,
then run it inside the guest:

```sh
python3 /path/to/lmbench_smoke.py \
  --bin-dir /nix/store/<lmbench-package>/bin \
  --output /tmp/lmbench-smoke.json
```

This executes 18 short checks: six syscalls, fork/exec/shell, pipe latency,
two-process context switching, signal install/catch, 1 MiB memory read/write/copy,
mmap, and page faults.
It uses `-P 1 -W 0 -N 1`, `ENOUGH=10000`, and a ten-second per-case timeout.
A missing measurement, nonzero exit, or timeout fails the run immediately.
The report retains commands, outputs, binary hashes, durations, and units.
`lat_proc` requires `/tmp/hello`; the runner verifies it with an empty environment
and refuses to replace an existing different file.

The Debian/QEMU smoke result establishes compatibility, not performance parity
with Linux or physical-board performance.
It is separate from the x86-oriented comparative benchmark runner and its
longer sampling settings.
[The first RISC-V run and replay instructions](../../docs/porting/evidence/2026-09-23-lmbench-basic.md)
record all 18 checks passing in 8.417 seconds, excluding VM startup and staging.

## Native LMBench `make results`

The native workflow uses [asterinas/lmbench](https://github.com/asterinas/lmbench)
at the same revision as the existing benchmark package. It runs upstream
`config-run`, `results`, and `lmbench`, retaining their benchmark selection,
sampling loops and raw result format. The native target is **`results`**;
our guest entry also accepts `result` as an alias.

Build a self-contained runtime archive inside the persistent container:

```sh
tools/docker/run_dev_container.sh -- python3 tools/riscv/lmbench_native.py \
  package --output target/lmbench-native/runtime.tar.gz
```

Before booting a guest with an existing archive, check that it matches the
current packager and carries both declared script adaptations:

```sh
python3 tools/riscv/lmbench_native.py verify \
  --archive target/lmbench-native/runtime.tar.gz
```

This read-only preflight checks the pinned revision, RISC-V platform, packaged
runner hash, GNUmakefile entry, and adapted scripts. It rejects older archives
before a roughly ten-minute native ALL run. Packaging performs the same check
before writing the checksum file; a changed runner requires rebuilding the
archive. The guest's `make results` command remains the single test trigger.

The archive contains the source, precompiled RISC-V executables, GNU make,
rpcbind, net-tools and their complete Nix runtime closure. Install it once in
a **disposable Debian guest**, with Python 3.10+, grep, awk, sed, tar and useradd.
The guest kernel must expose `/proc/sys/net/ipv4/ip_local_reserved_ports`
and preserve the script pathname in the interpreter's `argv` when running
the Debian `/usr/bin/egrep` wrapper. The latter is fixed by the
[shebang argument change](../../docs/porting/evidence/2026-09-24-shebang-script-argv/README.md).
Keep the archive's `.sha256` file when transferring it and verify before extraction:

```sh
sha256sum -c runtime.tar.gz.sha256
tar xzf runtime.tar.gz -C /
getent passwd rpc >/dev/null || useradd --system --no-create-home \
  --shell /usr/sbin/nologin rpc
. /opt/lmbench/activate
cd /opt/lmbench/src
make results
```

The GNUmakefile invokes the native Makefile with `-o lmbench`, which skips only
its compilation prerequisite. Native scripts still perform configuration and
execute the suite. Two declared script adaptations supply the server addresses
required by this fork and skip the two modern `netstat -i` table headings
before invoking `ifconfig`. The upstream `scripts/version` uses its original
`egrep` command. The native
RISC-V package uses the pinned fork's original `lat_rpc` binary, including its
25 ms total RPC deadline and 2.5 ms UDP retry interval. The original Makefile
and native configuration/results scripts remain intact. Package identities,
original script hashes, the `lat_rpc` hash and every adaptation are recorded
in `asterinas-runtime.json`. Because the RPC retry interval is short, avoid
sending repeated management-console commands while collecting native results;
such commands caused intermittent RPC/UDP timeouts in QEMU diagnostics.
The [current qualification](../../docs/porting/evidence/2026-09-24-native-lmbench-no-egrep/README.md)
records the clean 109/109 QEMU run with these two adaptations and the original
version probe.

The automated configuration selects native **ALL**, one copy, 8 MiB, FASTMEM,
file-system tests enabled, and loopback networking including RPC/HTTP.
ALL means the native driver's selection, not every executable in the repository:
for example `lat_fcntl`, `lat_fifo`, `lat_sem`, `lat_unix_connect`, and `lat_usleep`
are additional standalone benchmarks. It does
not select raw disks or remote machines, and always disables mailing results.
Native repetitions are retained. This is an on-demand suite qualification;
use the separate 18-case smoke for cheap daily checks. The default deadline is
900 seconds; an explicit `LMBENCH_TIMEOUT=600 make results` tightens the bound.
TCG output is not a physical hardware performance score.

Raw native results remain in `results/riscv64-unknown-linux-gnu/`. Each attempt
also creates `asterinas-runs/run-*/` with its configuration, input answers,
stdout/stderr, rpcbind log, raw-result copy and `report.json`. Upstream scripts
can exit zero despite individual failures: our entry additionally audits expected
measurements, table sizes, curves and diagnostic output, returning nonzero for
missing measurements, errors or timeout. A TLB estimate absent under TCG is
reported as missing rather than invented. Failed attempts retain their evidence.
The runner reserves `/tmp/hello` and refuses an existing file or symbolic link;
it reaps its process groups and cleans its own helper on normal exit or interruption.

[Native workflow evidence and remaining gaps](../../docs/porting/evidence/2026-09-23-lmbench-native.md)
record the tested kernel and suite identities.

## Linux Test Project syscall gate

The isolated LTP gate cross-builds the pinned LTP `20260529` syscall suite,
boots it through the guarded U-Boot runner,
and stores its evidence below `target/ltp/`.
It does not reuse `target/qemu-uboot/current`,
so running it cannot replace the prepared desktop boot disk.

Run its host tests with:

```bash
make test_riscv_ltp_unit
```

After building the RISC-V kernel and LTP initramfs,
record a baseline with:

```bash
python3 tools/riscv/ltp_gate.py run \
  --kernel target/osdk/aster-kernel-osdk-bin.Image \
  --smp 1 --run-id baseline-m1-smp1 --skip-build --baseline
```

See [the LTP gate operator guide](ltp/README.md)
for the exact containers, strict mode, SMP=4 runs, result schema,
and evidence-provenance rules.

## Debian persistent root

The Debian M1 gate validates a signed Trixie `riscv64` ext2 root, hands off
from a minimal stage-1 initramfs into Debian `/bin/bash`, and boots the same
writable root twice to prove persistence. It is an infrastructure gate: four
harts, Sv39, two VirtIO block devices, no network, display, USB, or input.

Run its local contract tests with:

```bash
make test_riscv_debian_rootfs_unit
```

See [the Debian persistent-root operator guide](debian/rootfs/README.md) for
the signed root build, current-main kernel/U-Boot/DTB/stage-1 preparation,
explicit two-boot target, and evidence inspection commands.

## Megrez persistent Debian shell

This workflow freezes separate current kernels for the two MMU contracts:
generic QEMU is built with `FEATURES=riscv_sv39_mode`, while Megrez uses the
default Sv48 build. Never reuse one artifact for the other platform. The
bundle also binds the signed Debian root, Stage1, both four-hart DTBs, and
U-Boot to one clean Git commit.

The physical order is deliberately read-only first: run ` inventory ` before
` install-if-needed `. A matching inventory skips installation. A measured
mismatch may authorize the Asterinas-only installer to write exactly
`/dev/mmcblk0p2`; this workflow must not boot Linux as an installer or runtime.
The short EIC7700X watchdog is forbidden during a full-device hash or install;
the bounded Asterinas reboot timer and fresh U-Boot recovery epoch are used
instead. `gate` performs two boots with one persistence nonce, and `handoff`
is allowed only after that physical result passes.

The physical boot transport is serial YMODEM for the compressed current
kernel and Stage1, plus a read-only, CRC-checked load of
`eic7700-milkv-megrez.dtb` from eMMC partition 1. The board's U-Boot GMAC at
`0x50400000` is not the RJ45 path used by Asterinas, so TFTP is deliberately
not used for inventory, the two-boot gate, or handoff. The network installer
starts only after Asterinas owns its verified RJ45 GMAC.

Use one stable evidence directory:

```bash
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

This milestone proves a persistent interactive Debian shell. The next scope is
systemd, network, and desktop; none is claimed by this gate.

## Current-main Megrez physical graphics

This gate is based on `origin/main` commit
`69a7b6e41ca74932f79d917f3638199da573b1e9`. The candidate branch adds the
opt-in debug-root console and physical-interaction witness without importing a
different RISC-V kernel baseline. Record both `git rev-parse origin/main` and
`git rev-parse HEAD` with every run so that the upstream base and candidate
source remain distinguishable.

Before building, require a clean tracked worktree and confirm that the pinned
base is an ancestor. The QEMU and physical results record artifact SHA-256
values; retain this source record beside those results:

```bash
set -euo pipefail
PINNED_MAIN=69a7b6e41ca74932f79d917f3638199da573b1e9
EVIDENCE_ROOT="$PWD/target/current-main-physical-graphics"
SOURCE_RECORD="$EVIDENCE_ROOT/source-identity.txt"
mkdir -p -m 0700 "$EVIDENCE_ROOT"
test ! -e "$SOURCE_RECORD"
test -z "$(git status --porcelain --untracked-files=all)"
test "$(git rev-parse origin/main)" = "$PINNED_MAIN"
git merge-base --is-ancestor "$PINNED_MAIN" HEAD
SOURCE_TMP=$(mktemp "$EVIDENCE_ROOT/.source-identity.XXXXXX")
trap 'rm -f -- "$SOURCE_TMP"' EXIT
{
  printf 'origin_main=%s\n' "$(git rev-parse origin/main)"
  printf 'candidate_head=%s\n' "$(git rev-parse HEAD)"
  printf 'tracked_and_untracked_clean=true\n'
} >"$SOURCE_TMP"
chmod 0600 "$SOURCE_TMP"
mv -- "$SOURCE_TMP" "$SOURCE_RECORD"
trap - EXIT
```

The QEMU run binds the source-built current-main `--kernel` to seven immutable
supporting inputs: `--uboot`, `--dtb`, `--stage1-initramfs`, `--root-image`,
`--root-manifest`, `--packages-lock`, and `--package-checksums`. Use one set of
these exact paths for the debug-console, browser-web, and interaction gates;
do not rebuild or replace an input between runs.

```bash
make test_riscv_physical_graphics_unit

QEMU_INPUTS=(
  "DEBIAN_KERNEL=$PWD/target/osdk/aster-kernel-osdk-bin.Image"
  "DEBIAN_UBOOT=$PWD/target/qemu-uboot/cache/u-boot-build/u-boot"
  "DEBIAN_DTB=$PWD/target/qemu-uboot/current/qemu-virt.dtb"
  "DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-riscv/stage1/initramfs.cpio"
  "DEBIAN_ROOT_IMAGE=$PWD/target/debian-riscv/browser-web/rootfs/debian-root.ext2"
  "DEBIAN_ROOT_MANIFEST=$PWD/target/debian-riscv/browser-web/rootfs/rootfs-manifest.json"
  "DEBIAN_PACKAGES_LOCK=$PWD/target/debian-riscv/browser-web/rootfs/packages.lock"
  "DEBIAN_PACKAGE_CHECKSUMS=$PWD/target/debian-riscv/browser-web/rootfs/source-metadata/package-checksums"
)
make test_riscv_debian_debug_console_qemu_gate "${QEMU_INPUTS[@]}" \
  DEBIAN_DEBUG_CONSOLE_QEMU_GATE_OUTPUT="$PWD/target/current-main-physical-graphics/qemu-debug-console"
make test_riscv_debian_browser_web_qemu_gate "${QEMU_INPUTS[@]}" \
  DEBIAN_BROWSER_WEB_QEMU_GATE_OUTPUT="$PWD/target/current-main-physical-graphics/qemu-browser-web"
make test_riscv_physical_graphics_qemu_gate "${QEMU_INPUTS[@]}" \
  RISCV_PHYSICAL_GRAPHICS_QEMU_GATE_OUTPUT="$PWD/target/current-main-physical-graphics/qemu-interaction"
```

The QEMU adapter injects keys through HMP and absolute pointer events through
a private QMP socket into the VirtIO keyboard and tablet.
HMP relative mouse moves cannot drive the tablet.
It validates three nonce-bound browser cycles and captures pixels, but its
result always records `"physical":false`: QEMU cannot satisfy the physical
result or prove the Megrez display scanout and real USB xHCI/HID paths.

The interaction adapter enters the Stage1 `isolated-root` console first. Fixed
boot parameters mask competing evidence/network and getty units, while Stage1
provides the volatile browser home. The host completes the low-load
kernel/rootfs probe before issuing one bounded Firefox start request. That
administrator-only request uses systemd's
`ignore-dependencies` job mode because the production browser unit requires
the online evidence service that this local interaction gate intentionally
masks.  The desktop and timeline prerequisites are enqueued explicitly, and
the browser script's own `wait-x` check remains the display-readiness barrier.
All subsequent preflight, browser-start, interaction-cycle, and final-state
requests call the fixed-action Stage1 `physical-graphics-control` helper. Each
request is below 128 bytes; the helper accepts only validated numeric and
nonce arguments and preserves the existing result markers. At the configured
10 ms per serial byte, this removes about 45 seconds of command transmission
from one interaction cycle compared with the former inline shell programs.
Python probes use an ephemeral `/run` bytecode-cache prefix, so repeated
cycles reuse memory-backed bytecode and do not mutate the installed ext2
cache.
This ordering and transport boundary avoid both multi-kilobyte serial commands
and the start-stop-restart race that let Firefox starve the diagnostic shell.
The measured per-cycle UART request time fell from about 48.2 seconds to 2.92
seconds, while an incremental menu update transfers only Stage1 and the menu
in about 55 seconds. The persistent offline development container reuses the
cross toolchain and Cargo/Rustup/Nix caches; neither operation rebuilds the
root image or downloads dependencies.

Never infer keyboard and pointer roles from `/dev/input/eventN`. The two
physical xHCI controllers initialize concurrently: retained runs on the same
board observed both keyboard=`event1`, pointer=`event0` and the reverse. The
rootfs device-access helper matches the Linux input bus, name, and physical
path, creates `/run/asterinas-input/keyboard` and
`/run/asterinas-input/pointer`, and only then allows Xorg to start. Missing or
duplicate identities fail closed. It reads the `EVIOCGID`, `EVIOCGNAME`, and
`EVIOCGPHYS` ioctls implemented by Asterinas instead of assuming Linux sysfs
identity files exist. When an older partition-2 root is reused,
the Stage1 control helper derives the same identities and supplies a transient
Xorg provider configuration under `/run`; it does not rewrite partition 2.
Readiness is accepted only when Xorg has both resolved evdev nodes open. The
interaction page is likewise carried by Stage1, preventing its DOM evidence
schema from drifting from the Stage1 gate.
The Stage1 initramfs must come from the same source revision so that the
`isolated-root` debug-console mode is available; an older cached Stage1 is
rejected instead of silently falling back to the normal desktop boot.

Browser evidence also publishes
`browser-performance-provenance.json`, which binds the observed display
provider, framebuffer geometry, Xorg package versions, and Firefox JIT-overlay
selection to the exact rootfs manifest. Completed physical interaction cycles
carry trusted-input-to-frame samples plus nearest-rank p50/p95 values. Keep the
same page and evidence schema when comparing the current `fbdev` provider with
a future `drm` provider; the desktop session selects a provider-specific Xorg
configuration through `ASTERINAS_DISPLAY_PROVIDER`. This integration adds the
selection and evidence boundary only and intentionally does not modify DRM
code.

The physical guest keeps its fixed 900-second safety reboot. The gate defaults
to three cycles for release-grade repeatability, but accepts `--cycles 1` for
one information-rich experimental interaction. Graphical setup, the requested
cycles, display evidence, and final verification share that lifetime, with 30
seconds reserved before reboot.
Per-phase timeout settings are upper bounds, not extensions of the board's
remaining lifetime; an exhausted budget fails closed and proceeds to recovery.

The real run consumes a schema-2 `debian-browser` debug plan. Its canonical
order is `kernel`, `initramfs`, `qemu_dtb`, `megrez_dtb`, `u_boot`,
`root_image`, `root_manifest`, `packages_lock`, `package_checksums`, and
`in_release`; the prepare target recomputes every size, SHA-256, and CRC32.
The plan's `megrez_dtb` must already pass `megrez_boot_menu prepare-dtb`.
The gate verifies that immutable file on the host, checks its MMC CRC, and
boots it directly; it no longer repeats framebuffer and USB `fdt set`
commands at every experiment.
Use a stable `/dev/serial/by-id/...` path. The release-grade mode asks an HDMI
capture program to atomically create or replace the `--hdmi-capture` file only
after the gate asks for the cyan final-cycle image. First validate every plan
artifact and print the exact command, without opening the serial device or
changing U-Boot state:

```bash
make prepare_riscv_megrez_physical_graphics \
  MEGREZ_PHYSICAL_GRAPHICS_PLAN="$PWD/target/megrez-debug/debug-plan.json" \
  MEGREZ_PHYSICAL_GRAPHICS_DEVICE=/dev/serial/by-id/usb-REPLACE_ME \
  MEGREZ_PHYSICAL_GRAPHICS_HDMI_CAPTURE="$PWD/target/current-main-physical-graphics/physical/operator-hdmi.png" \
  MEGREZ_PHYSICAL_GRAPHICS_OUTPUT="$PWD/target/current-main-physical-graphics/physical/evidence"
```

Run the command printed by that target. For each of the three 180-second
interaction windows, type the displayed random 16-hex-digit nonce on the
physical USB keyboard, then move the physical USB mouse and click the amber
button. The guest is booted with `asterinas.reboot_after=1140`; the host uses a
1170-second recovery wait so it can retain the final HDMI image and still
observe the fresh U-Boot prompt. The printed command makes every deadline
explicit: opening the serial link is bounded at 60 seconds, artifact
preparation at 300 seconds, graphical readiness at 300 seconds, each cycle at
180 seconds, a measured non-JIT Marionette setup at 540 seconds, and the
post-cycle HDMI update at 60 seconds. These are caps, not reserved waiting
periods; respond to each prompt immediately. The capture
must be a complete 1-byte-to-64-MiB PNG or JPEG that remains unchanged for at
least 0.5 seconds. A missing input record, DOM transition,
screenshot, HDMI update, recovery prompt, or any panic/xHCI/framebuffer fatal
marker produces `passed:false` while retaining the diagnostic evidence.

The external-HDMI path also needs host `ffmpeg` and `ffprobe`. It decodes the
final Firefox screenshot and HDMI capture, requires both to be 1920 × 1080,
checks that the Firefox image contains the cyan test page, and compares a
96 × 54 pixel sample. At least 95% of sampled pixels must agree within 32 per
channel and mean absolute channel error must be at most 12. A fresh image of
the wrong display now fails the gate; passing comparison metrics are retained
as `hdmi-pixel-check.json`. These conservative thresholds need validation
against the actual capture device before release qualification. Browser frame
callbacks still do not measure physical input-to-visible-frame latency.

When external capture hardware is unavailable during experimental development,
select the explicitly weaker `operator-attested` mode. It preserves real USB
evdev input, trusted Firefox DOM state, the serial guest PNG, final Firefox PID
and restart checks, and recovery, but it does not claim that an HDMI image was
captured. Existing versioned MMC files can be selected so this path performs no
artifact transfer or image rebuild:

```bash
make prepare_riscv_megrez_physical_graphics \
  MEGREZ_PHYSICAL_GRAPHICS_PLAN="$PWD/target/current-main-physical-graphics/physical/plan-firefox-77d7e42c.json" \
  MEGREZ_PHYSICAL_GRAPHICS_DEVICE=/dev/serial/by-id/usb-REPLACE_ME \
  MEGREZ_PHYSICAL_GRAPHICS_DISPLAY=operator-attested \
  MEGREZ_PHYSICAL_GRAPHICS_CYCLES=1 \
  MEGREZ_PHYSICAL_GRAPHICS_MMC_KERNEL=asterinas-77d7e42c-220572e8.Image \
  MEGREZ_PHYSICAL_GRAPHICS_MMC_INITRAMFS=asterinas-78c4a36c-f4d9b349-stage1.cpio \
  MEGREZ_PHYSICAL_GRAPHICS_MMC_DTB=dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb \
  MEGREZ_PHYSICAL_GRAPHICS_OUTPUT="$PWD/target/current-main-physical-graphics/physical/operator-attested-evidence"
```

After the single keyboard/mouse cycle reaches cyan PASS, visually inspect the
physical monitor and enter exactly the printed nonce-bound line,
`confirm-cyan-pass <suffix>`. A successful one-cycle result
proves one complete interaction path. Such a result does not prove three-cycle repeatability.
The evidence is published as `operator-display-attestation.json`, with `hdmi: null`; no file is
presented as an HDMI capture.

## Physical Firefox daily-use baseline

The daily-use baseline is a closed, three-run experiment. Build the current
Sv39/SMP=4 kernel in the persistent project container, then build Stage1 twice
with the pinned offline rootfs image. The two Stage1 hashes must match before a
board run is admitted:

```bash
tools/docker/run_dev_container.sh --workspace "$PWD" -- \
  make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
mkdir -p target/firefox-daily-use-physical/stage1-a \
  target/firefox-daily-use-physical/stage1-b
for copy in a b; do
  tools/docker/run_dev_container.sh \
    --image asterinas/asterinas:0.18.0-20260702-riscv-rootfs --offline -- \
    tools/riscv/debian/rootfs/build_stage1.sh \
    "target/firefox-daily-use-physical/stage1-${copy}/initramfs.cpio"
done
sha256sum target/firefox-daily-use-physical/stage1-{a,b}/initramfs.cpio
```

Create and check `target/firefox-daily-use-physical/plan.json` as described by
the Megrez debug-plan workflow. Before touching serial or U-Boot, validate the
frozen plan, all artifact hashes, the stable serial path, fixture addresses,
and a new output directory. This prepare command performs validation only and
prints the exact real command; it does not open the serial device:

```bash
make prepare_riscv_megrez_firefox_daily_use \
  MEGREZ_FIREFOX_DAILY_USE_PLAN="$PWD/target/firefox-daily-use-physical/plan.json" \
  MEGREZ_FIREFOX_DAILY_USE_DEVICE=/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0 \
  MEGREZ_FIREFOX_DAILY_USE_OUTPUT="$PWD/target/firefox-daily-use-physical/run-a1" \
  MEGREZ_FIREFOX_DAILY_USE_FIXTURE_BIND=10.100.19.216 \
  MEGREZ_FIREFOX_DAILY_USE_BOARD_PEER=10.100.19.200
```

Preflight the dedicated host Ethernet address and board peer before the first
run. The fixture is fixed at `10.100.19.216:17894`, the board peer is
`10.100.19.200`, and the serial link must resolve through the stable by-id path:

```bash
ip address show | grep -F 10.100.19.216
ping -c 1 -W 2 10.100.19.200
test -e /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0
```

Run exactly one profile per boot. These are three distinct one-shot commands;
do not reuse an output directory or combine profiles in one guest lifetime:

```bash
python3 -m tools.riscv.megrez_firefox_daily_use \
  /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0 \
  --plan target/firefox-daily-use-physical/plan.json \
  --output-directory target/firefox-daily-use-physical/run-a1 \
  --fixture-bind 10.100.19.216 --fixture-port 17894 --board-peer 10.100.19.200
python3 -m tools.riscv.megrez_firefox_daily_use \
  /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0 \
  --plan target/firefox-daily-use-physical/plan.json \
  --output-directory target/firefox-daily-use-physical/run-a2 \
  --fixture-bind 10.100.19.216 --fixture-port 17894 --board-peer 10.100.19.200
python3 -m tools.riscv.megrez_firefox_daily_use \
  /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0 \
  --plan target/firefox-daily-use-physical/plan.json \
  --output-directory target/firefox-daily-use-physical/run-a3 \
  --fixture-bind 10.100.19.216 --fixture-port 17894 --board-peer 10.100.19.200
```

Each command must observe the fresh U-Boot prompt after the safety reboot; a
missing recovery prompt makes that run unqualified. Generate the report only
from three distinct `qualified` run directories with identical immutable
deployment identity. The reporter validates every private manifest and never
repairs evidence:

```bash
python3 -m tools.riscv.megrez_firefox_daily_use_report \
  target/firefox-daily-use-physical/run-a1 \
  target/firefox-daily-use-physical/run-a2 \
  target/firefox-daily-use-physical/run-a3 \
  --json-output target/firefox-daily-use-physical/baseline-report.json \
  --markdown-output target/firefox-daily-use-physical/baseline-report.md
```

### Review policy for the experimental RISC-V fork

This RISC-V support is experimental development in the `asterinas-riscv` fork.
The fork intentionally does not carry or invoke the former repository-local
`aster-code-review` skill, its automation, or its compatibility symlink.
Use normal human-readable diff review plus the existing tests and hardware
gates instead.
When integrating future upstream changes, keep this removal as an explicit
fork policy unless the project owner decides to adopt a replacement review
workflow.

## Megrez unattended boot stability

The unattended gate separates deployment from acceptance. The schema-2
`DebugPlan` is the immutable deployment manifest; its sizes, SHA-256 values,
CRC32 values, and versioned MMC paths identify one release. A routine gate run
loads only the existing kernel, Stage1 initramfs, and DTB from MMC partition 1.
It does not build, upload, or fall back to serial transfer;
partition 2 is never written.

Keep compilation and all unit tests in the persistent development container:

```bash
tools/docker/run_dev_container.sh -- \
  make test_riscv_megrez_boot_stability_unit
```

Boot RockOS only when the next `DebugPlan` names a kernel or initramfs that is
not already present on partition 1.
Transfer only changed, versioned files,
then verify them with the controlled measurement tool below. It boots RockOS,
uses native `stat` and `sha256sum`, binds every output to a fresh random nonce
and the plan identity, performs a normal reboot, and requires a new
OpenSBI/U-Boot epoch before publishing. The private raw serial source is kept
as `deployment-measurement.serial.log`; the derived schema-2
`deployment-attestation.json` binds its SHA-256, RockOS boot ID,
`/dev/mmcblk1p1`, filenames, sizes, and observed SHA-256 values.

With the board at a fresh U-Boot prompt, generate the receipt once. The
password prompt is read from the terminal and is never placed in argv, an
environment variable, or the retained transcript:

```bash
python3 -m tools.riscv.megrez_rockos_attestation \
  /dev/serial/by-id/usb-FTDI_FT232R_USB_UART-REPLACE-if00-port0 \
  --plan "$PWD/target/current-main-physical-graphics/physical/plan-isolated-resolved.json" \
  --output-directory "$PWD/target/current-main-physical-graphics/physical/rockos-attestation" \
  --mmc-kernel asterinas-COMMIT-CRC.Image \
  --mmc-initramfs asterinas-COMMIT-CRC-stage1.cpio \
  --mmc-dtb dtbs/linux-image-VERSION/eswin/eic7700-milkv-megrez.dtb
```

The receipt and raw log are reused by every later boot of the same immutable
deployment. Retain the preceding plan, files, receipt, and log for rollback. A
kernel update does not require rebuilding or reinstalling the partition-2
Debian root. This is an auditable maintenance receipt, not a hardware root of
trust: the host controlling the exclusive serial port, this tool, and RockOS
remain inside the deployment trust boundary.

Run the gate directly on the host as the `dialout` user. The routine gate
validates the frozen plan and RockOS SHA-256 receipt, then observes the MMC
size/CRC32 at U-Boot. It does not reread local build outputs or require the
plan's original container paths to exist.
This keeps serial ownership and the private evidence files with the host user.
With the board at a fresh U-Boot prompt, run three unattended Asterinas boot
and recovery epochs using the frozen plan and the filenames already verified
on MMC:

```bash
python3 -m tools.riscv.megrez_boot_stability \
  /dev/serial/by-id/usb-FTDI_FT232R_USB_UART-REPLACE-if00-port0 \
  --plan "$PWD/target/current-main-physical-graphics/physical/plan-isolated-resolved.json" \
  --output-directory "$PWD/target/current-main-physical-graphics/physical/boot-stability" \
  --deployment-attestation "$PWD/target/current-main-physical-graphics/physical/rockos-attestation/deployment-attestation.json" \
  --deployment-measurement-log "$PWD/target/current-main-physical-graphics/physical/rockos-attestation/deployment-measurement.serial.log" \
  --mmc-kernel asterinas-COMMIT-CRC.Image \
  --mmc-initramfs asterinas-COMMIT-CRC-stage1.cpio \
  --mmc-dtb dtbs/linux-image-VERSION/eswin/eic7700-milkv-megrez.dtb
```

Each cycle verifies the MMC byte count and CRC32, reaches the isolated root
debug console, masks the external network-evidence workload, and bind-mounts
the volatile `/run/asterinas-physical-home` over `/home/asterinas`. Xorg,
Openbox, and Firefox therefore keep logs, profiles, and caches on tmpfs instead
of relying on unsupported partition-2 writeback. The gate then requires
systemd, `/dev/fb0`, Xorg using that framebuffer, Openbox, and an active Firefox
service with zero restarts. It has no keyboard, mouse, HDMI, or network-success
requirement.

Serial shell work is split into short, idempotent, acknowledged steps. A lost
acknowledgement is detected after 15 seconds; the host sends Control-C and a
newline to restore the shell boundary before retrying. Before an immediate
`reboot -f`, the gate retains a bounded snapshot of the kernel ring buffer,
mounts, failed systemd units, graphical unit state, process tree, full service
status, journal, and Xorg/Firefox logs. An oversized diagnostic snapshot fails
closed instead of dropping the earlier kernel log. `cycle-1.diagnostics.log`
and its peers contain those snapshots; `deployment-measurement.serial.log`,
`deployment-attestation.json`, `deployment.json`, `sha256sums.txt`, and the
per-cycle serial logs bind them to the release.

`result.json` is replaced last and reports pass only after all three cycles
return to distinct fresh U-Boot epochs without a panic, oops, fatal exception,
out-of-memory event, ext2 error, or block I/O error. To roll back, select the
previous plan and its retained versioned filenames, then run the same gate.

The default 240-second readiness deadline covers the measured cold-start
variance; it is an upper bound and does not delay a successful cycle. Remaining
`systemd-random-seed`/`systemd-sysctl` failures, read-only block writeback
attempts, syscalls 213/272, and `SA_NOCLDSTOP` warnings are retained as kernel
compatibility work. They are not hidden by the deployment gate and are not
treated as proof of a fatal graphics failure.

## One-command Megrez desktop and Firefox diagnosis

Configure the already deployed, measured MMC release once. This operation is
local-only: it validates and hashes the existing plan, RockOS receipt, and
measurement log, then writes one private bundle. It does not open the serial
device, build an image, transfer a file, boot RockOS, or write either MMC
partition:

The bundle records the plan's canonical semantic SHA-256 identity. The receipt
and measurement-log fields record their exact byte SHA-256 identities; loading
the bundle cross-validates all three before opening the serial device.

```bash
python3 -m tools.riscv.megrez_desktop configure \
  --plan "$PWD/target/current-main-physical-graphics/physical/plan-isolated-resolved.json" \
  --device /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0 \
  --deployment-attestation "$PWD/target/current-main-physical-graphics/physical/rockos-attestation-isolated-resolved/deployment-attestation.json" \
  --deployment-measurement-log "$PWD/target/current-main-physical-graphics/physical/rockos-attestation-isolated-resolved/deployment-measurement.serial.log" \
  --mmc-kernel asterinas-790ab694-34bc1cc0.Image \
  --mmc-initramfs asterinas-78c4a36c-f4d9b349-stage1.cpio \
  --mmc-dtb dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb \
  --evidence-root "$PWD/target/megrez-desktop/evidence" \
  --output "$PWD/target/megrez-desktop/current.json"
```

After that one-time configuration, the routine desktop boot is one command:

```bash
python3 -m tools.riscv.megrez_desktop start
```

`start` reads only the three named files already on MMC partition 1, verifies
their U-Boot byte counts and CRC32 values, and reaches the existing systemd,
framebuffer, Xorg, Openbox, and Firefox readiness contract. A successful run
closes the host serial descriptor and leaves the desktop running; it does not
arm the 900-second diagnostic reboot timer. A failure after Asterinas starts
collects bounded `dmesg`, systemd, process, Xorg, and Firefox evidence, requests
`reboot -f`, and checks for a new U-Boot epoch. Complete firmware/SBI loss is
reported as `manual-reset-required`; without an independent reset controller,
software cannot recover that state remotely.

For one unattended real-web check, use the same configured bundle:

```bash
python3 -m tools.riscv.megrez_desktop browse-firefox
```

`browse-firefox` owns a temporary host bridge from `10.100.19.216:17893` to
the local proxy (port `7890` by default), boots the existing three MMC files
once, and retains the frozen network and static-neighbor boot arguments. It
synchronizes the guest clock from a validated plain-HTTP `Date` header before
any HTTPS request, waits for Marionette with a real loopback TCP connection,
and uses one WebDriver session to validate and capture only the Baidu homepage.
It does not run the 20-request fixture stress test, rewrite partition 2, or use
`/proc/net/tcp` as a readiness oracle. The JSON and PNG are transferred with
nonce, byte-count, and SHA-256 framing, after which the action requests a reboot
and verifies a fresh U-Boot prompt. The PNG is a Marionette content-viewport
capture, not an HDMI framebuffer dump: it must be a complete decodable image of
at least 1024x700, while the paired JSON and ready marker independently bind the
same WebDriver session to Baidu HTTPS, verified TLS timing, and the required DOM.
The real-web action has a 1050-second terminal recovery timer and stops its page
gate early enough to export failure evidence. It records payload-free
Marionette command boundaries plus at most 128 kernel TCP events for loopback
port 2828; routine `start` keeps its existing non-diagnostic behavior.
Increasing the terminal bound does not delay a successful page. On a host with
a different local proxy port, pass `--proxy-upstream-port PORT`.

The two experiment helpers are carried by the small Stage1 initramfs and
bind-mounted into the ephemeral `/run/asterinas-tools` path. Updating this
workflow therefore replaces only the versioned Stage1 file on MMC partition 1
(about 650 KiB); deployment does not rewrite the Debian root image on
partition 2.

Use the heavier action only for one explicitly falsifiable Firefox experiment:

```bash
python3 -m tools.riscv.megrez_desktop diagnose-firefox \
  --hypothesis "The physical Firefox Marionette greeting completes and WebDriver:NewSession is fully sent but produces no response-header byte before the fixed 300-second deadline." \
  --contrary-outcome "The listener does not become ready, NewSession is not fully sent, a response is partial or rejected, or NewSession returns a valid session."
```

The diagnostic records a stable Firefox identity, captures the before snapshot,
then lets the one selected `WebDriver:NewSession` operation perform the loopback
connection, Marionette greeting, request, and response under one 300-second
absolute deadline. It does not send `WebDriver:Status` because that command is
not part of Firefox ESR 140's direct Marionette command table. Exact offline
NewSession error output is enabled only for this bounded diagnostic. It also
captures during/after Firefox-tree and syscall snapshots, kernel/service logs,
and payload-free Marionette transport counters, then always requests recovery.
The complete physical experiment is capped at 15 host minutes before the
independent recovery wait. It records zero QEMU runs, exactly one physical boot,
and requires zero artifact-transfer bytes.

Every runtime identity is admitted once in the mode-`0600`
`target/megrez-desktop/evidence/experiments.jsonl` ledger. Rewording the
hypothesis does not permit another identical boot. A repeat requires a proven
observer defect and a changed, regression-tested diagnostic protocol identity.
Each run gets a new mode-`0700` directory; raw evidence is mode `0600`, and
`result.json` is published last with hashes for every retained input and file.
The frozen partition-2 image is not reinstalled. Because
`/home/asterinas` is backed by the physical run's volatile tmpfs, Firefox still
starts with a cold profile on every boot; current measured graphical readiness
is approximately 175–220 seconds, not an instant warm resume.

Protocol v7 writes result schema version 2. Its classifier reports only the first
observed boundary: listener unavailable, NewSession not sent, response absent,
response partial, response rejected, response complete, or evidence incomplete.
Older `mmc-graphics-final-19` evidence predates transport records and therefore
classifies only as `evidence-incomplete`; it is not proof of a TCP, `poll`,
scheduler, or Firefox deadlock. If a new run is also incomplete, fix and replay
the observer before any further live Firefox boot.

Run the focused host tests in the persistent development container. The tests
use local loopback sockets, so do not pass the container launcher's `--offline`
network-isolation flag; the command still performs no dependency download:

```bash
tools/docker/run_dev_container.sh -- make test_riscv_megrez_desktop_unit
```

## Megrez firmware framebuffer handoff

`megrez_board_session.py` can add the physically established 1920x1080
scanout at `0xfd800000` to the live DTB before `booti`. The change is RAM-only:
the tool never runs `saveenv`, and the default serial-only path remains
unchanged unless `--firmware-framebuffer` is present.

Asterinas currently selects only the first `console=` value for `/dev/console`.
The framebuffer gate therefore requires `console=tty0` to be first. The closed
`firmware-framebuffer` final profile returns success when the serial log has
observed the kernel register the handoff; Debian/systemd output after that is
expected on HDMI rather than on the serial console.

```bash
PYTHONPATH=tools/riscv python3 tools/riscv/megrez_board_session.py /dev/ttyUSB0 \
  --booti ASTERINAS_IMAGE_ON_BOOT_FS \
  --initrd STAGE1_INITRAMFS_ON_BOOT_FS \
  --dtb DTB_ON_BOOT_FS \
  --expected-crc32 booti=8hex,dtb=8hex,initrd=8hex \
  --bootargs "console=tty0 loglevel=info init=/init asterinas.reboot_after=600 -- --root-init=systemd" \
  --firmware-framebuffer \
  --final-profile firmware-framebuffer \
  --yes \
  --log /absolute/path/to/megrez-framebuffer.serial.log
```

This proves the current-main firmware framebuffer registration boundary. It
does not by itself prove Xorg, a desktop session, or native EIC7700 DRM.

## Opt-in Asterinas root serial console

The `debug-root-console` profile verifies a one-boot-only root shell created by
Stage1 under the `/run` tmpfs. It does not change the rootfs shadow database,
install a password, or persist a sudo rule. The exact selectors are accepted
only together:

```text
-- --root-init=systemd --debug-console=root
```

The physical graphics orchestrator instead uses
`--debug-console=isolated-root`. Stage1 then creates a transient
`/run/systemd/system.control/default.target` that points at the debug-console
target. The `system.control` lookup tier takes precedence over the rootfs's
`/etc/systemd/system/default.target`, so no rootfs service can start before the
orchestrator installs transient `/run/systemd/system.control` masks for the
competing network evidence services and starts the graphical target. The
same high-priority tier masks the generated serial getty so it cannot take the
UART away from the debug shell. The kernel command line deliberately does not
declare `console=ttyS0`; Stage1 gives the debug service explicit ownership with
`TTYPath=/dev/ttyS0`, without inviting systemd's console generator to create a
second owner. The physical boot combines `loglevel=off` with
`asterinas.klog_capture=info`: kernel records remain available to later
`dmesg` probes, while repeated writeback failures cannot saturate the
115200-baud control channel. This does not depend on systemd's
optional kernel-command-line generators, and the ordinary
`--debug-console=root` mode continues to boot the rootfs's normal default
target.

First run the bounded acceptance boot. Resolve the stable FTDI path rather
than relying on the current `ttyUSB0` number, retain the 120-second Asterinas
recovery timer, and require a fresh U-Boot epoch:

```bash
SERIAL=/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0
PYTHONPATH=. python3 tools/riscv/megrez_board_session.py "$SERIAL" \
  --booti ASTERINAS_IMAGE_ON_BOOT_FS \
  --initrd STAGE1_INITRAMFS_ON_BOOT_FS \
  --dtb DTB_ON_BOOT_FS \
  --expected-crc32 booti=8hex,dtb=8hex,initrd=8hex \
  --bootargs "console=tty0 console=ttyS0 loglevel=off init=/init asterinas.reboot_after=120 -- --root-init=systemd --debug-console=root" \
  --firmware-framebuffer \
  --final-profile debug-root-console \
  --milestone-timeout 150 \
  --require-recovery \
  --yes \
  --log /absolute/path/to/megrez-debug-root-bounded.serial.log
```

The gate sends only five fixed, read-only commands. It requires UID 0, PID 1
`systemd`, `/dev/mmcblk0p2 ext2`, and active desktop evidence/session units.
It also retains the complete framed exchange in the serial log on failure.

After that bounded gate passes, an operator handoff may omit only the recovery
timer and `--require-recovery`. The runner closes its descriptor after the
fixed probes, so the same root prompt can then be opened interactively:

```bash
PYTHONPATH=. python3 tools/riscv/megrez_board_session.py "$SERIAL" \
  --booti ASTERINAS_IMAGE_ON_BOOT_FS \
  --initrd STAGE1_INITRAMFS_ON_BOOT_FS \
  --dtb DTB_ON_BOOT_FS \
  --expected-crc32 booti=8hex,dtb=8hex,initrd=8hex \
  --bootargs "console=tty0 console=ttyS0 loglevel=off init=/init -- --root-init=systemd --debug-console=root" \
  --firmware-framebuffer \
  --final-profile debug-root-console \
  --milestone-timeout 120 \
  --yes \
  --log /absolute/path/to/megrez-debug-root-handoff.serial.log
picocom --baud 115200 "$SERIAL"
```

`ASTERINAS_DEBUG_CONSOLE_READY uid=0` identifies the Asterinas root console;
it requires no username or password. The `debian` / `debian` credentials below
belong only to the unrelated RockOS recovery system and never authenticate to
Asterinas.

This profile requires exactly one `loglevel=off`. On Megrez, asynchronous
kernel diagnostics and the shell share the physical UART; sustained block
errors can otherwise splice bytes into a framed shell response. Suppressing
kernel logging is limited to this fixed-command acceptance and operator
handoff profile. Use a separate diagnostic boot when kernel logs are the
evidence under investigation.

## Generic U-Boot `booti`

Build the deterministic marker initramfs, then provide it with a RISC-V Linux Image.

```bash
python3 tools/riscv/make_qemu_uboot_initramfs.py \
  target/qemu-uboot/marker-initramfs.cpio.gz
make test_riscv_uboot_booti \
  ASTERINAS_RISCV_BOOTI="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  ASTERINAS_INITRAMFS="$PWD/target/qemu-uboot/marker-initramfs.cpio.gz"
```

Generated U-Boot, DTB, disk, logs, and JSON evidence stay below `target/`.

## SiFive U

Use the same Asterinas artifacts to validate the SiFive UART path through U-Boot and userspace.

**The kernel must be built in Sv39 mode** (`FEATURES=riscv_sv39_mode`): the
QEMU `sifive_u` machine models only Sv39-capable harts (see the contract's
`mmu_types`), and a default Sv48 image page-faults on the early DTB read.
This is the single most common cause of a failed sifive_u run.

```bash
make kernel TARGET_ARCH=riscv64 FEATURES=riscv_sv39_mode
python3 tools/riscv/make_qemu_uboot_initramfs.py target/qemu-uboot/marker-initramfs.cpio.gz
make test_riscv_sifive_u \
  ASTERINAS_RISCV_BOOTI="$PWD/target/osdk/aster-kernel-osdk-bin.Image" \
  ASTERINAS_INITRAMFS="$PWD/target/qemu-uboot/marker-initramfs.cpio.gz"
```

An optional Linux control run uses the same machine and evidence path.

```bash
make test_riscv_sifive_u_linux_reference \
  RISCV_LINUX_IMAGE=/absolute/path/to/Image \
  RISCV_LINUX_INITRAMFS=/absolute/path/to/initramfs
```

The Asterinas run is accepted only after its userspace marker appears.
Firmware output alone is not sufficient.

## Dependencies

Use the repository development container.
Preparing U-Boot additionally needs the RISC-V cross compiler, `dtc`, OpenSSL/GnuTLS development packages, and the Python development headers and `setuptools` used to build `pylibfdt`.
The unit tests use only the Python standard library and repository files.

### Host-side Megrez debugging

#### RockOS recovery login

The public factory-default credentials for the Milk-V Megrez RockOS/Debian
maintenance system are:

```text
username: debian
password: debian
```

A serial session on 2026-07-16 confirmed that this pair reaches
`debian@rockos-eswin`. It applies only to the board's RockOS recovery system,
not to the Debian root filesystem running on Asterinas. In particular, it
cannot unlock an Asterinas account whose shadow entry is `!` or `*`.
If an operator replaces the factory password, do not record the replacement in
the repository, shell history, or controller logs.

Keep builds and QEMU runs in the pinned development container.
On the host, install only the tools that observe the physical serial and Ethernet paths:

```bash
sudo apt-get install -y ethtool iperf3 arping
```

`ethtool` reports the host-side link state, negotiated speed, and error counters.
`arping` separates layer-2/ARP reachability from DNS, TCP, and the browser.
`iperf3` provides a controlled throughput test when a matching guest endpoint has explicitly been installed;
it is not a substitute for the HTTPS browser gate.

Packet decoding and screenshot OCR are optional:

```bash
sudo apt-get install -y tshark tesseract-ocr tesseract-ocr-chi-sim
```

The repository workflow already uses `picocom`/Python for serial I/O,
`tcpdump` for packet capture,
`dtc`/`fdtget` for device trees,
`gdb-multiarch` for debugging,
and e2fsprogs for Debian root images.
Do not install a second host QEMU merely for this workflow:
use the pinned container so that the emulator and cross-toolchain versions remain reproducible.

Verify the host once before a physical session:

```bash
for tool in docker picocom socat tcpdump ethtool iperf3 arping; do
  command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done
id -nG | tr ' ' '\n' | grep -qx dialout
test -r /dev/ttyUSB0 && test -w /dev/ttyUSB0
MEGREZ_HOST_IFACE=${MEGREZ_HOST_IFACE:-enp12s0}
ip -br link show "$MEGREZ_HOST_IFACE"
ethtool "$MEGREZ_HOST_IFACE"
```

The `dialout` check avoids running the serial gate as root.
`ttyUSB0` is the currently validated path;
resolve the actual USB-serial node again after unplugging or re-enumerating the adapter.
Broad passwordless `sudo` access is not required.

Profiles are reviewed code objects; command-line CPU, memory, bootarg, and resource-gate overrides are intentionally restricted.
Add a new machine by defining its contract and tests instead of adding board-specific branches to the runner.
