# RISC-V browser interaction baseline, 2026-09-16

The requested greater-than-fivefold improvement in everyday Firefox use is **not yet verified**.
The observations below separate browser scheduling, the QEMU emulator, and the physical-board qualification boundary.
They must not be combined into one speedup ratio.

## RockOS board availability and lightweight probe

The Megrez board is reachable over SSH and remains in RockOS Linux 6.6.87;
the Asterinas ext2 partition is unmounted, the existing Xorg process is running,
and no Firefox or QEMU guest process was left after the checks.
RockOS has `qemu-system-riscv64` but no `/dev/kvm`, so its guest runs in TCG rather than hardware virtualization.
Its `/boot` filesystem has only about 3.4 MiB free; no boot files or selector were changed.

Four fresh diskless, networkless dual-host gates used identical release-kernel bytes
`0c2da50816abea7d6be6aefc092acccd31b64f2421aceeed8047f2a5b97f956b`
and Stage1 bytes `2a1da2995176a935be89ea4dccf5cb1a4f6db3cf800a2dcc37b6d8f5f02caed3`.
Both the developer QEMU and RockOS TCG passed `boot,syscall213` in every gate.
RockOS guest elapsed times were 2.875, 3.155, 2.950, and 2.847 seconds;
the independent result JSON files are under the dual-host worktree's
`target-ubuntu/dual-host-qemu-probe/runs/20260915T1653*` and `20260915T165400-0a9a68bf`.
Older 142–191-second RockOS probe records used a different, larger development kernel,
so the contrast supports using the release profile for quick kernel probes but is not a controlled browser speedup.
QEMU `virt` does not test the Megrez's real MMC, USB, framebuffer, HDMI, or Firefox desktop.

## Repeated Asterinas QEMU keyboard result

Two three-cycle QEMU interaction gates passed with byte-identical inputs:
release kernel `0c2da50816abea7d6be6aefc092acccd31b64f2421aceeed8047f2a5b97f956b`,
ESR root image `beed8e4022b9fa0be238b9ef99ab73f5a72d32471d0db71ab3b5885108454d2f`,
Stage1 `c84505340e61f940ea70dbbee4807e6f021a589145ac39c93852ba8423ee3577`,
and the same DTB, U-Boot, manifest, package lock, and checksums.
The root's `/usr/share/asterinas/firefox-riscv-jit-overlay.json` is absent,
so these are Firefox ESR fallback samples, not JIT samples.
Each cycle accepted 16 trusted keyboard input events, the pointer/button gate, and distinct framebuffer screenshots.

| Run | Cycle | Keyboard samples | Event-handler-to-second-rAF p50 | p95 |
| --- | ---: | ---: | ---: | ---: |
| Parent | 1 | 16 | 349 ms | 784 ms |
| Parent | 2 | 16 | 219 ms | 371 ms |
| Parent | 3 | 16 | 189 ms | 321 ms |
| Repeat | 1 | 16 | 411 ms | 880 ms |
| Repeat | 2 | 16 | 208 ms | 406 ms |
| Repeat | 3 | 16 | 232 ms | 325 ms |

The accepted design's 100 ms p95 target is not met even with the release kernel.
The timer starts in Firefox's trusted input handler and ends at the second `requestAnimationFrame` callback.
It does **not** include USB-to-handler delay, physical framebuffer transfer, or HDMI scanout.
QEMU uses RISC-V TCG with `bochs-display`, four virtual harts, and a 1280×1024 test screen;
these numbers are not physical-board Firefox latencies.
Raw local results remain under the Firefox validation worktree's
`target/physical-firefox-validation/qemu-input-identity-v4-{parent,repeat}/run/result.json`.

## Same-board Linux browser control

RockOS stayed online on the Megrez board throughout this test.
Its Firefox 131.0.2 used a disposable, independent headless profile and the same fixed local performance fixture.
The existing Xorg desktop and boot menu were untouched.
Eight synthetic browser events per kind produced these event-handler-to-second-rAF p95 results:
keyboard 67 ms, pointer 66 ms, and scroll 34 ms.
The browser snapshot was `startTime=0`, `fetchStart=-1`, `responseStart=24`,
`responseEnd=24`, `domContentLoadedEventEnd=51`, and `loadEventEnd=51` milliseconds.
The existing strict contract correctly rejected the negative `fetchStart`;
no value was clamped and no navigation result was admitted.

This supports the conclusion that negative Firefox `fetchStart` is not unique to Asterinas,
but it does not prove Asterinas timekeeping is perfect:
the browser versions differ, the Linux input was synthetic,
and the Linux run was headless while Asterinas used a QEMU display.
The Linux rAF numbers are a hardware/browser reference, not a valid fivefold A/B comparison.
RockOS Xorg uses KMS `modesetting` at 2560×1600,
but its log says glamor refused softpipe and initialization failed;
it is not a known hardware-accelerated Firefox control.

A second same-board headless control varied only `MOZ_LOG` across two disposable Firefox profiles.
With 16 synthetic samples, keyboard second-rAF p95 was 66 ms without detailed logging
and 67 ms with `timestamp,Widget:2,Marionette:2`.
The verbose trial's Mozilla log files were all zero bytes for this simple local workload.
This does not establish the logging cost of a physical Desktop or a complex webpage,
but it provides no evidence that disabling these categories would fix the observed keyboard delay.
The corresponding temporary Firefox processes, profiles, fixture server, and SSH tunnel were removed after the control.

## JIT candidate and test-harness limit

The existing fixed JIT root image `179403de54e757093e4135a228d33e075a20fb8dea6b24b5687f0bcfc45ea505`
was locally reused without a package download or rootfs rebuild.
Its marker file is present.
An exploratory same-kernel QEMU interaction gate did not return a result within the 900-second outer limit.
`timeout -k 10` killed the parent with exit 137 before it could publish serial evidence or finish cleanup.
The exact orphan QEMU PID was then verified by its unique temporary boot path,
terminated, and confirmed absent.
Only the generated 2 GiB run-disk copy and 64 MiB boot disk were removed;
the immutable cached JIT input remains available.
There is no qualifying JIT-versus-ESR latency result from this run.

The next information-rich experiment needs a single cumulative deadline with enough time for signal cleanup and live phase evidence.
The Firefox validation worktree now has a TDD-tested QEMU serial observer that emits only fixed stage names,
handles split serial chunks, suppresses duplicates, and never echoes the four-digit interaction code.
Its 23 targeted host tests and Ruff lint pass;
it has not yet been qualified by a fresh full graphical QEMU run.
It should collect Firefox/Xorg CPU windows during the trusted input cycles,
then compare ESR and JIT using frozen base manifests.
On physical Megrez, the same workload must qualify the release desktop image and measure keyboard, pointer, local navigation, and display-provider costs in one guarded boot.
The board currently remains in RockOS, and partition 2 passed read-only `e2fsck -fn` after repair;
do not switch to another writable desktop run while its earlier communication stall and emergency-reboot safety are unresolved.

## Board-hosted graphical QEMU and Firefox startup CPU

The RockOS board also ran a separate **graphical** Asterinas QEMU `virt` guest with the
same frozen release kernel, but a JIT-derived Debian root image
`98f8c62f5bea1be09494f72e23486d73f718d50e2d2ee45fe0d33ef8839f8ba4` and Stage1
`764a18a2665dfe76a45b983bc86ac58751ecb257141326668787f4a036ed0d13`.
The root contains `/usr/share/asterinas/firefox-riscv-jit-overlay.json` and an additional
`asterinas-dev-overlay` manifest entry; it is **not** the same frozen-base image as the
ESR interaction gates above or the cached JIT base `179403de...`. No JIT/ESR speedup
can be calculated from these inputs. Uploaded images were SHA-256-verified in a
board-side cache, and guest writes went to a thin qcow2 overlay. The real board boot
selector, `/boot` contents, and Asterinas partition 2 were untouched.

Two setup attempts reached the Debian console but did not expose `/dev/fb0`.
The kernel logged `framebuffer: Framebuffer not found`. Source inspection showed why:
the QEMU `bochs-display` framebuffer is registered only when U-Boot's `pci bar` and
`fdt mknode`/`fdt set` sequence inserts a `simple-framebuffer` node into the DTB.
Merely switching from direct `-kernel` boot to U-Boot is insufficient. Reusing the
existing desktop gate's DTB fixup created `/framebuffer@40000000`; the next guest
reported `/dev/fb0`, two input nodes, an Xorg socket, Firefox service active with
no restart, and Marionette listening on port 2828. A local fixture page returned
HTTP 200. This establishes **setup viability under RockOS TCG**, not a real-panel
or trusted-input qualification.

Ten consecutive 0.5-second guest `/proc` CPU intervals after browser startup covered
5,293.966 ms. Firefox PID 77 accumulated 3,870 ms user CPU and 1,930 ms kernel CPU;
Xorg PID 72 accumulated 0 ms in those sampled intervals. Guest CPU 2 was busy in
all ten intervals (last interval 98.1%). A one-shot `top -H` showed Firefox's main
thread running on CPU 2 while the other listed Firefox threads slept. The complete
raw per-process and per-CPU ledger is in
[`2026-09-16-board-rockos-jit-startup-cpu.json`](2026-09-16-board-rockos-jit-startup-cpu.json).
These data are **startup/idle CPU windows**, not overlapping measured keystrokes or
navigation. They point to a Firefox-side CPU hotspot worth profiling, but they do
not establish whether that hotspot is a Gecko loop, excessive syscalls, a QEMU TCG
artifact, or a physical-board bottleneck.

A short synthetic browser capture did not publish complete navigation or rAF timing
before the outer 600-second QEMU deadline; that attempt is inconclusive. `timeout`
terminated the guest QEMU, and a subsequent SSH check confirmed RockOS 6.6.87
remained online, partition 2 was unmounted, and no QEMU or fixture server was left.
The next minimal experiment should capture Firefox main-thread stacks or PCs **and**
Firefox/Xorg CPU intervals overlapping a trusted input action. No greater-than-fivefold
improvement has yet been implemented or measured.

## Follow-up board boot isolation

A follow-up profiler attempt used a fresh qcow2 run overlay, the same U-Boot
framebuffer fixup, and an explicit virtio-net device with a local-only port forward.
It reached `OSTD initialized. Preparing components.` but did not emit Debian/Stage1
progress during more than four minutes. The uniquely identified QEMU process was
terminated with SIGTERM. **No Gecko profile was collected**, and this attempt is
neither a Firefox performance sample nor evidence of a kernel boot success.

To distinguish the device combination from the full desktop path, two 30-second
direct-kernel Stage1 probe boots used the same cached kernel and Stage1 archive.
One included only virtio-net; the other included the three block devices,
bochs-display, keyboard, tablet, and virtio-net. Both reached
`ASTERINAS_PROBE_READY v=1 pid=1` in their time limits. A third 120-second
**U-Boot** Stage1 probe with the same graphics/network/block combination and
the injected simple-framebuffer node also reached that READY marker; its
`loglevel=info` serial reported framebuffer registration and completion of
Bootstrap, Process, and device-initialization stages. Each bounded probe then
ended by its host `timeout`, not by a guest shutdown request.

This narrows the inconclusive long run to the complete Debian root-init path,
its run overlay, or a non-deterministic timing fault; the existing evidence does
not choose among them. `loglevel=info` also emitted large virtio-block request
logs, so it is unsuitable as a normal interactive-performance setting. A future
full-root diagnostic should use fixed stage markers and a short targeted trace
rather than blanket detailed logging. The board remained on RockOS and the
Asterinas partition stayed unmounted throughout these QEMU-only trials.

## Direct-DTB desktop control and failed Gecko export

A generated, one-time DTB `50f82a33fa71916d6db40e26568b4bcd9fd8674bbb25460531e632570d6f391a`
was made from the frozen `qemu-virt.dtb` with the same U-Boot
`/framebuffer@40000000` simple-framebuffer properties. With this DTB supplied
by QEMU's `-dtb` option, the same release kernel and JIT-derived root reached
Stage1 root discovery, ext2 mount, Debian 13 systemd, the fbdev display provider,
and an Xorg socket within the bounded direct-kernel run. A second direct-DTB
run with the existing `--debug-console=root` Stage1 option reached an opt-in
root serial shell. This confirms that the U-Boot profiler run's four-minute
silence was not an inevitable result of this frozen root or framebuffer DTB.
It does **not** yet prove a deterministic U-Boot-specific kernel defect:
the runs had different fresh qcow2 overlays and startup timing.

The first debug-console Firefox launch returned
`ASTERINAS_FIREFOX_WEB_FAIL reason=invalid-network-profile` because the
browser `ASTERINAS_WEB_NETWORK_MODE` was absent. The regular M5 network
evidence separately failed `megrez-bootarg` because the experiment lacked its
QEMU network-selector bootarg. After setting `ASTERINAS_WEB_NETWORK_MODE=direct`
only in the disposable guest and starting the browser service with its existing
ignore-dependencies mechanism, Firefox became active with `NRestarts=0`.
`/proc/<Firefox PID>/environ` verified that `MOZ_PROFILER_STARTUP=1` and its
sample settings reached the **Firefox process**, not just systemd's manager.

The existing opt-in `browser_web_evidence.sh` sends `SIGUSR2` to export a
Gecko profile. This JIT Firefox run instead exited on signal 12:
`ExecMainCode=2`, `ExecMainStatus=12`, `Result=signal`, and no
`profile_*.json` appeared. Thus that diagnostic path is unsafe for this
Firefox variant and must not be used in a normal browser or performance gate.
Mozilla's [Firefox profiler documentation](https://firefox-source-docs.mozilla.org/networking/http/logging.html#capturing-a-profile-with-logs-at-startup)
documents `MOZ_PROFILER_SHUTDOWN=/path/profile.json` with a normal browser exit.
A restarted guest Firefox did receive that shutdown variable, but the bounded
QEMU session ended before an in-app shutdown and no profile file was produced.
The next experiment must request a normal Firefox quit through Marionette
after a short local workload and verify the actual output file before reading
it as profiler evidence. Merely seeing the environment variable is insufficient.

A separate no-Firefox control established that Python's
`http.server` responded with HTTP 404 when bound to guest loopback
`127.0.0.1:19002`; the file was intentionally absent. The same server bound
to the guest's `10.0.2.15:19001` had a live socket fd but both guest self-connect
and RockOS's QEMU host forward failed. The kernel currently lacks
`/proc/net/tcp`, so this does not yet identify whether the failure lies in
local-address routing, inbound virtio-net delivery, or TCP listener matching.
It does mean the guest-to-board **outbound** transfer path should be preferred
for diagnostic artifacts instead of assuming QEMU `hostfwd` works. No profile
was uploaded or silently substituted with another capture type.

