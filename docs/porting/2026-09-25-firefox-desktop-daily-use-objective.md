# Firefox and Debian desktop daily-use objective

Date: 2026-09-25

## Outcome

On the Megrez board, Asterinas should boot into a usable Debian desktop: a
visible wallpaper and desktop, a persistent launcher/task bar, a file manager,
a terminal, and Firefox. A person should be able to use the mouse and keyboard
to launch, switch, minimize, and close applications, then browse, download, and
play a simple video without relying on a serial command for ordinary actions.
The local root debug console must remain available and survive a separate
reboot check.

The first implementation should reuse the already packaged lightweight Debian
shell (Openbox, PCManFM, LXPanel) and make LXPanel a bottom launcher/task bar.
This gives the requested Dock-like interaction without making a compositor or
an unvalidated GPU path a prerequisite. A heavier GNOME/KDE session is a later
candidate only after its required graphics and resource costs are measured on
this board. The target is genuine desktop use, not a Firefox window over an
otherwise empty X root window.

## Current checkpoint

The current `main` Firefox 143 JIT root (ext2 SHA-256
`d4f4e88fb20a8938e7270f4ceaf9c79855ba3d038acbd6f1030fe269dfca9bd4`)
is installed on Megrez partition 2 after a verified 4-GiB backup and
byte-for-byte post-write comparison. Its six-state QEMU desktop gate passed.
On the board, the persistent-home desktop visibly retained its wallpaper,
three launchers, and bottom panel when Firefox was minimized; the taskbar
restored the window. Fresh nonce-framed root serial commands established the
boot identity, and the bounded experiment returned to RockOS with partition 2
unmounted. The `basic-only` autogate fix was tested in the signed image: the
service exited successfully without invoking the QEMU-specific fixture gate.

Two short physical daily-use boots each passed all seven controlled functional
groups. Their context-open times were 718 and 1,336 ms; Firefox-exec-to-first-
window times were 48.3 and 68.3 seconds. They used different isolated-boot
home conditions, so this is a variability warning, not a same-condition A/B
comparison. The separate fixed 720p video baseline still drops roughly
110–125 of 300 frames, and 51 of 60 symbolized `libxul` video PCs were in
SWGL YUV conversion. No twofold speedup or qualified performance gain has
been demonstrated. The RockOS-default menu and older Asterinas kernel entry
remain unchanged; an unbounded Asterinas menu boot with persistent home and a
debug-console reconnect after an actual Asterinas reboot remain open. The
[signed-root evidence](evidence/2026-09-25-firefox-online-desktop/README.md)
contains the image identities, captures, raw daily-use results, and recovery
limits.