Operational note: sending Ctrl-C through the RockOS SSH PTY terminated the
foreground **host QEMU** with SIGINT; it was not a guest-shell interrupt.
Future runs must use a validated QEMU PID for host termination or a guest
shutdown command while the shell responds. All test roots used throwaway
qcow2 overlays over the immutable raw image; board `/boot`, selector, and
partition 2 were not modified.

## Live-PC sampling and page-table lock stall

An additional direct-DTB desktop QEMU run used a fresh `guest-livepc-run.qcow2`
overlay and a QEMU GDB stub bound only to RockOS loopback. The host connected
through an SSH local-forward; `qemu_live_pc_sampler.sh` detached after every
sample. Firefox PID 103 was active with `NRestarts=0`. Its `/proc/103/task/103`
was running on guest CPU 3 when inspected, while Xorg had reached its socket.
The [twelve raw samples](2026-09-16-board-rockos-qemu-livepc/metadata.txt)
were all connected. On CPU 3, eleven PCs were user addresses: three inside
`/usr/lib/firefox/firefox`, three inside `libxul.so`, two inside GTK 3, two
inside GLib 2, and one inside libc. The twelfth PC was in the kernel. The
Firefox mappings were identified from `/proc/103/maps`; this was a startup/
idle window, **not** a measured keystroke or navigation. The frozen root's
`libxul.so` is stripped, so `addr2line` cannot identify its internal hot
functions; its nearest exported `XRE_GetBootstrap` label must not be mistaken
for the sampled function.

After a later guest-shell query, further serial input stopped echoing. RockOS
and the QEMU GDB stub remained responsive. [Three follow-up samples](2026-09-16-board-rockos-qemu-livepc-followup/metadata.txt)
showed CPU 3 at `0xffffffff8041111e` on all three samples and CPU 2 at the
same address on two of three; both had return address `0xffffffff80412824`.
The ELF paired with the frozen Image SHA
`0c2da50816abea7d6be6aefc092acccd31b64f2421aceeed8047f2a5b97f956b`
resolves the return address to `VmMapping::handle_single_page_fault` calling
`Cursor<UserPtConfig>::new`. RISC-V disassembly at `0xffffffff8041111e`
shows `lr.w.aq` inside the page-table-node lock's wait loop, not a generic
idle instruction. The two waiting CPUs therefore establish a sustained
**page-table lock contention/stall** during the console loss. They do not
identify the lock owner or prove a permanent deadlock, and GDB sampling itself
or the SSH PTY may still have influenced the timing. The normal 390-second
outer `timeout` sent SIGTERM to that guest QEMU. A read-only RockOS check
afterward found Linux 6.6.87 still online, partition 2 unmounted, no guest
QEMU running, and no remaining GDB listener.

Source inspection shows that `VmMapping::handle_single_page_fault` acquires a
one-page `VmSpace::cursor_mut` under `disable_preempt()`, while the cursor
implementation locks page-table nodes across its claimed range and releases
them on drop. The observed wait is consistent with either high page-fault
contention, an unusually long cursor owner, or a missing/blocked unlock;
the current samples cannot select one. The next minimal reproduction should
capture the holder CPU's stack/registers and page-table-lock identity while
the wait is in progress, then compare against a no-Firefox control with the
same GDB and PTY setup. No lock-protocol change or fivefold improvement is
claimed from this diagnostic run.

That first **no-Firefox control** has now run on another fresh qcow2 overlay,
with the same direct DTB, desktop devices, SSH PTY, and GDB tunnel. Its guest
root shell returned `CONTROL_PRE_GDB_OK`, then the same twelve-sample sampler
connected and detached on every sample. The shell immediately returned
`CONTROL_POST_GDB_OK` and later `CONTROL_DELAY_OK` after a guest ten-second
sleep. `ps -C firefox` was empty. The raw
[control PC samples](2026-09-16-board-rockos-qemu-livepc-control/metadata.txt)
showed neither CPU 2 nor CPU 3 at `0xffffffff8041111e` in that window. The
180-second outer timeout then ended the QEMU run normally. This rules out a
*deterministic* GDB/PTY-induced console stall under the control conditions,
but single-run and different-workload evidence does not prove Firefox causes
the observed page-table stall. A Firefox run **without prior GDB sampling**
and lock-owner capture at the first stalled console remains necessary.

The second **Firefox, no-early-GDB control** used the same frozen kernel, DTB,
JIT-derived root, and disposable qcow2 setup, with a GDB listener configured
but no GDB connection at any point. Firefox PID 102 remained `active` with
`NRestarts=0`; the root shell returned markers after roughly 20, 50, and
95 guest-second delays, an additional marker near the 240-second wall-clock
point, and a final marker immediately before the 320-second outer timeout.
It also completed the same Python `/proc/<Firefox PID>/maps` query that had
preceded the first run's serial non-response. Its main Firefox thread was
`Rsl` and had accumulated 51 seconds CPU at the 50-second guest check, so
normal shell responsiveness did not depend on Firefox being idle. This single
control shows that **Firefox startup and the maps query alone do not
deterministically reproduce the page-table stall**. The remaining hypotheses
include a rare workload/timing race and a pause/resume-sensitive lock problem;
neither is proven. The correct follow-up is a repeatable, focused page-fault
contention reproducer with opt-in lock wait/owner observability, rather than
an untested change to the RCU or page-table lock protocol. After the timeout,
RockOS again reported no QEMU process, no GDB listener, and unmounted
partition 2.

On the separate RockOS Linux control, Firefox 131.0.2 exited normally via
Marionette (`forced=false`, `in_app=true`) with `MOZ_PROFILER_STARTUP` and
`MOZ_PROFILER_SHUTDOWN` in its environment, yet no shutdown profile appeared.
Its privileged Marionette context returned `typeof Services.profiler ==
"undefined"`. This proves only that the **RockOS Firefox 131 build** does not
expose that service, not that the Asterinas Debian Firefox 143 build lacks it.
The guest Firefox 143 `SIGUSR2` termination above is a separate, confirmed
unsafe export path. These limitations are why the live-PC evidence was used
instead of inferring that an absent Gecko JSON file was a valid profile.

## Opt-in syscall and VM attribution on RockOS QEMU

A new disposable QEMU desktop run enabled only the frozen kernel's
`asterinas.syscall_profile=1` diagnostic switch and manually started Firefox
with the existing ignore-dependencies service path. Firefox PID 102 remained
active without restarts. One `/proc/102/stat` reading gave `utime=3791` and
`stime=3750` centiseconds: substantial kernel CPU accrued as well as user CPU.
At its per-PID syscall snapshot after 8192 tracked events, Firefox had
`openat=614/624`, `read=712/477`, and `mprotect=870/671` where each pair is
count / **elapsed milliseconds** (OSTD `TIMER_FREQ=1000`). Other regular
file/mapping calls were similarly much smaller than tens of CPU-seconds.
Large `ppoll`, `futex`, and `epoll_pwait` elapsed sums include legitimate
blocked sleep, so they must **not** be added to CPU usage or blamed as
compute bottlenecks. The profiler itself adds atomic bookkeeping and verbose
serial records; this run cannot provide an unperturbed Firefox speed ratio.
It does establish that tracked ordinary syscall elapsed time alone does not
explain the process's kernel CPU observation.

A separate fresh-overlay run enabled only `asterinas.vm_profile=1`.
At global fault count 40960, Xorg PID 82 had 4192 faults and 840 ms total
fault-handler elapsed time. At count 49152 it had 4984 faults but 59097 ms;
at count 57344 it had 6705 faults and 123046 ms. Firefox PID 102 had 7327
faults and 3999 ms at that last marker. During the Xorg jump, its proc CPU
fields reached `utime=216`, `stime=9038` centiseconds and it was observed
runnable. This is evidence of a **long-tail Xorg/VM-path stall**, not a
per-fault mean or proof that every Xorg fault is CPU-bound. The VM ledger
includes page-table lock acquisition, VMO page preparation, and execution
cache synchronization, as well as time a task may spend waiting.

That Xorg tail was not stable. A second same-input VM-profile run, this time
also leaving a loopback GDB listener for later sampling, reported Xorg PID 82
at 6895 faults / 1089 ms and Firefox PID 103 at 8834 faults / 5117 ms at
global count 57344. A pre-GDB proc reading gave Firefox `utime=3374`,
`stime=1295` centiseconds and Xorg `utime=227`, `stime=311` centiseconds.
The difference between the two runs is much larger than a plausible routine
mean; any claimed fixed VM bottleneck or fivefold gain needs a repeatable
workload and distribution, not one tail event.