A follow-up persistent-home boot exposed a repeatability bug: the daily-use
gate rejected its own previous fixture download before collecting a new run.
The gate now safely clears only a verified test-owned copy. With that fix and
an opt-in context-open CPU probe, the next short physical run passed 7/7 and
uploaded its complete evidence before an authenticated reboot to RockOS.
`NewWindow` took 823 ms; Firefox's parent accumulated 860 ms user and 420 ms
kernel CPU across its threads during the 828-ms snapshot interval, while Xorg
used 30 ms. This single instrumented run also had a 731-ms keyboard next-rAF
outlier. It narrows the investigation but does not qualify a speedup or a
kernel function hotspot. The [repeatability and context evidence](evidence/2026-09-25-firefox-online-desktop/README.md#persistent-home-repeatability-and-context-open-cpu-diagnostic)
records the artifact hashes and RockOS recovery.

The next persistent-home, isolated-console Desktop menu generation has been
prepared and staged as an immutable canary. Its active selector is still the
older kernel/Stage1 with volatile HOME. Promotion remains contingent on the
same-menu physical cycle gate and a credible recovery path for an unbounded
Desktop boot; the [candidate record](evidence/2026-09-25-firefox-online-desktop/README.md#current-desktop-menu-candidate-not-promoted)
separates preparation from deployment.
One candidate RockOS cycle and one missing-selector fallback cycle passed;
the board returned to RockOS with a fresh root-console reconnect. Basic,
Probe, Desktop and the repeat counts required for promotion remain open.

A short physical native-size video A/B/A then tested CSS `image-rendering:
crisp-edges` against Firefox's default sampling on the same signed root.
The candidate dropped 246 frames, versus 182 and 175 in its two same-boot
controls, so it is rejected. The [three-run record](evidence/2026-09-25-firefox-online-desktop/README.md#native-size-video-sampling-aba-crisp-edges-rejected)
keeps the 303-frame candidate quality counter and the recovery limits intact.
No production browser preference changed, and the twofold target remains open.

A new opt-in physical CPU-clock probe has now measured the Asterinas boot hart
at 1.400 GHz in three 10 ms windows. RockOS on the same board ran at 1.8 GHz
and its controlled 1.8/1.4/1.8 GHz static-binary comparison reproduced the
expected compute-throughput gap. The live board OPP table declares 800 mV at
1.4 GHz and 900 mV at 1.8 GHz; the actual CPU rail and safe transition path
are still unverified. The [clock attribution and recovery record](evidence/2026-09-25-firefox-online-desktop/cpu-clock-attribution.md)
includes the raw logs. This explains some CPU-bound cost but is not a measured
Firefox speedup. No Asterinas clock register was changed; the twofold desktop
and video goals remain open.

## Earlier checkpoint before signed-root installation

The online shell integration and a vDSO writer lock-order stability fix are
on `main`. The signed Firefox 143 JIT image passed a short QEMU desktop gate:
wallpaper, icons, bottom panel, Firefox minimize/restore, Files, Terminal,
and task switching. Two independent bounded QEMU browser runs passed the
owned JavaScript/WebAssembly fixture, search, download, and Bilibili playback;
both then failed the strict Baidu search group when Baidu presented its
external challenge. The seven-group gate has **not** passed. See the
[dated evidence](evidence/2026-09-25-firefox-online-desktop/README.md) for
image identities, raw results, and the corrected captcha classification.

The board's normal menu still selects its previously installed kernel and
root. A temporary desktop overlay demonstrated the shell, but minimize/icon
behavior varied between attempts; no new signed root has been installed or
reboot-qualified.
The short native-size VP8 baseline still loses roughly 110–125 of 300 frames.
One full-size run attributed 9.71 CPU-seconds to Firefox `Renderer` and 7.44
to `SwComposite` over ten seconds, while Xorg used about 0.9. A smaller
displayed image lost 10/300 frames and used 7.66 plus 5.77 seconds in those
threads. The Gecko profiler attempts produced no profile; a qualified
performance optimization remains open.
RISC-V `PTRACE_GETREGSET` for `NT_PRSTATUS` has since merged to `main` as
[PR #177](https://github.com/TankTechnology/asterinas-riscv/pull/177).
Its 33-check QEMU regression and an x86-64 kernel build passed on the merged
tree. A separate Sv48/SMP=4 gate also passed 33/0, then the Sv48 release Image
completed two one-time physical boots with the preceding board root. The
RockOS-default menu and old Desktop Image stayed available. A signed desktop
root has still not been installed or reboot-qualified.
The [bounded thread-PC sampler](../../tools/riscv/debian/rootfs/thread_pc_sampler.py)
snapshots `/proc/<pid>/maps`, samples named threads through
`PTRACE_ATTACH`/`GETREGSET`/`DETACH`, records stop time, and caps the sample
count. Its host Linux integration tests and a physical pilot passed. During a
short physical VP8 A/B/A gate, native-size playback dropped 112/300 frames
without sampling, 116/300 with sampling, then 111/300 without sampling. With
the matching Debian Firefox 143 debug file, 51 of 60 `libxul` user-PC samples
from `Renderer` and `SwComposite` resolved to SWGL `linear_row_yuv<false>`.
This points to user-space YUV presentation work; the probe cannot identify
kernel execution or produce a call-stack flamegraph. The
[raw evidence and limits](evidence/2026-09-25-firefox-online-desktop/README.md)
are retained. No performance change or twofold speedup has been established.

## Earlier baseline and architecture

- Before this milestone, the online `browser-web` image inherited `pcmanfm`
  and `lxpanel` packages but started only Xorg and Openbox. The shell profile,
  launchers, and session startup are now installed in the signed JIT image on
  partition 2; the earlier board root did not contain them.
- The current display path is Xorg `fbdev` on `/dev/fb0` with GLX disabled. The
  physical Firefox video probe in [PR #176](https://github.com/TankTechnology/asterinas-riscv/pull/176)
  saw no `/dev/dri`. QEMU virtio-gpu DRM results do not establish physical
  Megrez GPU acceleration.
- The [physical daily-use baseline](../performance/2026-09-18-firefox-daily-use-physical-baseline.md)
  passed seven functional groups. Its median `contextSwitchTotalMs` was
  1215 ms. The [wake-balance A/B](../performance/2026-09-19-firefox-wake-balance-physical-ab.md)
  reduced that metric to 891 ms or less and improved both scroll metrics, but
  six of nine primary metrics overlap and the post-change classification is
  `mixed`. Another scheduler change needs new attribution first.
- [PR #176](https://github.com/TankTechnology/asterinas-riscv/pull/176)
  records a 720p VP8 clip losing about 120–140 of 300 frames at 1280×720,
  versus 5–12 at 640×360. In a comparable short sample, Firefox `Renderer`
  plus `SwComposite` used 14.17 versus 9.55 CPU-seconds; user-mode work
  accounts for 4.37 of the 4.62-second difference. Neither CPU pinning nor
  disabling WebRender established a speedup. This makes software presentation
  the leading video hypothesis, not a proven kernel hotspot.
- [PR #177](https://github.com/TankTechnology/asterinas-riscv/pull/177)
  added RISC-V `PTRACE_GETREGSET` for stopped user-thread PC attribution. It is
  diagnostic functionality, not a performance improvement or a kernel-PC
  profiler.

## Work sequence and acceptance

### 1. Restore the visible desktop shell

Install the existing wallpaper, PCManFM profile, LXPanel profile, and three
launchers into the online image. Start PCManFM desktop and LXPanel as UID 1000
after the X socket becomes ready; preserve one Xorg owner and the root debug
console. Provide a bottom launcher/task bar for Firefox, files, and terminal.
Firefox should open as a normal switchable window, not replace the desktop.

Acceptance requires a QEMU screenshot with Firefox minimized that visibly
shows the wallpaper, desktop icons, and launcher/task bar, plus a second
screenshot with Firefox open. Check process identity and click each launcher;
verify application switching and minimizing, not merely package presence.
Then repeat the same bounded gate on the board with a fresh serial root
reconnect. The existing browser functional gate must still pass.

### 2. Establish a short, reproducible user-experience baseline

Freeze the kernel Image, rootfs, display provider, resolution, Firefox package
and profile, local page/clip identity, and power/CPU settings for each
comparison. Use the same simple local page for startup, navigation, text input,
scroll, and download; separately use a fixed 300-frame 720p video and its
640×360 control. Record time from launch to usable window, input and scroll
p50/p95, dropped/decoded frames, Firefox and Xorg thread CPU time, runqueue
wait, kernel/user CPU split, and whether the desktop remains responsive while
video plays. Record a screenshot and the board's boot/control identity.

Use three short qualified runs per variant; first compare repeated baseline
runs to learn measurement spread. QEMU checks functionality and repeatability;
physical runs establish perceived performance. Do not replace this with an
hours-long load test or compare unrelated Firefox builds as a speedup claim.

### 3. Attribute the slow path before changing it

For browser startup and navigation, separate CPU execution, runnable delay,
blocking, page faults, file reads, and network time. For video, obtain bounded
function-level evidence for Firefox `Renderer`/`SwComposite`, distinguish
decode, scaling/color conversion, compositing, X11 copy, and scanout, and
measure the profiler's own overhead. User-PC samples can identify executable
regions; kernel hotspots need separate kernel-PC or focused syscall evidence.
An uninstrumented control is required for every intrusive probe.

Choose one change only after a dominant boundary is identified. Kernel work
may include compatibility or measured scheduling/VM/I/O hotspots; browser
software rendering and the display stack must remain valid candidates. A
physical DRM path enters the comparison only after modesetting, input, cursor,
desktop display, and recovery all pass on Megrez. A QEMU virtio-gpu pass alone
does not promote it to a board performance result.

### 4. Demonstrate an actual gain without losing daily use

For each candidate, run baseline/candidate/baseline with the same short
workload and artifact manifest. Require all seven existing daily-use groups,
the desktop visual/interaction gate, and control-channel recovery. Report each
latency and frame metric with its complete observed range; accept an affected
metric gain only when it exceeds baseline spread and no important interaction
regresses. Keep the changed component isolated so a failed candidate can be
reverted.

The stretch target is at least a twofold improvement in the measured bottleneck
that users actually notice. For the current video case, use the PR #176 gate:
native 1280×720 playback should drop fewer than 30 of 300 frames, with no
visible corruption, and the hot `Renderer`/`SwComposite` cost should fall by
at least half relative to a fresh same-artifact baseline. A claim that the
*whole desktop* is twice as fast requires startup, interaction, navigation,
and video to improve together; a microbenchmark or single thread is not that
claim.

## Review boundaries

Keep the shell integration, kernel functionality fixes, performance probes,
and display/DRM changes in separately reviewable commits or PRs. The next
functional boundary is a persistent-home Asterinas menu boot with fresh root
serial control after a separate actual Asterinas reboot; the external Baidu
challenge must remain separately classified. The next performance boundary
is a bounded A/B/A of
one SWGL/YUV or physical display-path variant against the identified hot
function, with matching Firefox and clip artifacts. The current `/dev/fb0`
mapping and
Megrez's lack of Svpbmt make a blind RISC-V cache-policy change unsafe.
Preserve the physical board's authenticated recovery path throughout
experiments and report unsupported observations as such.

The current [R4 DRM draft](https://github.com/TankTechnology/asterinas-riscv/pull/141)
adds a Megrez firmware-framebuffer backend that copies scanout data with the
CPU; it has not opened `/dev/dri/card0` on the physical board. Its
[R3 dependency](https://github.com/TankTechnology/asterinas-riscv/pull/142)
has a 64-MiB GEM pool without reclamation. These are functional admission
candidates, not measured 720p acceleration. Keep their validation separate
from the current signed-root video baseline and fix resource lifetime before
claiming a durable daily-use graphics path.