After eight [symbolized GDB samples](2026-09-16-board-rockos-vm-gdb-livepc/metadata.txt)
in the second run, the root serial shell stopped echoing again. Three
[follow-up samples](2026-09-16-board-rockos-vm-gdb-followup/metadata.txt)
repeated an explicit wait chain across all three samples: CPU 0 and CPU 1 at
`0xffffffff8041111e`, the atomic page-table-node lock wait inside
`Cursor<UserPtConfig>::new`, with caller return address
`VmMapping::handle_single_page_fault+164`; CPU 2 at
`0xffffffff802f3b8e`, the spin wait inside
`TlbFlusher<DisabledPreemptGuard>::sync_tlb_flush`; CPU 3 was in the AP
idle loop. These PCs show concurrent **page-table-lock wait and remote-TLB
ACK wait** while the guest console was unresponsive. They do not yet reveal
the TLB target CPU, its interrupt-enable/pending CSRs, the exact page-table
node address, or whether one wait is causal to the other. The sync implementation
asserts local IRQs are enabled on the waiting owner; a preemption guard does
not itself disable local IRQs on the lock waiters. The
[RISC-V privileged ISA's WFI rule](https://docs.riscv.org/reference/isa/v20240411/_attachments/riscv-privileged.pdf)
also requires a locally enabled pending interrupt to wake `WFI` despite the
global SIE bit, so an idle hart's disabled global SIE alone is not a justified
fix hypothesis. The VM may have a missed ACK, target-set race, lock-order
cycle, or QEMU-specific interrupt behavior; capturing CSR/ACK/queue state
is required before changing the protocol.

## Focused RISC-V IPI lost-wakeup reproduction and local fix

Two additional clean Firefox/GDB runs with the frozen Image did **not** stall:
the serial shell responded both immediately and after a 15-second guest delay
following sampling. A healthy CSR snapshot had `sie=0x222` on all four harts;
one idle hart had global SIE clear but `sip=0`. The intermittent desktop runs
alone could not establish the failed interrupt or its cause.

The Firefox validation worktree therefore added
`tools/riscv/diagnostics/tlb_shootdown_probe.c`, a diskless static RISC-V
userspace probe that forces four threads through private COW faults, then
keeps the address space active while repeating `mprotect` read-only/read-write
transitions. The **same binary** completed natively on the board's RockOS
Linux 6.6.87: COW wall time 0.024149 s and twelve protection rounds
0.012579 s. These are a native-Linux reference, not an Asterinas/QEMU speed
ratio because QEMU on RockOS runs under TCG without KVM.

The old Asterinas Image SHA `0c2da508...` used exactly that static binary and
a fresh, diskless QEMU `virt` launch. An uninstrumented first run reached
`mprotect round=9` and then made no serial progress while QEMU consumed
approximately 370% host CPU; its 90-second outer timeout returned 124. A
second run with a loopback-only GDB stub stalled **before COW completion**.
At the stop, CPU 0, CPU 1, and CPU 3 were all spinning at
`Cursor<UserPtConfig>::new+738`, with the same page-table lock address
`0xfffffff002925d04` containing byte `0x01`. CPU 2 was spinning in
`TlbFlusher<DisabledPreemptGuard>::sync_tlb_flush+222` on ACK address
`0xffffffc0a02534a0` containing byte `0x00`. The three page-fault waiters
had `sstatus=0x200000022`, `sie=0x222`, and `sip=0`, i.e. their software
interrupt was enabled but **not pending**. A brief CPU 1 timer trap had
`sip=0x20`, not the software interrupt bit. Subsequent GDB backtraces still
showed the same TLB ACK and page-table lock wait; the 100-second outer timeout
ended this QEMU normally. [The raw serial log](2026-09-16-board-rockos-tlb-probe/asterinas-gdb-serial.log)
and symbolized GDB data in this task support a sustained lost-wakeup syndrome.
GDB has not decoded the remote callback queue contents, so the target CPU and
the queue's exact state remain inferred rather than directly observed.

Source tracing found an independent, concrete lost-wakeup race: RISC-V's
software-interrupt handler runs `do_inter_processor_call`, which drains the
per-CPU callback queue, **then** `HwIrqLine::ack` clears the SSIP pending bit.
If another hart enqueues a new callback and sends an IPI after that drain but
before the clear, the new pending bit is erased; an ACK awaited by another
hart can remain false forever. The bounded explicit-state model in
`tools/riscv/tests/test_riscv_ipi_ack_model.py` enumerates all order-preserving
interleavings of one concurrent enqueue/send with one drain/clear: the old
ordering admits a terminal state with a queued callback and no pending IPI;
the pre-clear ordering admits none in this model. This is **not** a proof of
SBI implementation behavior or the entire kernel's concurrency protocol.
The source-order regression tests failed on the old code and pass after the
local change in `ostd/src/arch/riscv/trap/mod.rs` and
`ostd/src/arch/riscv/irq/mod.rs`, which acknowledges SSIP at trap entry and
does not clear it again after callback execution. The
[RISC-V privileged ISA](https://docs.riscv.org/reference/isa/v20240411/_attachments/riscv-privileged.pdf)
defines `sip` as pending interrupt state and the software interrupt as a
locally enabled interrupt; clearing the old pending state before callbacks
allows a concurrently sent IPI to remain pending.

The offline cached `RELEASE=1`, `SMP=4`, generic-Sv39 Docker build completed
in 1m25s and produced fixed Image SHA
`38024cb9bc2c544bf7729de289a282b6dba5d965bae3cd7c3786dee28c7573ff`.
With the same probe binary/initramfs, QEMU CPU count, and boot args, four
independent fixed-Image runs all reached `TLB_PROBE_OK` and exited with code
0. Their COW wall times were 2.296, 2.303, 2.387, and 2.182 s; protection
wall times were 2.556, 2.500, 2.149, and 2.451 s. The
[four raw serial logs](2026-09-16-board-rockos-tlb-probe/) preserve each
run. The improvement demonstrated here is **removal of a reproducible
unbounded stall**, not a fivefold reduction of normal COW cost or Firefox
interaction latency. A physical Asterinas boot was not attempted while no
operator was at the board. After every QEMU timeout/exit, read-only RockOS
checks showed Linux 6.6.87 online, no guest QEMU process, and Asterinas
partition 2 unmounted; the frozen boot cache Image remained unchanged.

A stricter second version kept all four reader threads running until **after**
the final `mprotect` round, using a C11 atomic stop flag rather than a fixed
reader iteration count. Its new static RISC-V ELF SHA is
`e3ec60e992e3dd249dd902ec15391617897766e1682d2f12fed817efadc0bb42`.
The same v2 binary passed natively on RockOS (COW 0.024259 s; protection
0.040524 s), then completed on the fixed Asterinas Image with COW 2.234756 s,
protection 1.989811 s, all twelve rounds, `TLB_PROBE_OK`, and QEMU exit 0.
The **old** Image with the **same v2 binary** made no progress marker at all,
continued consuming roughly 350% host CPU, and hit the bounded 40-second
timeout (exit 124). The [v2 old/fixed serial logs](2026-09-16-board-rockos-tlb-probe/)
preserve this stronger A/B. This establishes that the local IPI-order change
removes the reproducible wait in this QEMU concurrency workload; a finite
run count and the still-unobserved callback queue prevent claiming full
hardware-level proof or a physical Firefox qualification.

## Fixed-Image Firefox startup and Marionette boundary

Two later board-hosted graphical QEMU runs used the fixed Asterinas Image
`38024cb9...`, a fresh qcow2 overlay over the frozen JIT-derived Debian root
`98f8c62f...`, and the same one-time framebuffer DTB. Both reached the Debian
desktop path and an HTTP 200 local performance page. External DNS failed in
the disposable guest, so the Firefox service was started with its existing
`--ignore-dependencies` mechanism; that bypass did not make public navigation
a valid performance measurement. The Firefox and Xorg PIDs were 125 and 97.

In a 10.525-second overlapping startup CPU window, Firefox accumulated 5.890 s
user and 2.070 s kernel CPU; Xorg accumulated 0.010 s user and 0.040 s kernel
CPU. By the second run's 480-second host limit, Firefox's `/proc/125/stat`
reported 213.17 s user plus 162.98 s kernel CPU; Xorg reported 3.09 s user
plus 6.62 s kernel CPU. These are **TCG guest CPU accounting and startup/full-run
totals**, not CPU samples overlapping a qualified trusted-input action, and
they do not establish a physical-board bottleneck. They do show that this
QEMU run's large CPU bill was not primarily charged to Xorg.

The first synthetic capture did not finish before its outer deadline. A second
capture used the existing opt-in Marionette transport records and an opt-in
interaction checkpoint added to `browser_perf_capture.py` by a failing-then-
passing host test. No interaction checkpoint was emitted: the capture stopped
with `TimeoutError` before synthetic rAF samples. The bounded transport record
identified `WebDriver:NewSession` as the request; its send completed, but the
response timed out at `response_header` with zero header bytes. A separate
bare connect/greeting later took about 2.145 s and succeeded. Because the
capture used a cumulative deadline and its greeting had consumed most of it,
this does **not** prove a Firefox event-loop deadlock or a kernel networking
failure. It identifies the next timing boundary to instrument without adding
unbounded kernel logs. The opt-in checkpoint does not alter the strict
navigation-quality contract: negative Firefox `fetchStart` remains rejected
and recorded, as requested.

After the outer timeout, RockOS remained on Linux 6.6.87, QEMU and the fixture
server were absent, and partition 2 was unmounted. The real boot selector,
board `/boot`, and Asterinas partition were not changed.

## Same RockOS-hosted QEMU Linux control: TLB workload

The first attempted Linux `-kernel` control accidentally selected
`/boot/Imagezrp_riscv64_eswin`, a Rust `rust_shyper` uImage; it panicked on
QEMU's device tree and is **not** a Linux or Asterinas comparison. The actual
RockOS Linux kernel was copied and decompressed from
`/boot/vmlinuz-6.6.87-win2030` into a scoped `/var/tmp` artifact, SHA-256
`19f70042f4e454256ae74b69fbcbe56c627e24fe1afd9bd957ab915f28d66b95`.
It booted as Linux 6.6.87 under the board's QEMU `virt` TCG and passed the
same v2 static probe: COW 1.881240 s, twelve protection rounds 0.199218 s,
`TLB_PROBE_OK`. The expected Linux panic after PID 1 exited with code zero
occurred **after** the probe, not during it. The [valid control serial](2026-09-16-board-rockos-tlb-probe/linux-rockos-kernel-virt-probe.log)
is preserved.

The v2 protection wall timer included per-round serial printing and reader
join, so its approximately tenfold contrast with the earlier fixed Asterinas
v2 run was too coarse to attribute to the syscall. A v3 probe was therefore
rebuilt from the same C source with individual read-only/read-write syscall
durations and separate reader-join timing. Its static ELF SHA is
`8c454a37bd1c29de07d19f43e268763fad27b5ee4e071e32461243cfa0c09b78`;
the new diskless initramfs SHA is
`3762e21ea7ea99ecbfcf804d2c853698cac8e8cb7f73f37352a97900ca38fee9`.
The host regression test failed before instrumentation, then passed; the
cross-build used the already-running cached RISC-V rootfs container without
a download. Both kernels ran this **same v3 ELF** with four QEMU harts, 2 GiB
RAM, no disk, and no network. Asterinas required `init=/init`; Linux required
`rdinit=/init`. The first Asterinas v3 launch used the Linux argument and
failed with `ENOENT` before the probe; it was corrected, not counted as a
performance sample.

| Kernel / run | Concurrent COW | 24 `mprotect` calls | Reader join | Entire protection phase |
| --- | ---: | ---: | ---: | ---: |
| Asterinas fixed 1 | 2.373 s | 0.414 s | 0.010 s | 0.515 s |
| Asterinas fixed 2 | 2.318 s | 0.385 s | 0.003 s | 0.493 s |
| Asterinas fixed 3 | 2.337 s | 0.396 s | 0.015 s | 0.516 s |
| RockOS Linux QEMU 1 | 2.001 s | 0.104 s | 0.004 s | 0.132 s |
| RockOS Linux QEMU 2 | 1.961 s | 0.103 s | 0.004 s | 0.131 s |
| RockOS Linux QEMU 3 | 1.707 s | 0.137 s | 0.005 s | 0.158 s |

The controlled v3 syscall aggregate is about **2.8–4.0 times slower** on
Asterinas than Linux across these runs; the coarse v2 near-tenfold observation
must not be described as a stable syscall ratio. Reader join accounts for
only a few milliseconds. Source inspection shows Asterinas walks the mapping's
page-table entries in `VmMapping::protect`, batches TLB operations at a
32-page threshold, and waits for remote ACKs. Which of the page-table walk,
shootdown, VMAR mapping bookkeeping, or TCG scheduling accounts for the gap
is still unmeasured. The [v3 serial logs](2026-09-16-board-rockos-tlb-probe/)
preserve all rounds and phase times. This is a bounded **kernel microbenchmark**;
it does not show a fivefold Firefox improvement or qualify the physical
Megrez display/input path.

After `clang-format`, the C source compiled warning-clean with
`riscv64-linux-gnu-gcc -Wall -Wextra -Werror -O2 -static -pthread`, but the
binary hash changed (line-numbered diagnostic strings are compiled in). A
final same-ELF check used that current-source ELF
`0cbf31609182c6a6fc87dd77fe4c44089e5964d190b4a8288307f00d332f57de`
and archive `5e68105e30b404f5d30cdb7a8c750e5a1d0dbbc72964b1fb0aea2628d68d6040`.
Asterinas passed with COW 2.136 s and 24-call aggregate 0.334 s; Linux passed
with COW 1.921 s and aggregate 0.114 s. The approximately 2.9-fold syscall
contrast is consistent with the v3 range. Both [current-source serial logs](2026-09-16-board-rockos-tlb-probe/)
contain `TLB_PROBE_OK`; no full-browser or physical speedup is inferred.

Every QEMU run finished or was stopped by its bounded host control. The final
read-only board check found RockOS online, no QEMU process, and partition 2
unmounted. No boot/partition write was used for the control.

## Firefox-specific syscall attribution under RockOS TCG

A fresh graphical QEMU run reused the fixed Asterinas Image, frozen JIT-derived
Debian raw root through a new qcow2 overlay, and the same framebuffer DTB. It
enabled the kernel's existing **opt-in** `asterinas.syscall_profile=1`; the
browser service was started once from the root serial shell with direct mode
and basic-only preferences because the disposable guest's public-DNS gate
failed. The local fixed fixture returned HTTP 200. Firefox MainPID 108 remained
active with `NRestarts=0`; Xorg PID 85 was ready; Marionette eventually logged
that it listened on port 2828.

The [raw serial profile](2026-09-16-board-rockos-firefox-syscall-profiler.log)
contains per-PID snapshots at every 512 tracked calls. Just before the
330-second QEMU outer timeout, the Firefox snapshot reported 2,292 completed
`mprotect` calls with **1,855 elapsed jiffies** in aggregate. OSTD configures
1,000 timer ticks per second, but these are overlapping per-call *wall*
durations, **not** CPU time. An earlier `/proc/108/stat` read in the same run
reported 178.45 s Firefox user plus 116.26 s kernel CPU. Because the profile
is opt-in and the CPU and syscall snapshots are not exactly simultaneous,
neither number is a normal-image latency result. They do rule out treating the
large-mapping `mprotect` microbenchmark as the dominant explanation for this
Firefox run: the entire tracked protection bill was under two seconds, while
Firefox had accumulated hundreds of seconds of CPU.

The per-PID `futex`, `ppoll`, and `epoll_pwait` jiffy totals were far larger,
but are mainly thread-blocking time and can overlap; summing them or calling
them kernel CPU would be wrong. The guest's `/proc/<pid>/stat` `minflt` and
`majflt` fields returned zero because `kernel/src/fs/fs_impls/procfs/pid/task/stat.rs`
currently uses placeholder fault counters, so this run cannot attribute the
large kernel CPU bill to page faults from procfs alone.

An opt-in local timing capture failed before synthetic rAF samples. Its bounded
Marionette transport record showed a valid greeting, then
`WebDriver:NewSession` send completed at guest monotonic 256.860 s and timed
out at 296.343 s while awaiting the **first response-header byte**. No quality
gate was relaxed and no interaction result was admitted. Source inspection
identified a measurement-harness budget problem: the Marionette constructor's
deadline begins before TCP connect/greeting, and the first `NewSession` command
inherited only its remainder. A failing-then-passing host test now gives
`NewSession` its own finite budget after the greeting; a new Stage1-only CPIO
was built offline without rebuilding the kernel or root image. That change
does **not** establish that Firefox will respond within a full budget; a fresh
normal-log QEMU capture and later physical qualification are still required.

The QEMU process ended at its host timeout. The fixture was stopped by its
uniquely checked PID. RockOS remained online, with no residual guest QEMU and
partition 2 unmounted. No boot, selector, or partition write occurred.

## Full-budget Firefox session control

The normal-log control reused that fixed Image, framebuffer DTB, JIT-derived
Debian root, and graphical QEMU devices with another disposable qcow2 overlay.
It booted the offline Stage1-only CPIO containing the tested capture-budget
reset; `asterinas.syscall_profile` was **not** enabled. The
[raw serial log](2026-09-16-board-rockos-firefox-session-budget.log) has SHA-256
`c440c2ddaa568b240def89cd660f41457f9bea8756c18c1b7d600b385c16ee1c`.
Firefox MainPID 108 and Xorg PID 85 were live, the browser service stayed
`active` with `NRestarts=0`, Marionette listened on port 2828, and the local
fixture returned HTTP 200.

The transport received the greeting frame. It began `WebDriver:NewSession` at
guest monotonic 279.516 s, finished sending at 279.584 s, then timed out at
349.519 s waiting for **any response-header byte**. The finite 70-second
deadline had been reset *after* the greeting, so the previous partial-budget
measurement problem is no longer an explanation for this control. The quality
capture correctly emitted `ASTERINAS_BROWSER_PERF_FAIL reason=TimeoutError`;
it obtained no synthetic rAF or navigation result. This does not prove a
Marionette protocol bug or a browser crash: Firefox was still active after the
failure, and the outstanding command's internal progress is not instrumented.

After the timeout, `/proc/108/stat` reported 206.14 s user plus 134.84 s
kernel CPU in this run. `ps -T` assigned 4:14 CPU to the running Firefox main
thread, versus 0:31 to IPDL Background and 0:24 to the software-compositor
thread. These are cumulative readings, not a sampled flame graph, but they
locate the dominant *observed* CPU consumer in Firefox's main thread rather
than the input device or the capture client's wait. The earlier opt-in syscall
profile showed under two seconds aggregate `mprotect` wall time, so changing
only the TLB microbenchmark path is unlikely to yield a fivefold Firefox
interaction gain. The main-thread work and its kernel/user split still need
instruction/stack or Firefox-profiler attribution during a bounded workload;
no fivefold speedup is claimed.

The host's 480-second timeout ended only this QEMU process. A read-only check
found RockOS Linux 6.6.87 online, no QEMU or fixture process, and partition 2
unmounted. The boot selector and physical partitions were untouched.

## Bounded live-PC control during the stalled Firefox command

Another disposable graphical QEMU overlay reused the exact fixed Image
(`38024cb9...`), Stage1 capture CPIO, Debian root, framebuffer DTB, and
normal kernel log level. A first attempt had an SSH PTY on the remote side
but lacked a writable local terminal; it reached the desktop but could not
accept guest commands. Its validated QEMU PID alone was terminated, and no
result from that attempt was used. The corrected attempt retained a writable
local PTY and a QEMU GDB stub bound only to RockOS loopback, forwarded over
SSH. Firefox MainPID 106 stayed active with `NRestarts=0`, Xorg PID 85 was
ready, and the same local fixture returned HTTP 200. The
[raw corrected serial log](2026-09-16-board-rockos-firefox-newsession-pc.log)
has SHA-256
`9ca9a94935c16a3ebedcfe94b1c1e8010c5302bd0f3b86eab9266457965a71d6`.

After a valid greeting, `WebDriver:NewSession` finished sending at guest
monotonic 183.288 s. The [16 GDB samples](2026-09-16-board-rockos-qemu-newsession-pc/metadata.txt)
all connected and detached while the command awaited a reply. Among the 64
four-hart PC readings, 32 were at the kernel's post-`wfi` idle instruction,
14 were in user address space, 16 at other kernel PCs, and two in OpenSBI.
The exact paired non-stripped kernel ELF was verified by its Image SHA before
resolving the other kernel PCs: they included timer/trap work, RCU deferred
frame reclamation, and **one** page-table cursor acquisition. The previously
observed page-table lock wait was not repeatedly present in this window.
Several user addresses lay in the guest's mapped `libxul.so` text, with others
in Firefox or its dependencies; `libxul.so` is stripped, and hart-wide PC
readings do **not** identify a particular Firefox thread. These samples are
diagnostic, not a non-perturbing CPU profile or an interaction-latency result.

The command again returned no response-header byte and timed out at guest
monotonic 253.274 s, almost exactly 70 s after send completion. Immediately
afterward, Firefox remained active and its cumulative process CPU was
139.49 s user plus 101.28 s kernel; `ps -T` assigned 3:05 CPU to the running
Firefox main thread, versus 0:12 to IPDL Background and 0:11 to its software
compositor thread. Those readings are not aligned to just the 70-second
window, so they cannot be divided into a valid per-command utilization rate.
They do support a main-thread-heavy workload rather than an Xorg crash or a
continuously spinning page-table lock as the sole explanation.

Guest `systemctl poweroff` failed because its `login1` unit could not load;
the already-validated host QEMU PID was then terminated with SIGTERM. The
uniquely identified fixture server and SSH GDB forward were also stopped.
RockOS remained online, no QEMU process remained, and partition 2 stayed
unmounted. This control still does **not** meet the interaction quality gate
or establish any Firefox speedup. The next meaningful contrast is the same
Firefox build/root under a Linux guest with the same RockOS TCG host, or a
Firefox-specific call-stack/profile capture that does not use the unsafe
`SIGUSR2` export path; changing a kernel lock solely from these PC counts
would be unjustified.

## Same-TCG Linux guest: headless Firefox boundary, not desktop A/B

RockOS Linux 6.6.87 Image SHA
`19f70042f4e454256ae74b69fbcbe56c627e24fe1afd9bd957ab915f28d66b95`
was booted in the **same board-hosted QEMU RISC-V TCG** with four harts,
2 GiB RAM, the same frozen Debian/JIT root as a fresh qcow2 overlay, and the
same direct DTB and emulated devices. Its `CONFIG_CMDLINE_EXTEND=y` appended
five built-in kernel parameters *after* the test's `--`, which the product
Stage1 correctly rejected. The [first boot log](2026-09-16-board-rockos-linux-firefox-boot.log)
records only `root-init-argument`, not a Firefox result. A tiny
**comparison-only** `/init` now execs the unchanged Stage1 with exactly
`--root-init=systemd --debug-console=root`; its host regression test was red
before implementation and green afterward. The next
[boot log](2026-09-16-board-rockos-linux-firefox-argfilter.log) showed Stage1
entry but a root-discovery timeout despite Linux detecting both `vda` and
`vdb`. A second red/green test verified that the comparison init mounts
`devtmpfs` at `/dev` before the product Stage1; the final diagnostic CPIO SHA
`51d940942bafa88a4db3d56d154af7b7aabd338715e02abd466d0880bc737ffa`
was built offline with the existing persistent Docker container and matched
on board. None of these comparison shims is part of an Asterinas boot image.

With that diagnostic CPIO, the [successful Linux guest boot and browser log](2026-09-16-board-rockos-linux-firefox-devtmpfs.log)
reached ext2 root handoff, Debian 13 systemd, and the opt-in root serial shell.
The guest had **no `/dev/fb0`**, so its desktop service failed; no graphical
input or rendering comparison is valid. The same JIT Firefox **143.0.3**
binary/root was instead started unprivileged with `--headless --marionette`
and the same basic-only direct-mode Firefox profile preferences. Firefox PID
255 remained running. Its first transport connected but received no greeting
header byte within 20 s. A later connection did receive a valid greeting in
11.544 s, then sent `WebDriver:NewSession` completely at guest monotonic
282.155 s and received no response-header byte by 322.145 s: a 40 s timeout.
The Linux guest process had accumulated 247.93 s user plus 76.81 s kernel
CPU at the later snapshot; `ps -T` attributed 3:38 cumulative CPU to its
running main thread. These are process-life totals, not a per-command CPU
profile. The log also contains `RenderCompositorSWGL failed mapping default
framebuffer`, an additional graphics/headless confounder absent from a valid
same-display A/B.

This control shows that a long Firefox 143 Marionette wait can occur on the
same RockOS TCG host **without** the Asterinas kernel. It does not show equal
latency: Asterinas used a graphical desktop and a 70 s command budget,
whereas Linux was headless with no fbdev and a 40 s command budget. The
common TCG/single-thread Firefox cost is now a serious hypothesis; an
Asterinas-specific kernel bill remains possible and requires per-workload
attribution rather than inference from the two failures. All Linux guest
tests used disposable qcow2 overlays, exited at a scoped termination or host
timeout, and left RockOS online, no QEMU/fixture process, and partition 2
unmounted.

Source review also found that `asterinas.vm_profile=1` already counts page
faults and elapsed handler jiffies by PID; adding another generic fault counter
would duplicate existing observability. In earlier VM-profile runs, Firefox's
reported handler bill was about 4–5 s while its procfs kernel CPU was much
larger. One Xorg run had an unstable long tail; a later focused probe
independently reproduced an IPI/ACK lost wakeup, but did not prove that
specific Xorg tail had the same cause. Likewise, the syscall profiler's `futex` and
`ppoll` jiffy sums include blocked sleep and cannot be equated to CPU. The
`/proc/<pid>/stat` CPU fields come from per-thread user/kernel clocks that
pause on schedule-out, and the clock accounting uses RISC-V hardware-counter
deltas. The next narrow instrumentation, if pursued, should read and account
that **active per-thread kernel CPU clock** around only the opt-in syscall
profile's selected calls, without changing the normal dispatch path. This
would directly test whether frequent futex or another syscall class accounts
for the still-unexplained kernel CPU bill; it should not infer a lock fix from
wall-time sums.

## Firefox 143.0.3 `NewSession` startup dependency

The frozen guest binary is Firefox 143.0.3, so the matching Mozilla release tag
is more useful than current `firefox-main` for tracing this command. Its
[`server.sys.mjs`](https://hg.mozilla.org/releases/mozilla-release/raw-file/FIREFOX_143_0_3_RELEASE/remote/marionette/server.sys.mjs)
awaits the driver's command handler before sending the Marionette response.
The tagged
[`driver.sys.mjs`](https://hg.mozilla.org/releases/mozilla-release/raw-file/FIREFOX_143_0_3_RELEASE/remote/marionette/driver.sys.mjs)
shows that ordinary `WebDriver:NewSession` first waits for
`Marionette.browserStartupFinished`, then for the initial application window,
and finally for its initial navigation. The tagged
[`Marionette.sys.mjs`](https://hg.mozilla.org/releases/mozilla-release/raw-file/FIREFOX_143_0_3_RELEASE/remote/components/Marionette.sys.mjs)
resolves that first promise only on `browser-idle-startup-tasks-finished` (or
the mail equivalent). In the tagged
[`browser-init.js`](https://hg.mozilla.org/releases/mozilla-release/raw-file/FIREFOX_143_0_3_RELEASE/browser/base/content/browser-init.js),
the browser notification is the last per-window `requestIdleCallback` after
`SessionStore.promiseAllWindowsRestored` schedules the window idle tasks.

This identifies **where a missing response can wait**, not which dependency
actually held the two 70-second Asterinas controls or the headless Linux
control. A single disposable guest run with `remote.log.level=Trace` can test
whether Marionette receives the idle-startup observer and, if it does,
whether the initial-navigation listener finishes. That run is a diagnostic
contrast only: verbose logging changes scheduling and must not be used as a
performance baseline. If the observer never arrives, inspect window-restore
and the Firefox main-thread CPU profile before changing an Asterinas kernel
lock. The matching release's `remote/shared/Log.sys.mjs` uses
`remote.log.level`, not a `marionette.log.level` preference.

## Same-board Mozilla startup Trace and ESR selector contrast

One more graphical Asterinas guest reused the fixed IPI-ACK kernel Image
(`38024cb9...`), Stage1 CPIO, DTB, boot disk, and frozen Debian/JIT raw root
behind a new qcow2 overlay. In that *disposable guest profile only*,
`remote.log.level=Trace` was set before Firefox launch. The first service
start was rejected by its existing `invalid-network-profile` guard because
the diagnostic guest had not yet set `ASTERINAS_WEB_NETWORK_MODE=direct`;
after setting that and `ASTERINAS_BROWSER_WEB_BASIC_ONLY=1`, Firefox PID 132
was `active` with `NRestarts=0`. The RockOS raw serial artifact is
`/var/tmp/asterinas-marionette-trace-sGjSuE/serial.log`, SHA-256
`44b77cd4c5774c501b60ba2e9b4a772521d8e161fcec87b74e9de10e42b4f5e5`.

The Firefox 143.0.3 listener announced port 2828. The client received its
greeting, completed sending `WebDriver:NewSession` at guest monotonic
246.751 s, and Mozilla's own Trace then logged both the accepted request and
`Waiting for initial application window`. At guest monotonic 316.735 s, the
client still had zero response-header bytes and hit its fresh 70-second
deadline. A query of the Firefox stderr file after that deadline showed only
the `final-ui-startup` observer, **not**
`browser-idle-startup-tasks-finished`; it also showed no initial-navigation
entry. Firefox remained active without a service restart, and its process-life
CPU ticks were 15276 user plus 9863 kernel at that later query. This locates
the stalled command *before* the idle-startup observer in this Trace run. It
does not prove why the observer was late or missing: session restore, the
per-window idle queue, and main-thread starvation remain separate hypotheses.
Trace log volume also means this is not an admissible performance baseline.

A separate normal-log control used the same fixed kernel, Stage1, DTB, QEMU
devices, and raw Debian root with another new qcow2 overlay. Only inside that
overlay, the JIT selector marker was moved to `/run` before Firefox launch;
`/proc/113/exe` verified `/usr/lib/firefox-esr/firefox-esr` and the binary
reported Firefox **140.15.0esr**. The RockOS raw serial artifact is
`/var/tmp/asterinas-firefox-esr-control-cL06lX/serial.log`, SHA-256
`7a3aaae92b390869951d51eb7f64b65839d2ccf56cc95d086d907d8efa426c45`.
The ESR listener also announced port 2828, returned a valid greeting, and
accepted a completed `WebDriver:NewSession` send at guest monotonic 185.993 s.
It likewise returned **no response-header byte** by the fresh 70-second
deadline at 255.961 s. The service remained `active`, `NRestarts=0`, and its
later process-life CPU ticks were 14651 user plus 9143 kernel; `ps -T` showed
the main Firefox ESR thread at 2:50 cumulative CPU. Since the two runs did
not start the command at the same process age and one used Trace while the
other did not, these CPU totals are not a before/after speed ratio. The ESR
result does rule out simply selecting the already-installed ESR binary as a
way to make this same-board TCG `NewSession` complete within 70 seconds.

Both tests used only disposable QEMU root overlays. Each uniquely identified
host QEMU PID received SIGTERM after its bounded result; follow-up SSH checks
found RockOS Linux 6.6.87 online, no remaining guest QEMU, and partition 2
unmounted. The physical browser latency and the requested fivefold
improvement remain unverified. The next evidence boundary is to distinguish
`SessionStore.promiseAllWindowsRestored` from the final per-window idle
callback, while attributing selected syscall **active kernel CPU** separately
from blocked wall time. Neither a kernel-lock rewrite nor an ESR switch is
justified by these control outcomes.

## VNC backend contrast stopped at Stage1 handoff

Firefox 143's tagged `browser-init.js` installs its delayed-startup handler
on `MozAfterPaint` and schedules the final per-window idle callback only
after `SessionStore.promiseAllWindowsRestored`. Because the prior QEMU controls
used `-display none`, a display-backend hypothesis was tested with a new
disposable qcow2 overlay, the same fixed kernel/Stage1/root/DTB and graphical
devices, and VNC bound to RockOS loopback (`127.0.0.1:5921`). The raw RockOS
artifact is `/var/tmp/asterinas-firefox-vnc-control-x58fsn/serial.log`,
SHA-256 `a86d33db0605482b6fa0c37d9079add7dcc4c0f90b649a0e87e26cbd58674cea`.

That run did **not** reach Firefox. Stage1 found `/dev/vdb`, completed
`root-mount`, then reported `DEBIAN_ROOTFS_FAIL reason=dev-bind`. Source review
of `stage1_init.c` shows that `dev-bind` includes both verifying
`/newroot/dev` and `mount("/dev", "/newroot/dev", NULL, MS_BIND, NULL)`;
the existing failure marker records neither which call failed nor its errno.
The normal-display controls with this same kernel/root/Stage1 had completed
that step, but one failed VNC attempt is not evidence that VNC caused a mount
failure. The only valid conclusion for the VNC hypothesis is
**inconclusive**. Its scoped QEMU PID was terminated, and RockOS remained
online with partition 2 unmounted. If this handoff failure recurs, the next
minimal observability addition is an opt-in Stage1 action/errno record before
any filesystem or kernel change; without that, guessing at an ext2 or VNC
fix would be unjustified.

## Existing kernel CPU-clock contract for the next attribution test

The kernel's RISC-V `read_tsc()` reads the hardware `time` CSR and converts
ticks using the firmware timebase frequency. `CpuTimeAccounting` credits that
delta to the current mode, pauses at the pre-schedule hook, resumes on
post-schedule in kernel mode, and switches between user/kernel at the user
execution hooks. `PosixThread::account_cpu_time()` flushes the active interval
into both its **thread** and process `ProfClock`; a thread's
`kernel_clock().read_time()` is an atomic snapshot of already credited
nanoseconds. This explains why a per-syscall CPU measurement must flush at
both entry and exit, then subtract the *current thread's* kernel-clock
snapshots. Reading only the process clock would mix concurrently running
Firefox threads, and reading the clock without flushing would omit its active
tail. A blocked `futex`'s jiffy elapsed time includes the scheduled-out
interval, whereas this active-clock delta should not.

The current opt-in syscall profiler counts 21 selected operations and uses
relaxed atomics plus per-PID snapshots. Its PID slot is claimed once at
`pid % 256`; collision can make one later PID invisible and must be reported,
not silently interpreted as zero CPU. Firefox PID 108 had 77824 tracked
events in the earlier raw profile, including 47060 `futex` calls. Adding two
clock-accounting flushes to every tracked call therefore may itself affect a
slow TCG guest. A short sleeping-`futex` control must prove wall time greatly
exceeds its recorded active CPU, and a profiler-off/profiler-on microbenchmark
must bound diagnostic overhead before any CPU total is used to pick a hot
path. Snapshot atomics are approximate across threads; asynchronous totals
must not be presented as an exact decomposition of `/proc/<pid>/stat`.

The earlier raw profile emitted 152 PID-108 snapshot lines totaling 60793
bytes, and 849 syscall-profile lines totaling 166099 bytes overall. These
counts are from the raw serial artifact, not an estimated UART baud time.
Extending every 512-event text snapshot with CPU columns would increase an
already perturbing synchronous output stream. A CPU attribution run should
retain bounded counters but use a deliberately sparse CPU summary and record
both output volume and profiler-off/on workload timing; otherwise the
measurement could itself create the apparent Firefox bottleneck.

There is a second accounting boundary in the OSTD switch path. The kernel's
`pre_schedule_handler` pauses the outgoing POSIX thread's clock before OSTD's
`before_switching_to` flushes the target stack mapping and may spin on the
target task's `switched_to_cpu` flag. The incoming thread's clock resumes in
`post_schedule_handler`, after OSTD has already performed previous-task
cleanup. Those between-hook CPU cycles are not charged to a POSIX thread's
`stime`; a syscall clock-delta probe would not reveal them. Conversely,
post-schedule VM activation performed *after* resume can be charged to the
incoming thread. The proposed metric must therefore be named **charged
per-thread kernel CPU**, not all CPU executed inside the kernel. If its
selected-call totals are small while host QEMU CPU remains high, do not infer
that kernel scheduling is cheap: use separate bounded scheduler-path
measurement or hart-PC samples for that gap before choosing a fix. The
existing `clock_ticks.c` regression checks `/proc` and CPU-clock unit
agreement to about 50 ms, but it does not test this off-accounting switch
interval or sleeping-futex exclusion.

## Same-board Gecko startup-profile attempt and visible-window timing

On RockOS, a single bounded Asterinas QEMU control reused the fixed kernel,
Stage1, DTB, boot disk, raw Debian root, four harts, 2 GiB, fbdev/Xorg, and
Firefox 143 selector. Its only writable disk was a fresh qcow2 overlay
(`root-overlay.qcow2` initially 196640 bytes). The raw board transcript is
`/var/tmp/asterinas-gecko-cpu-control-UiVSXa/serial.log`, SHA-256
`ccc9de0cc4781619ff46e95c0b1e3699871b15dc5e859d6ad5e5884965122fa1`.
The guest set direct/basic-only network mode and passed
`MOZ_PROFILER_STARTUP=1`, 65536 entries, 25 ms interval,
`MOZ_PROFILER_STARTUP_FILTERS=GeckoMain`, and
`MOZ_PROFILER_SHUTDOWN=/home/asterinas/asterinas-gecko-control-profile.json`
to the actual Firefox PID 114; `/proc/114/environ` verified all settings.
The intended output directory `/home/asterinas/Downloads` was absent, so the
output was placed in the existing browser-owned `/home/asterinas` **before**
the browser service started. No Firefox service was launched on the failed
directory precheck.

Xorg reached its socket at guest monotonic 74.4 s and the Firefox wrapper
executed at 74.74 s. Marionette announced port 2828 while PID 114 was active,
but at guest 192.30 s X11 had no visible Firefox window. An unmapped 10-by-10
window (`WM_NAME=Firefox`, `WM_CLASS=firefox`, PID 114) existed at guest
257.42 s. A `WM_DELETE_WINDOW` request on that placeholder did **not** quit
Firefox: GTK warned that the GdkWindow was unexpectedly destroyed. It should
not be repeated as an exit strategy. At guest 299.16 s an actual visible
`Mozilla Firefox` navigator window (XID 4194306, PID 114) was observed.
This single run puts the first mapped Firefox window more than 224 s after
wrapper execution under same-board TCG. The Gecko startup profiler was enabled
and the hidden placeholder received a delete request before mapping, so this
is **not** an unperturbed browser baseline, a physical-machine latency
estimate, or a fivefold speed result. Firefox stderr
also recorded a delayed `WaitFlushedEvent::Run` and an SWGL framebuffer-map
failure; those graphics warnings are clues, not a proven root cause.
Mozilla's own [Bug 1693011](https://bugzilla.mozilla.org/show_bug.cgi?id=1693011)
records this SWGL message in headless/software-rendering startup trouble,
but later comments also report the same message while headless Firefox works.
The [Firefox 145 Wayland-specific message fix](https://bugzilla.mozilla.org/show_bug.cgi?id=1973891)
is not an Asterinas X11 fbdev fix. Therefore this single X11 warning cannot
be treated as proof of a compositor defect or used to justify disabling
software WebRender in the current Firefox 143 image. Correlate a graphics
candidate with mapped-window, paint, and compositor CPU evidence first.

The guest sent `Ctrl+Q` to the real visible navigator window. Marionette then
logged `Stopped listening on port 2828`, showing the quit request reached
browser shutdown, but PID 114 remained active at guest 402.44 s with
6:02 cumulative CPU, and the shutdown-profile path did not yet exist. The
host's predeclared 420 s `timeout` then terminated QEMU (exit code 124); no
profile was verified before termination, so this attempt yields **no Gecko
sampling data**. Post-run board checks found RockOS Linux 6.6.87 online, no
QEMU process, and partition 2 unmounted. The overlay grew only to 26.8 MB.
An orderly profile export needs a longer *separately bounded* shutdown window
or a validated in-app export path; this run alone does not justify kernel or
graphics changes.

Mozilla's [HTTP/profiler guide](https://firefox-source-docs.mozilla.org/networking/http/logging.html#capturing-a-profile-with-logs-at-startup)
states that `MOZ_PROFILER_SHUTDOWN` writes the profile **when Firefox exits**.
Its [profiler lifecycle documentation](https://firefox-source-docs.mozilla.org/tools/profiler/code-overview.html)
places that output in profiler shutdown and describes multi-process child
profile gathering before a complete JSON capture. Thus Marionette closing
its listener after `Ctrl+Q` is an intermediate shutdown marker, not proof
that the profile should already be on disk. Mozilla's documented
`Ctrl+Shift+2` capture shortcut launches a viewer tab rather than promising
a local JSON file, so it is not a drop-in file-export replacement here.

The timed-out qcow2 overlay was later inspected without changing it: a new
qcow2 **child** backed by that overlay booted the same Asterinas kernel to its
debug shell, and `/home/asterinas/asterinas-gecko-control-profile.json` was
still absent after reboot. The Firefox service remained inactive in this
inspection guest because the normal `megrez-bootarg` network prerequisite
failed; no second Firefox run was started. This rules out treating a possible
last-seconds-on-disk export as an unverified success. The uniquely matched
inspection QEMU PID was stopped after the check, without accessing the board's
physical partition 2.

## Amdahl check on existing Firefox syscall candidates

The earlier raw syscall-profile artifact's final Firefox PID 108 line reports
`sched_yield=2452/1493` and `mprotect=2292/1855`: count/aggregate elapsed
**internal jiffies**. `ostd::timer::TIMER_FREQ` is 1000 Hz, so those observed
aggregate wall bills are 1.493 s and 1.855 s, respectively. The profiler's
entry is before dispatch and its completion after dispatch, including the
direct scheduling delay of `Thread::yield_now()` when it occurs *inside* the
call. Their combined 3.348 s is far below the earlier same-run Firefox
`/proc/108/stat` kernel CPU snapshot of 116.26 s, and far below the hundreds
of seconds of process CPU observed in the full-budget control. The snapshot
times are not identical and the profiler itself may perturb the workload, so
this is a candidate-screening bound, not an exact CPU decomposition or speed
ratio. An isolated `sched_yield` or `mprotect` fast path cannot plausibly
remove hundreds of seconds solely by shrinking its recorded direct syscall
duration; *indirect* scheduling effects and other wait chains are still open.

The same PID line records `futex=47060/5615069`,
`ppoll=6887/561806`, and `epoll_pwait=782/291507`; these large elapsed
sums include scheduled-out waits across many threads and must never be
equated with active CPU. The next kernel attribution boundary remains active
per-thread kernel-clock time for selected calls, with a sleeping-futex
negative control and instrumentation-overhead check before applying Amdahl's
law to their CPU contribution. No scheduler, TLB, or futex algorithm change
is justified by these wall counters alone.

## Physical interaction-latency evidence coverage

The physical graphics protocol already carries per-cycle input-latency
count, minimum, median, p95, and maximum. The host parser in
`tools/riscv/megrez_physical_graphics.py` rejects malformed latency fields
and places the accepted values in each `InteractionCycleEvidence`; a passing
`PhysicalGraphicsResult` requires every requested cycle and serializes those
cycles into `result.json`. This is an existing measurement path, not a missing
instrumentation feature.

The currently accessible historical `result.json` files under the isolated
Firefox worktree's
`target/current-main-physical-graphics/physical/stage1-helper/` all report
`passed=false` and `cycles=[]`. Their reasons include guest input timeouts,
serial-marker timeouts, and one duplicate-button-down rejection. A search of
that worktree's `target/` result files found no `physical=true,
passed=true` result. Its QEMU browser gate, by contrast, contains three
completed cycles with 16 keyboard samples each and recorded p95 values.
This artifact search is limited to the accessible worktree and does not
invalidate earlier operator-visible desktop/PASS reports or other evidence
locations; it establishes that these files do **not** supply a qualified
physical input-latency distribution for a fivefold A/B comparison.

One diagnostic serial transcript in that worktree did emit a physical
`LATENCY` marker with four trusted key events and p95 24 ms, but its host
result failed operator-display confirmation and the accepted host parser
requires at least 16 key downs for a passing interaction cycle. The guest
marker is an exploratory low-load observation, not a passing physical p95.
Separate historical three-cycle `boot-stability-pass` results in the
physical-graphics worktree confirm Firefox/Xorg/framebuffer readiness and
recovery only; their cycles have no input-latency fields. Neither artifact
fills the matched physical interaction baseline.

The next physical performance run should reuse the existing accepted gate
and record a complete `result.json` with the same frozen browser/rootfs,
cycle definition, and before/after kernel build provenance. Until such a
matched physical pair exists, the QEMU p95 values are a simulator baseline
only, and no physical browser speedup factor should be asserted. On
2026-09-16 the board was checked read-only: RockOS Linux 6.6.87 remained
online, no RISC-V QEMU process was running, and partition 2 was unmounted.

## Current-main mechanism status, not a browser speed claim

At current `main` commit `66ffb684e`, the fair scheduler already honors an
explicit `Yield` with a queued peer (`138cca9d1`), and POSIX clone already
copies the creator's CPU-affinity mask (`01f04a3fc`). The controlled
same-binary QEMU handoff result for the yield change fell from 1194.7852 ms
to 2.0984 ms median for 100 same-CPU round trips. Physical probe boots
qualified the affinity inheritance correction, but the post-yield physical
tail still included outliers and neither mechanism has a matched Firefox
before/after latency result. Reapplying either fix is therefore not the next
optimization step; their microbenchmark ratios must not be used as a Firefox
fivefold result.

By contrast, the RISC-V software-IPI acknowledgement order fix validated in
the isolated Firefox worktree was not present in `main` at `66ffb684e`: the
software-interrupt callback ran before `sip::clear_ssoft()` in the generic
IRQ ACK. The worktree's bounded interleaving and source-order tests pass
four of four; source inspection at `66ffb684e` reported no pre-callback clear
and a post-callback clear. At that commit this was a kernel-stability
integration gap, not yet a demonstrated normal-case Firefox latency
bottleneck. A narrow transfer and fresh old/new probe require their own
design review and verification.

**Addendum 2026-09-20.** That integration gap has since been closed and must
not be read as the current status. `269ee6285` (2026-09-16, "Fix RISC-V IPI
acknowledgement ordering") moved `sip::clear_ssoft()` ahead of the IPI
callback drain in `ostd/src/arch/riscv/trap/mod.rs`, together with the bounded
interleaving model in `tools/riscv/tests/test_riscv_ipi_ack_model.py`; both are
ancestors of `main` at `e96ec6b97`. Every other finding in this record is
unchanged and remains scoped to the artifacts and dates named in its own
sections.

## Historical physical real-page navigation boundary

Three successful September 11 physical Firefox/Baidu evidence bundles from
`target/megrez-desktop/evidence-6b3037dc/` share the exact plan SHA
`9232c6d29b...`, boot-argument SHA `e1e503cecd9...`, staged kernel
`asterinas-da3e516e-26230a23.Image`, Stage1
`asterinas-6b3037dc-949149fc-stage1.cpio`, and one physical boot each.
Each result reports `passed=true`, `reason=baidu-home-ready`, verified TLS,
completed DOM, framebuffer evidence, and zero Firefox service restarts. The
recorded page-JSON SHA-256 values were freshly recomputed and matched their
respective result manifests. Their `PerformanceNavigationTiming` values are
milliseconds relative to the current document navigation:

| Bundle suffix | Document responseEnd (s) | DOMContentLoaded end (s) | Load end (s) | Response→DOMContentLoaded (s) |
| --- | ---: | ---: | ---: | ---: |
| `browse-5e17d6dfbded1e7f` | 3.673 | 96.616 | 135.116 | 92.943 |
| `browse-d87ae5223ab1231e` | 15.501 | 176.062 | 188.500 | 160.561 |
| `browse-1c0e99c019cdc1db` | 15.012 | 137.973 | 182.748 | 122.961 |

The [Navigation Timing specification](https://www.w3.org/TR/navigation-timing-2/)
defines `responseEnd` as the end of the **current document** response, and
`domContentLoadedEventEnd` as the end of the document's DOMContentLoaded event.
Thus the repeated large interval is **after the base HTML response**, not
necessarily after every network request. The saved resource list has 67–69
entries, including 32–34 script entries; its longest recorded resource
durations were 37–49 s. Those entries preserve duration but not their
navigation-relative start/end positions, and cross-origin details can be
restricted. The current evidence cannot apportion this interval among
subresource fetches, JavaScript, layout/paint, Firefox main-thread scheduling,
or Asterinas kernel CPU. It does, however, refute the simplistic diagnosis
that the base document's proxy connection time alone consumed the full
96–176 s to DOM readiness.

The same bundles' serial transcripts preserve each Marionette transport
request. The gate deliberately sets `pageLoadStrategy=none`, so a short
`WebDriver:Navigate` response does not assert that the new document has
committed or loaded. The first `WebDriver:GetTitle` after navigation is a
metadata control request; its send finished promptly, but the first response
header was delayed as follows:

| Bundle suffix | Navigate request total (s) | GetTitle send→first response (s) | Immediate JS ping |
| --- | ---: | ---: | --- |
| `browse-5e17d6dfbded1e7f` | 1.222 | 1.215 | no document |
| `browse-d87ae5223ab1231e` | 1.253 | 23.296 | `about:blank`, uninitialized |
| `browse-1c0e99c019cdc1db` | 6.777 | 14.083 | no document |

The 14–23 s `GetTitle` tails began before the new-page readiness probe saw a
committed Baidu document. They cannot be attributed solely to the later
page's JavaScript sandbox, nor to serial transmission of the tiny metadata
request; they could include navigation commit, network/subresource activity,
browser-main-thread delay, or IPC/guest scheduling. Several later
`ExecuteScript` requests also waited tens of seconds, but those scripts
actively probed the page and are not a cheap control. All three saved
`diagnostics.log` files are zero bytes and have no overlapping process CPU
snapshots, so there is no defensible kernel/user CPU split for these tails.
In the next current-kernel run, capture process CPU and document-commit
timestamps around the same `Navigate`/`GetTitle`/first-document boundary,
not just the final Navigation Timing entry.

These September 11 runs predate the later batched ext2 page-cache and Megrez
SD High Speed kernel changes. They are a historical real-page baseline, **not**
a current-main browser latency measurement or a before/after speed ratio.
The matched physical Firefox cold-start evidence for those later I/O changes
reported one comparable 61.81 s visible-window baseline, a 43.27 s mean
after batching, and a 39.20 s mean after SD High Speed; these are
boot-to-window observations,
not Baidu navigation or input-to-frame measurements. A new controlled
local-text-versus-script/resource-heavy page contrast on the same current
kernel is the efficient next split before optimizing a particular kernel path.

## RockOS board check of the CPU ledger, not an Asterinas qualification

On September 16 the board remained remotely reachable as RockOS Linux
6.6.87. Its native `qemu-system-riscv64` is QEMU 9.2.0, but `/dev/kvm` is
absent, so board-hosted Asterinas guests still use TCG. Partition 2 was
unmounted and no QEMU guest was running. No boot selector, board partition,
or board kernel was changed for this check.

The isolated Firefox worktree's `browser_system_time.py` was streamed to
RockOS Python over SSH and sampled stable PID 1 for two 0.25 s intervals.
The private board-local output at
`/tmp/asterinas-rockos-cpu-ledger-20260916.json` reported schema version 1,
durations 250.8 and 250.9 ms, all four per-CPU rows in each interval, and
mode `0600`. This establishes that the parser and exclusive-output path work
on native RISC-V RockOS procfs; it is **not** a measurement of Asterinas
Firefox or Xorg CPU time.

The older artifact named `qemu-system-time-20260915` contains a passing
graphical debug-console gate, but its result keys and serial transcript do
not include a CPU-ledger invocation or exported CPU JSON. Its `passed=true`
must not be interpreted as a successful Asterinas CPU-sampling gate.

The separate `qemu-time-ledger-20260915` artifact **does** contain an actual
Asterinas QEMU guest sample. The serial console shows Firefox PID 164 and
Xorg PID 112 selected from `/proc`, and the one base64 payload decodes to
the saved `guest-time-ledger.json` with identical SHA-256
`1ec010dd350022ea3b08c8add8441578db39b1a2d6082f0270a00d8fa1aafac2`.
Its four half-second intervals total 2.012 s guest-monotonic wall time;
Firefox accumulates 1.590 s user CPU and 0.590 s kernel CPU, Xorg 0.040 s
total CPU. On CPU 2 the per-interval busy fractions are 1.00, 1.00, 1.00,
and 0.98. Firefox's summed process CPU can exceed wall time because it has
multiple threads on four guest CPUs. This sample was near Firefox's initial
Marionette-listener readiness, **not** during the later `NewSession`, real-page
`GetTitle`, or input-to-frame tails. It qualifies the Asterinas guest
procfs collector but cannot yet assign those tails to kernel versus browser
user CPU.

The remaining experiment is to align a bounded CPU ledger with the exact
browser transport and paint intervals. The existing `browser_perf_capture.py`
starts its CPU sampling thread only *after* `WebDriver:NewSession` and
`GetWindowHandles` succeed, so it structurally omits the same-board QEMU
`NewSession` stall seen in prior runs. That is an evidence-window gap, not
proof that the kernel owns the stall. Preserve a normal-log control and
avoid using the logging-heavy syscall profiler as the initial comparator.

## Bounded Asterinas QEMU CPU sample overlapping `NewSession`

A further one-boot QEMU experiment reused the cached `browser-web` root,
Stage1 SHA `a6883ed21f6c42899a78e0234e3902fdf26179a0b097fe6139a8dcc49e06afa5`,
U-Boot, and four-hart DTB with isolated-worktree kernel Image SHA
`38024cb9bc2c544bf7729de289a282b6dba5d965bae3cd7c3786dee28c7573ff`.
The only root write was a disposable QEMU copy; no physical board or partition
was touched. Firefox PID 152 and Xorg PID 111 were live when the root console
started a 40-interval, 0.5 s CPU ledger just before connecting to Marionette.
The serial transcript under `qemu-newsession-cpu-ledger-20260916` contains
one complete base64 ledger payload; its decoded SHA-256 is
`ecdaa39f397e78bd8b5c679b68e636db87f2b11b16ceaf9e68edadf824b1ae52`.

Guest monotonic markers place the sample start at 28.304 s, the
`WebDriver:NewSession` send completion at 30.004 s, its response completion
at 47.356 s, and the sample end at 48.562 s. The transport wait was
17.353 s; the ledger's 40 intervals total 20.221 s. Over that larger window,
Firefox accumulated 16.370 s user CPU and 7.440 s kernel CPU across its
threads, while Xorg accumulated 0.070 s CPU. CPU 3 was at least 90% busy in
35 of 40 intervals (mean busy fraction 0.964). Aggregate Firefox CPU exceeds
guest wall time because four guest harts can run its threads concurrently.
The sample overlaps but is **not identical to** the transport wait, and
background network checking also overlapped; neither the 7.440 s kernel CPU
nor the saturated core can yet be called the causal `NewSession` bottleneck.

The `NewSession` command returned successfully, but the encompassing
debug-console gate reports `passed=false`, `reason=protocol`: the guest's
background external Baidu HTTPS check failed with curl status 35 before
protocol classification. The QEMU process terminated during normal gate
cleanup, with no residual guest. This makes the run useful for a bounded
CPU/transport overlap but **not** a quality-passing browser baseline or a
performance ratio. Same-host Linux QEMU Firefox controls also showed long
`NewSession` waits under TCG, so do not transfer this result to the physical
board or infer a kernel-only fivefold optimization. The next useful split is
per-thread Firefox main-thread CPU versus other threads, ideally with an
offline fixture and a physical same-image run when an operator is present.

## `NewSession` leader-thread counter split and offline-control failure

Two further QEMU runs used the same cached root/Stage1 and Image SHA as the
overlap run above. Each selected the Firefox parent/leader TID and read both
`/proc/<pid>/stat` (aggregate process clock) and
`/proc/<pid>/task/<pid>/stat` (leader-thread clock) immediately before and
after one Marionette `NewSession`; both commands succeeded and both process
start-time identities remained stable. The shell transcript uses `\r\r\n`
line endings. A one-off host extractor that expected at most one carriage
return therefore failed to publish the first run's JSON despite a complete
guest payload; the decoded serial payload SHA-256 is
`fdb76b3eac5f6412c891c3c107d57dae50324f3bb64c0e95626ca25f581f49a1`.
The second extractor accepted repeated carriage returns and saved a private
`mainthread-newsession.json` (mode `0600`, SHA-256
`593ce83dede3c869d48995b6dfc4664bca140226d23f6c355a2db00b7c788547`).

| QEMU run | Command wall (s) | Firefox process user/kernel CPU (s) | Firefox leader user/kernel CPU (s) | External evidence unit |
| --- | ---: | ---: | ---: | --- |
| `qemu-newsession-mainthread-control-20260916` | 14.939 | 12.300 / 4.900 | 6.940 / 2.480 | running, failed `browser-content` |
| `qemu-offline-newsession-mainthread-20260916` | 15.322 | 14.430 / 4.820 | 7.610 / 2.320 | still running, failed `browser-content` |

Leader CPU was 9.42–9.93 s across the approximately 15 s command. Its
unaccounted remainder cannot be called idle: it may include IPC waits,
descheduling, or kernel time omitted by the current OSTD scheduling-clock
boundary. The process totals include CPU from other concurrent Firefox
threads and exceed wall time. The leader's directly charged kernel time of
2.32–2.48 s does not identify which syscall, page-fault path, or lock owns it;
it also does not provide a fivefold kernel-only speedup candidate.

The second run tried to make an offline control using only boot arguments:
`systemd.mask=asterinas-browser-web-evidence.service` and
`systemd.setenv=ASTERINAS_BROWSER_WEB_BASIC_ONLY=1`. The Firefox `user.js`
in the disposable guest root shows that `BASIC_ONLY` did disable selected
background update/safety/DoH preferences. However the external evidence
service still started and emitted `DEBIAN_BROWSER_WEB_FAIL reason=browser-content`;
the requested unit mask was **not effective in this Asterinas guest**. This is
not an admitted offline A/B. Official
[systemd-debug-generator documentation](https://github.com/systemd/systemd/blob/main/man/systemd-debug-generator.xml)
defines `systemd.mask=` as a runtime boot mask, and a read-only RockOS-host
generator control created the expected `early/<unit> -> /dev/null` symlink.
The boot-environment mechanism was subsequently isolated as described below.
An earlier attempt to stop the already activating unit inside QEMU timed out
after 15 s in one run; in another it
removed the evidence-owned Marionette-ready marker while Firefox itself
continued listening. The correct experiment boundary is therefore an
offline profile/boot contract established **before** the unit starts, with
Firefox's own listener as the readiness source, not repeated `systemctl stop`
or a claim based on a merely present kernel parameter.

## Root PID namespace inode makes systemd misdetect QEMU as a container

A separate bounded QEMU debug-console run used the same frozen root, Stage1,
four-hart DTB, and kernel Image as the `NewSession` counter split above.
It completed with `passed=true`, `reason=pass` before the online evidence unit
could fail. Its serial witness is under
`qemu-nsfs-inode-diagnostic-20260916/nsfs-inode-diagnostic.serial.log`
(SHA-256 `87b63a74800c2d4f9315f137ef8ef4f9e31c2860a5ffa0943a53aa0fb8130c36`).
The guest reported `/proc/self/ns/pid` inode `4`, `/proc/1/ns/pid` inode `4`,
symlink target `pid:[4]`, and `systemd-detect-virt --container` output
`container-other`. None of the tested container markers
`/.dockerenv`, `/run/.containerenv`, `/run/systemd/container`, `/proc/vz`,
or `/proc/bc` existed. A read-only control on the RockOS 6.6.87 board reported
initial PID namespace inode `4026531836` (`0xEFFFFFFC`) and container result
`none`; its KVM device remained absent, and it was not rebooted.
Linux exposes that value as `PID_NS_INIT_INO` in its
[nsfs UAPI header](https://github.com/torvalds/linux/blob/master/include/uapi/linux/nsfs.h).

The preceding one-boot mask diagnostic also proved that `/proc/cmdline` contained
the requested `systemd.mask=`, while PID 1's argv was only `/sbin/init` and
`/run/systemd/generator.early/<unit>` was not a `/dev/null` symlink.
Manually invoking `systemd-debug-generator` with the same mask in
`SYSTEMD_PROC_CMDLINE` *did* create the symlink, so the parser and symlink
implementation work. The [systemd container-detection source](https://github.com/systemd/systemd/blob/main/src/basic/virt.c)
compares the current PID namespace inode with Linux's fixed initial value;
its [command-line parser](https://github.com/systemd/systemd/blob/main/src/basic/proc-cmdline.c)
reads PID 1's argv instead of `/proc/cmdline` when it detects a container.
Asterinas's initial PID namespace currently calls the generic
`StashedDentry::new()` allocator, which gives it inode `4` in this guest.
These independent facts explain the ineffective mask without attributing
the roughly 15-second `NewSession` delay to the kernel.

The narrow candidate is a Linux-compatible initial PID namespace nsfs inode,
with a regression check that child namespace inode allocation remains unique.
It is **not implemented** in the current kernel. A clean offline fixture
comparison would still need a passing QEMU boot proving the evidence unit is
masked before activation, followed by same-image physical-board Firefox timing
when an operator is available. No fivefold browser speedup is claimed.

## QEMU graphical input gate with a low-frequency CPU window

One further graphical QEMU gate used the cached `browser-web` root SHA
`98f8c62f5bea1be09494f72e23486d73f718d50e2d2ee45fe0d33ef8839f8ba4`,
Stage1 SHA `a6883ed21f6c42899a78e0234e3902fdf26179a0b097fe6139a8dcc49e06afa5`,
and isolated-worktree kernel Image SHA
`38024cb9bc2c544bf7729de289a282b6dba5d965bae3cd7c3786dee28c7573ff`.
The guest-side `/proc` sampler was started immediately before the first
trusted QEMU keyboard/pointer cycle. The standard gate completed all three
cycles with `passed=true`, `reason=pass`; there was no physical board write or
reboot. The serial witness SHA-256 is
`5c702f51da72c15e636075729c2f59d6f57f83674d595ece0f280e0fd623d514`,
and the gate result SHA-256 is
`ed8754c078c51ee8b98e1710d329b999b6478a0079d3e0a9b763e4502207a0d5`.
Both remain under `qemu-input-cpu-window-20260916` in the Firefox validation
worktree's generated `target/physical-firefox-validation/` output.

The first host extractor missed the payload because a following shell command
echo appeared between the `BEGIN` marker and the single base64 data line.
The intact data line decoded and passed schema, sample-count, and Firefox/Xorg
PID checks. The private decoded JSON (mode `0600`) has SHA-256
`08e6d74a40c5b48dd273a055bf933a209f527c14a1f9e19fc4426081f01ff01e`.
It records 64 half-second intervals totaling 32.223 s for Firefox PID 403 and
Xorg PID 387. Across that window, Firefox consumed 23.410 s user and 10.220 s
kernel CPU over all its threads, Xorg 0.510 s user and 0.250 s kernel CPU,
and `/proc/stat` reported 47,093 context switches. Guest CPU 0 had mean busy
fraction 0.964 and was at least 90% busy in 56 of 64 intervals; the other
three guest CPUs' mean busy fractions were 0.069, 0.190, and 0.255.

The gate's four trusted key-down samples per cycle had event-handler-to-second-rAF
p95 values 741, 275, and 596 ms. These are small-sample **QEMU** observations,
not physical USB-to-HDMI times; they cannot be directly compared with the
older 16-sample ESR gate because this run used the JIT-derived `98f8...` root
and a different kernel/Stage1 combination. The sampler began before the
cycle's Marionette `NewSession` and finished before that cycle returned; the
existing interval JSON records durations but not absolute guest-monotonic
start/end timestamps or guest phase timestamps. Therefore this run cannot
prove which particular half-second intervals overlap key delivery versus
Marionette setup. Its aggregate data support investigating Firefox and the
kernel charged CPU paths before changing fbdev, but do **not** identify a
fivefold bottleneck or a physical-board speedup. The next timing experiment
must align a small number of guest-monotonic setup/READY/key/pointer markers
with the CPU intervals and keep all other frozen inputs identical.

## Guest-clock-aligned first-cycle QEMU input/CPU experiment

The Firefox validation worktree's Stage1 helpers now emit six separate,
low-volume guest-monotonic phase markers per trusted interaction cycle,
without changing the existing READY/key/pointer/PASS protocol or Firefox
Navigation Timing validation. The CPU interval report now includes its
absolute guest-monotonic start/end values. Two new assertions were observed
failing before implementation; the 60 focused sampler/graphics tests then
passed. The new Stage1 archive SHA-256 is
`d472201d2ca010a6395899402ec7258820906ba3b874bb7643edb5711ea71151`.
Its cached predecessor differed in only three archive entries:
`browser_system_time.py`, `physical-graphics-gate`, and an unrelated
`browser_perf_capture.py` helper. The latter was not invoked by this gate.
The persistent developer container lacked cross-libc headers and static
libraries; they were copied locally, without overwriting existing files,
from the already-running rootfs-builder container with the same GCC 13.3.0.
The Stage1 build then completed without a package download or image rebuild.

The new one-boot `qemu-input-phase-cpu-20260916` gate reused the exact same
kernel Image, root image, DTB, manifest, package lock, and U-Boot as the
preceding input/CPU run; only Stage1 changed. It passed all three trusted
cycles (`physical=false`). Serial SHA-256 is
`d19e3fa0ad37bb60b78d8f4402648a1cdb1b98ab12575839b8f66da035ce934b`,
result SHA-256 is
`74f21ae450937d52ee173aeef3cf4cf0c899901e9f33f6a5a7d68cd869039254`,
and the private 64-interval CPU JSON SHA-256 is
`b221b61643712ae977b8e21793f068c080e82072d4563c0abc513ab6dc97f36a`.
The CPU ledger spans guest monotonic 115.113–163.329 s and totals 48.215 s.
Its Firefox/Xorg process identities remained stable at PIDs 391/386.

| First-cycle guest phase | Wall time | Fully contained 0.75 s CPU intervals | Firefox user/kernel CPU in those full intervals | Xorg total CPU |
| --- | ---: | ---: | ---: | ---: |
| `NewSession` start→done | 13.294 s | 16 (12.065 s covered) | 11.870 / 5.060 s | 0.140 s |
| `NewSession` done→READY | 4.868 s | 6 (4.522 s covered) | 5.890 / 2.430 s | 0.720 s |
| READY→key-ready | 0.749 s | 0 | not isolated | not isolated |
| key-ready→pointer-ready | 0.855 s | 0 | not isolated | not isolated |
| pointer-ready→PASS | 3.116 s | 3 (2.261 s covered) | 2.010 / 0.730 s | 0.050 s |

The two 0.75 s intervals *intersecting* READY→key-ready recorded Firefox
user/kernel CPU of 1.110/0.320 s and 1.080/0.570 s, Xorg CPU of 0.080 s
in each interval, and guest CPU 0/1 busy fractions 0.855/0.986 and
0.684/0.955. They also recorded 2,046 and 1,701 system context switches.
These are neighboring intervals, **not** CPU exclusively attributable to
the 0.749 s key phase: they include preparation and following pointer time.
The fully contained `NewSession` intervals showed guest CPU 1 mean busy 0.921,
while the six post-session setup intervals showed CPU 0/1 mean busy
0.730/0.887. The data distinguish a large browser-heavy setup cost from
Xorg's much smaller charged CPU, but do not yet distinguish Firefox main-thread
work from other Firefox threads or quantify runnable wait.

Four trusted key-downs per cycle yielded QEMU event-handler-to-second-rAF
p95 values 206, 217, and 287 ms. These small-sample values are still above
the 100 ms local target and cannot establish an acceleration ratio: the
preceding run's first-cycle p95 was 741 ms with an older Stage1, and QEMU TCG
timing varies across boots. A matched-input repeat and a short phase-triggered
quarter-second Firefox leader-thread sample are needed before selecting a
kernel or Gecko hot-path fix. Real-board interaction/scanout remains untested
for this candidate image; the board was not switched from RockOS.

A second graphical QEMU gate then repeated the **byte-identical** kernel,
root, Stage1, U-Boot, DTB, and manifest without the additional CPU sampler.
It again passed all three cycles. Four-key event-handler-to-second-rAF
p95 values were 285, 369, and 278 ms, compared with 206, 217, and 287 ms
in the first timed run. Its `NewSession` start→done phase was 13.183 s and
READY→key-ready was 0.548 s. The repeat's result SHA-256 is
`0de8e80ab79e9204ec58393f9eb0b0864836ea2098b0436523376ba511616783`,
serial SHA-256 is
`bc0088ee733e77217ae246585eb4e36ba489e06a434fd471c4009c29059a700e`,
and its generated output is `qemu-input-phase-repeat-20260916`.
This confirms the new phase markers remain compatible with the existing
three-cycle gate, but also shows substantial QEMU timing spread; do not
attribute the first run's lower p95 to the markers or announce a fivefold
gain. Quarter-second, leader-thread-scoped samples overlapping the key phase
and a physical-board run remain needed for causal optimization.

## Discarded leader-thread QEMU probe: stale kernel input

A subsequent one-boot, quarter-second diagnostic sampled both Firefox's
`/proc/<pid>/stat` (whole-process CPU) and
`/proc/<pid>/task/<pid>/stat` (leader-thread CPU) for 65 bounded snapshots.
Its graphical gate completed all three trusted cycles (`physical=false`),
but its input audit found a host-command mistake: it selected the cached
`qemu-inputs/kernel.Image` with SHA-256
`f333402e9102e9094cead70cee6ed59e161fb0d226bff461e1a66cc1fca73607`
instead of the current worktree Image with SHA-256
`38024cb9bc2c544bf7729de289a282b6dba5d965bae3cd7c3786dee28c7573ff`
used in the two qualified timed runs above. The old and current kernels are
**not** interchangeable performance inputs.

The old-image run's first `NewSession` lasted 98.938 s guest-monotonic and
its delayed 18.288 s diagnostic window ended before `NewSession` completed,
so it did not sample READY or the key phase. It also reported approximately
180 seconds of `/proc/stat` CPU time *per vCPU* in an 18.288-second,
four-vCPU guest window; Firefox process CPU deltas exceeded the physical
four-vCPU upper bound. This is an observer-consistency failure for that
cached Image, not evidence that the current kernel or browser was using
impossible CPU time. By contrast, the current-image 0.75 s ledger above
reports roughly 60–76 USER_HZ ticks per vCPU per interval, consistent with
the 100-Hz userspace clock and interval duration. The discarded run's
serial/result SHA-256 identities are
`94960e8b2367407fdd1f3b409c1fb4746dd70e8c8709a36be7c4cad36dff6eb7`
and
`77b564f254cc6219796f92c3c721119f816dca8e4067c0fe39092dff08bea4de`;
its private decoded CPU artifact SHA-256 is
`c13ca42a506d832580d2aabfbd3549af979e0faa11bfefb18c323af6660ba967`.
All timing/CPU values from this discarded run are excluded from acceleration
comparisons. A new diagnostic uses the explicit current worktree Image path.

## Current-image leader-versus-process QEMU diagnostic

The corrected one-boot run selected the explicit worktree kernel Image SHA-256
`38024cb9bc2c544bf7729de289a282b6dba5d965bae3cd7c3786dee28c7573ff`;
its `result.json` also records that identity, the same root image SHA-256
`98f8c62f5bea1be09494f72e23486d73f718d50e2d2ee45fe0d33ef8839f8ba4`,
and the timed Stage1 SHA-256
`d472201d2ca010a6395899402ec7258820906ba3b874bb7643edb5711ea71151`.
The gate completed all three trusted keyboard/tablet cycles (`passed=true`,
`reason=pass`, `physical=false`). Its first-cycle guest-monotonic markers were
`NewSession` start 116.310 s, done 130.743 s, READY 134.773 s, key-ready
135.568 s, pointer-ready 136.122 s, PASS 138.823 s. The bounded private
diagnostic artifact contains 65 ordered quarter-second snapshots of Firefox
process PID 399, its leader thread under `/proc/399/task/399/stat`, and
`/proc/stat`, spanning guest-monotonic 125.330–141.581 s. Decoded artifact
SHA-256 is
`06cb4770e4bcb2ad5a80f8fc3a086cbaace958493c42632cc169ff5e5f9b947c`.
Each of its 64 per-core intervals passed a generous guest-time/USER_HZ upper
bound, unlike the discarded stale-image run.

| First-cycle phase | Guest elapsed | Fully contained 0.25 s intervals | Firefox process user/kernel CPU in full intervals | Firefox leader user/kernel CPU |
| --- | ---: | ---: | ---: | ---: |
| `NewSession` start→done | 14.432 s | 21 | 6.46 / 2.53 s | 3.48 / 1.17 s |
| `NewSession` done→READY | 4.030 s | 15 | 4.40 / 1.46 s | 1.48 / 0.44 s |
| READY→key-ready | 0.796 s | 2 | 0.58 / 0.22 s | 0.17 / 0.12 s |
| key-ready→pointer-ready | 0.553 s | 1 | 0.25 / 0.07 s | 0.10 / 0.04 s |
| pointer-ready→PASS | 2.701 s | 10 | 2.73 / 0.67 s | 1.48 / 0.38 s |

The two fully contained READY→key-ready intervals cover about 0.507 s;
their process-minus-leader charged CPU is about 0.51 s, versus 0.29 s in
the leader. Guest CPU 1 is 100% busy in both intervals, while CPU 0 busy
fractions are 0.280 and 0.192 and CPU 2 fractions are 0.640 and 0.346.
`/proc/stat` reports 2,009 system context switches in the two intervals.
These facts warrant a **thread-name/TID-resolved** diagnostic of the other
Firefox threads and their wait/IPC path, not an assumed main-thread-only
or DRM-only fix. The recorded four-key input p95 values are 427, 346, and
299 ms across the three cycles, but the extra quarter-second sampler is a
diagnostic perturbation and its p95 must not be treated as a normal matched
performance baseline or a fivefold acceleration result. This remains QEMU
TCG evidence, with no current candidate-image physical USB/HDMI run.

For the next information-rich capture, the existing bounded
`firefox_diagnostic_snapshot.py` collector in the isolated validation
worktree now retains each sampled thread's `utime`/`stime` ticks from the
`/proc/<pid>/task/<tid>/stat` read it was already making. Stable identity
comparison still uses only PID, PPID, and start time, so advancing CPU counters
do not falsely look like PID reuse. The collector also records its guest
monotonic start/end and `SC_CLK_TCK`; this permits two snapshots to be compared
without equating host and guest clocks. New tests were observed failing before
each addition, and the focused collector suite then passed. These collector
changes are **source-only** so far; the frozen QEMU root image and physical
board do not yet contain the updated helper. No charged per-syscall CPU
profiler, scheduler migration change, or framebuffer behavior change has been
made on this evidence alone.

The separate CPU-interval tool now rejects a per-core USER_HZ tick rate more
than three times guest-monotonic elapsed time (with a 50-tick floor for short
asynchronous procfs reads). A regression test first demonstrated that the
old implementation accepted a tenfold-invalid per-core interval; it passed
after the fail-closed bound was added. The final 98 focused host tests, Ruff
checks, Python compilation, and tracked diff whitespace check passed. A new
Stage1-only archive containing that bound was built offline in the persistent
developer container, SHA-256
`ba536a0b5e1db615ed7331c94edce5443081ad9ebc3aa56b7a617e4f1a7e82ec`.
The qualified current-image QEMU leader diagnostic above used the **preceding**
Stage1 `d472201d...`; the new archive has not yet been exercised in QEMU or
on the board. The collector's thread-CPU source change is not in either
Stage1 archive and awaits a normal rootfs candidate build/qualification.

## TID/name/affinity QEMU probe: valid setup data, no input overlap

Another bounded one-boot diagnostic selected the same current kernel Image,
root image, and timed Stage1 as the qualified leader run and completed three
trusted graphical cycles (`physical=false`). It recorded 21 ordered TID
snapshots, 65 Firefox thread names, and 65 readable thread affinity sets,
with no collector limitation; the private artifact is 28,132 bytes, SHA-256
`e615ef7f55bf2022904123ab060d9821684af53ac46ca180e9ca23da0ecefae4`.
Serial/result SHA-256 are
`b67a7f05c115103a3ab4513876f4e3b400130a4fa643d1136bb105946b1af921`
and
`4481dc11cffaff0a2b4936ba9479be9785bad8bea8829ef67fa0619d508b7490`.

This diagnostic **does not cover keyboard input**: its guest-monotonic span
131.010–137.023 s was within first-cycle `NewSession`, whereas READY was
157.543 s and key-ready 158.353 s. The long base64-encoded serial launcher
and a fixed guest sleep were not a robust phase trigger, and the extra TID
reads coincided with slower `NewSession`/post-session setup. The four-key
p95 values 437, 528, and 736 ms are perturbed diagnostic values, not a
matched acceleration baseline. No per-key TID CPU ranking is admitted from
this artifact.

The setup-only data still show all 65 sampled Firefox TIDs allowed on CPUs
0–3; none was explicitly pinned to CPU 1 during that window. Over the
sampled `NewSession` segment, the Firefox leader charged 3.22 s user and
1.12 s kernel CPU; `IPDL Background`, `Renderer`, `WRRenderBackend`,
`Socket Thread`, and software-compositor threads were present with much
smaller individual charged CPU. This confirms process/thread CPU and affinity
interfaces are observable, but it does not establish that the same threads
or affinities held during READY→key-ready. The static scheduler has an
unconditional valid-`last_cpu` return in `ClassScheduler::select_cpu`, so
the earlier CPU-1 saturation is a plausible **load-balancing hypothesis**;
it is not yet a measured runnable-wait bottleneck or authorization to change
the scheduler. The next capture should use a short, phase-triggered helper
or a physical one-boot snapshot rather than another fixed-delay inline
program. No unattended RockOS→Asterinas switch was performed.

## READY-triggered Firefox TID capture during trusted QEMU input

The follow-up used the explicit current worktree kernel Image SHA-256
`38024cb9bc2c544bf7729de289a282b6dba5d965bae3cd7c3786dee28c7573ff`,
the same frozen root SHA-256
`98f8c62f5bea1be09494f72e23486d73f718d50e2d2ee45fe0d33ef8839f8ba4`,
and a freshly built offline Stage1-only archive SHA-256
`f8c9c6b2ae1df3644e1ac8f152b6b457a52fe38139d3fc22dda00e2735ed268d`.
The new opt-in guest helper creates `/run/asterinas-key-thread-ready` immediately
before emitting the first cycle's READY marker. A short background guest command
waits for that actual file, then captures twelve bounded quarter-second TID CPU
intervals. The default graphical gate protocol is unchanged without the opt-in.
The focused host suites passed 69 tests; Ruff, Python compilation, and tracked
whitespace checks passed before this QEMU run.

The QEMU gate passed all three trusted keyboard/tablet cycles
(`reason=pass`, `physical=false`). Private artifact SHA-256 identities are
`ae6d10d993f8fb63e780537c314d020632920388a8639fd9f337923ccfcc88cd`
for the 97,977-byte TID report,
`24fe8f33e381f12ab012596f44f82dc270fab5896a944cf0180d6c1dd28bd8d2`
for serial, and
`84f79205c35c547af84214190ddc47f6bd98e55f7b525b6bc2927d40a312d0aa`
for the result. The READY file contained guest-monotonic
131.0818104 s, before the emitted READY time 131.0885774 s. The sampler
covered 131.0842280–134.5718970 s, including the first trusted keyboard
input and the subsequent pointer transition. Firefox PID 395 had 71 sampled
TIDs; all 71 affinity reads reported CPUs 0–3, with no sampler limitation.

First-cycle READY→key-ready lasted 529.197 ms. No quarter-second interval is
**fully contained** within this phase, so the two overlapping intervals cannot
be used as exact per-key charged-CPU totals. The first interval overlaps
321.5 ms of the phase and charged Firefox leader 270 ms (210 user/60 kernel)
and `Renderer` 100 ms (90 user/10 kernel); the second overlaps 207.7 ms
but extends 77.1 ms past key-ready, charging the leader 190 ms
(140 user/50 kernel), `Renderer` 220 ms (190 user/30 kernel), and
`Socket Thread` 60 ms (20 user/40 kernel). The leader's last CPU was 3,
the renderer's 0, and the socket thread's 2 at both sampled endpoints.
During the one fully contained 304.077 ms key-ready→pointer-ready interval,
the leader and renderer each charged 210 ms and the socket thread 130 ms.
This supports a multi-thread browser/IPC investigation; it does **not** show
a single CPU-1 pin or prove scheduler runnable-wait starvation. The four-key
first-cycle p95 was 492 ms under diagnostic perturbation, not a matched
acceleration result. No fivefold improvement or physical qualification is
claimed.

The RockOS board remained remotely reachable on Linux 6.6.87 with low load,
unmounted Asterinas partition 2, and no `/dev/kvm`; native RISC-V QEMU exists
but would use software emulation. No boot selector, partition, or board runtime
was changed. The developer host's root filesystem reached zero available
space after QEMU generated a disposable root-run copy. After verifying the
frozen input image, reports, and no active QEMU process, only the boot ext4
and run ext2 images from this and four prior named QEMU diagnostic directories
were removed. Their JSON, serial logs, screenshots, and thread artifacts were
retained. Free space rose from zero to 5.5 GB; another QEMU run should first
reserve space or direct disposable artifacts to a roomier volume.

RockOS also exposes a `performance` CPU-frequency governor at 1,800,000 kHz
on all four harts, matching the older native-board environment snapshot.
The current Asterinas source tree has no CPU-frequency driver. This is a
plausible *global* performance variable for a future physical comparison,
not evidence that Asterinas booted at a lower clock: the guest's actual
cycle/time ratio has not been measured in this one-boot QEMU experiment.

## Local-performance fixture blocked by the deliberate refusal proxy

A Task-3 QEMU smoke reused the same locked current Image, frozen Debian root,
and Stage1 `f8c9c6b2...`. Its three trusted graphical cycles passed, but the
opt-in local page capture failed after the bounded document wait with
`observed=about:interactive`. No local performance JSON was published; the
separate ten-second CPU artifact was retained, SHA-256
`a64bc568a60abef1a725705f0b625b74249b9cf18eda92e38dd1727122a33106`.
That window charged Firefox PID 400 2.68 s user and 1.73 s kernel CPU,
versus Xorg PID 388 0.29 s user and 0.03 s kernel CPU. It is a failed
navigation window, not a local-page latency measurement. Result/serial
SHA-256 are `87f3f520a161c91996ad4eea946c5f6f50bf10e4c3b7a9762c662986e3b21da5`
and `b18a050d0895f6df2677fe98498fa4972668a285cc0ee466d3cc43302e545ffc`.

Source inspection found that the physical interaction gate deliberately
restarts Firefox with an isolated refusal proxy at `127.0.0.1:9`. The
profile's `network.proxy.no_proxies_on` contains only localhost and that
proxy host, not the QEMU local fixture host `10.0.2.2`. A second same-input
one-boot control completed all three trusted graphical cycles, then issued
two short requests for exactly
`http://10.0.2.2:17894/browser-quality/perf.html`:
`curl --noproxy '*'` returned status 0 / HTTP 200, whereas forcing
`http://127.0.0.1:9` returned status 7 / HTTP 000. Thus the local server
and Asterinas's direct socket path worked; the refusal proxy explains why
this gate cannot be reused *as-is* to measure the non-loopback fixture.
The second control result/serial SHA-256 are
`d7ed6f30e04ed76231e9e0e0bbbc9f4f8ad02fe767e458c022604f552decd882`
and `700379d921ecd88674d2b37e33919d8f7711b9e2652a54ccd7f5e24a10437c3d`.

An attempted **guest-runtime-only** proxy exception through Marionette chrome
context did not succeed: `Marionette:SetContext` returned a command error,
so no preference was changed and no follow-up browser latency was measured.
Mozilla's documented Marionette client implements preference changes through
chrome-context script execution; this Firefox launch does not grant that
context. Do not weaken system-access checks merely to make a timing probe
pass. The next QEMU check should launch the existing browser wrapper in its
already-supported `direct` mode after the trusted interaction gate, recording
the new PID/provenance and keeping the board untouched. Both QEMU controls
removed only their regenerable boot/root-run disks after preserving result,
serial, and CPU artifacts.

## Local fixture loads under the existing direct Firefox mode

A third same-input QEMU control passed the three original trusted
keyboard/tablet cycles, then stopped only its temporary refusal-proxy
Firefox service. The existing `/usr/lib/asterinas/browser-web-firefox`
wrapper launched a new unprivileged JIT-overlay Firefox PID 668 with
`ASTERINAS_WEB_NETWORK_MODE=direct`; its profile was checked for exact
`network.proxy.type=0` and absence of the proxy-exception preference.
This is a **new PID and browser launch inside one QEMU boot**, not a
same-instance performance comparison with the previous three input cycles.
The separate local capture reached both lightweight fixture pages and
produced synthetic event-handler-to-rAF data (four samples per kind):

| Synthetic action | First rAF p95 | Next rAF p95 | Scope |
| --- | ---: | ---: | --- |
| Keyboard | 37 ms | 44 ms | Browser callback only |
| Pointer | 89 ms | 89 ms | Browser callback only |
| Scroll | 41 ms | 54 ms | Browser callback only |

These small QEMU samples are below the design's 100 ms local rAF target,
but are **not physical USB→HDMI latencies**, are not a matched baseline,
and do not meet the requested fivefold before/after criterion. The parallel
20-interval CPU artifact (10.172 s guest monotonic) charged Firefox PID 668
8.22 s user and 4.24 s kernel CPU and Xorg PID 389 1.10 s user and
0.17 s kernel CPU; CPU 2 was at least 95% busy in 19 of 20 intervals.
That larger window includes setup/page work and cannot be divided by a
particular rAF sample. CPU artifact SHA-256 is
`7672685395f5ef041b7956995e2559cb5d32c3cb514f5642bb98d7611fa30778`.

The local navigation quality door remained **rejected**, as previously
agreed: Firefox's raw second-page Navigation Timing contained
`startTime=0`, `fetchStart=-8`, `responseStart=27`, `responseEnd=28`,
`domContentLoadedEventEnd=638`, and `loadEventEnd=640` (browser-local ms).
The diagnostic explicitly reported `fetchStart is out of bounds`; the
sampler did not clamp it to zero or publish a passing local capture JSON.
The independently ordered `responseEnd→DOMContentLoaded` segment is
610 ms, but it is partial evidence, not an admitted total-navigation result.
Result/serial SHA-256 are
`2a72be0cabeb7f427f5e21fa336d8f3959c93005041641fd91be37d0a3fd262f`
and `f4f7540fb2e1dd4a0d439f78e973c3f727bb83a1aab20fd29ade6765876d74fb`.

This control rules out the deliberate refusal proxy as a remaining cause
of *this* loaded local page and suggests Firefox's browser callback latency
under TCG is not obviously the physical-system-wide slowdown. It does not
rule out real-board CPU-frequency, framebuffer/HDMI, USB scheduling, proxy,
or public-page resource tails. The next high-information step is a bounded
physical same-image one-boot provenance/input/page capture with an operator
present; do not change the kernel scheduler from this QEMU-only result.
Only this control's regenerable boot and root-run disks were removed after
verifying the result, serial, CPU artifact, frozen input, and absent QEMU
process; all evidence files above remain available.

## Lightweight-page trusted-input control did not complete

A fourth same-frozen-input QEMU boot, `qemu-lightweight-trusted-20260916`,
again passed all three original trusted keyboard/tablet graphical cycles.
Afterward, the existing direct-mode Firefox wrapper launched a new PID 660;
the profile had `network.proxy.type=0`, and the local lightweight page loaded.
The guest-side Marionette sampler completed four synthetic keyboard,
pointer, and scroll repetitions, focused `#timing-input`, and reported the
`#timing-pointer` DOM rectangle. The host then sent QEMU keyboard/tablet
device input and its bounded completion sentinel. The sampler ended with
`trusted keyboard/pointer samples incomplete` after the 60-second limit.
It did **not** publish a same-page latency comparison. This means neither
the trusted-event sample counts nor the focused X11 window/device-input
route were established; DOM `activeElement` alone is insufficient proof of
the latter. The original graphical gate's pass remains valid, but it applies
to its earlier Firefox page/window, not this newly launched PID.

The fourth result/serial SHA-256 are
`54f54c4e267a4e41461b5bde78ce7e83aaaf4d21daae8fa5fe0741deb5fdc722`
and `d3d986d1f43aeff4d491f4744208f9ec61bbffbcd99503028908f02252b3ec10`.
After verifying these files and the absence of QEMU, only this run's two
regenerable temporary disks were removed; its result, serial log, and three
graphical-cycle screenshots were retained. Any repeat must first establish
OS-window focus and device/event counters without changing the performance
quality protocol or treating a failed control as a speedup baseline.

## Unattended Megrez bare-metal preflight, 2026-09-16

The operator confirmed they are **not at the board**. Read-only SSH and host
checks found RockOS Linux 6.6.87 online for over 14 hours, no QEMU guest,
unmounted Asterinas partition 2, and the stable FTDI UART at
`/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0` with no
current owner. RockOS `/boot` (`/dev/mmcblk1p1`) has only 3.4 MiB available;
its existing `/boot/extlinux/asterinas.conf` offers Basic, Probe, and Desktop
entries but defaults to RockOS. None of those entries selects the current
fixed Image. The staged `asterinas-0c2da50816ab.booti` matches the earlier release
kernel SHA-256 `0c2da50816abea7d6be6aefc092acccd31b64f2421aceeed8047f2a5b97f956b`.
The fixed RISC-V IPI-order Image SHA-256
`38024cb9bc2c544bf7729de289a282b6dba5d965bae3cd7c3786dee28c7573ff`
is **not** staged on this board. The old `plan-stage-diagnostics.json` selects
the old kernel and explicitly enables `asterinas.mmc_write_partition2`, so
it is not a safe performance-plan substitute or a read-only boot.

Host USB inventory showed only the FTDI UART, not an independent power/reset
controller. The boot tooling has an opt-in pre-boot hardware-watchdog path,
but the accessible history does not establish that this new path recovers a
full Firefox desktop on this board; the desktop runner explicitly reports
`manual-reset-required` on firmware/SBI loss. Therefore this preflight did
**not** reboot RockOS, select an old Asterinas kernel, write either MMC
partition, arm a new watchdog, or claim a bare-metal latency sample. A
current-image bare-metal run requires an operator/recovery path and a
content-addressed RAM/network load or sufficient boot-partition space; it
must not silently reuse the old image or delete existing boot files.

## Unattended RAM-only Megrez probe, 2026-09-16

The operator then explicitly authorized unattended physical experiments.
The current IPI-order fixed Image (`38024cb9bc2c544bf7729de289a282b6dba5d965bae3cd7c3786dee28c7573ff`)
was loaded into RAM, not staged on MMC.
The dedicated TCP-probe Stage1 (`4eef36c5481e004216f31236e0bc24005aca5dd3c4828f722c40a02042d5939b`)
and original Megrez DTB (`02a8d43d581b4aa8e957e231ee90eba19ffd7e8cfcf74694e86a1fb9c6b37f17`)
were selected because the board runner adds the framebuffer node itself.
The first attempt used an already-prepared DTB, so `fdt mknode` found the node
and stopped before `booti`; no guest or watchdog ran in that attempt.

The matching four-hart Sv39 QEMU TCP probe and dedicated software-reboot
recovery gate passed before the board was touched.
The second physical attempt booted Asterinas and reached the client-validated
16 KiB, 64 KiB, and 1 MiB progress markers.
The fixture accepted all four responses, including 16 MiB, but the guest did
not emit the final 16 MiB verification marker before a fresh firmware epoch.
RockOS subsequently came online. The board runner was stopped only after SSH
confirmed recovery and retained a failing `board-terminated-15` result;
the fixture's send completion is not guest completion.
Its result/serial SHA-256 values are
`e6ff1e40e074d5548171307b17b99f31d5dc0ffca81dbbc2324f6ef6a9200099`
and `ac68b242a5283daca8d5395b16dfcda7728ccb7436f322b3ad3956d4486e3539`.

The third plan declared the 1 MiB guest verification line as the bounded
physical terminal marker, with the same kernel, Stage1, original board DTB,
and bootargs. Its plan SHA-256 is
`38cb89a253e825f3b65a2ce6aa2b7158c8c3f337812da1fa8e0310a57e9119e4`.
The fresh QEMU fast gate passed, and recovery evidence was bound to that plan.
On Megrez the runner verified the planned RAM payloads by CRC32, armed and
read back the EIC7700 watchdog, executed one `booti`, observed the 1 MiB
guest marker exactly once, then observed a fresh OpenSBI/U-Boot prompt.
It published `board-pass`. The same raw serial additionally contains one
full 16 MiB **guest** ready line and the fixture records all four accepted
responses; that extra line was observed but was not the declared terminal
quality door, so repeatability of the full 16 MiB gate remains unproven.
The successful board result/serial/fixture SHA-256 values are
`348d811d40702c5d7bcf68babccbfe5d3f0eb89a19b53f555c5f1e67d7c0419e`,
`cabe56d214052569318d250d6a58bd54039c23cffba1bde60b83217eb9da0a75`,
and `214886d6f0591ad5a15004f6cd7dce8f96f9d521af3b1734fd1cedd8535d2ea3`.

After the guarded runner stopped at U-Boot, the host explicitly ran
`bootcmd_rockos`, not the generic `bootcmd` (which tries the Asterinas
selector first). RockOS returned over SSH with boot ID
`82a5daa1-40a1-47ba-8fc4-119bc08f054d`.
The vendor extlinux default remained `l0` (RockOS), partition 2 was unmounted,
and the earlier staged release Image retained its SHA-256.
No partition write, `saveenv`, selector promotion, or browser latency sample
was performed. The short watchdog is a valid recovery gate for this probe,
not a demonstrated recovery guarantee for a long Firefox desktop session.
The requested matched physical Firefox speedup remains unmeasured.
