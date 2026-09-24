# Firefox online desktop and short Megrez video baseline

Date: 2026-09-25. This record began with a signed QEMU image and a temporary,
memory-only desktop experiment on the previously installed Megrez system.
Later sections document signed-root installation and bounded physical boots;
the earlier live-overlay experiment alone did not establish reboot behavior.

## Signed QEMU desktop

The RISC-V Sv39/SMP=4 kernel Image was
`a64142bd775fd46c958125d9a85256bd45e96d984d47695e09327bb496dbc01b`.
The signed `browser-web` ext2 root was
`b7f0f75a8bc2e6fcc2e60e2058ebcfbb1ec926b633b6bca0f55710d618e9bab2`.
The root contract verified against its manifest and package lock.  The short
graphical gate completed in 80.03 seconds with six captured states: Firefox
open, minimized, restored, Files, Terminal, and a taskbar switch to Firefox.
The [minimized capture](qemu-minimized.png) visibly shows the wallpaper,
desktop launchers, and bottom taskbar.  Files and Terminal windows and the
taskbar switch were also visually inspected in the retained local gate output
`/tmp/asterinas-main-desktop-shell-qemu-final/`.

The signed image contains a world-readable shared MIME database.  This is
necessary for PCManFM to recognize `.desktop` launchers and the SVG wallpaper;
the first signed attempt without generated MIME data displayed blank icons and
no wallpaper.  The panel uses application IDs instead of absolute launcher
paths, and the Terminal launcher uses one quoted-free Xterm title argument.
The gate used a QEMU tablet and framebuffer.  It does not exercise Megrez HDMI
scanout or physical USB input.

The broader online browser gate did **not** pass on this signed image.  It
twice reached Baidu's home page, then stalled after Marionette requested the
controlled fixture home page.  The first run was stopped after about eight
minutes; the second bounded diagnostic run ended with
`browser-timeout:phase-probe-fixture-home`.  The fixture received twenty
earlier network-probe requests.  Its summary intentionally does not record
ordinary `/browser-quality/` page requests, so it cannot establish whether
the browser requested the fixture page.  Twelve GDB-stub samples during the second
stall showed active user-space PCs and idle kernel harts, without recurrence
of the earlier vDSO/coarse-clock lock deadlock.  They do not identify the
Firefox function or prove that the kernel is uninvolved.  The local raw
samples are under
`/home/ubuntu/.codex/asterinas-main-qemu-pc-20260925/`.

A third bounded QEMU run tested whether the new desktop processes caused this
stall.  A verified development overlay replaced only
`/usr/lib/asterinas/desktop-m5-session` with the pre-shell version; it kept
the same signed root as its base and the same Sv39 kernel.  PCManFM and LXPanel
therefore did not start.  The [raw gate result](qemu-shell-off-result.json)
still failed at `browser-timeout:phase-probe-fixture-home` after Baidu loaded.
This excludes the new shell processes as a necessary cause
of this particular QEMU failure, but does not identify the remaining cause.

Two more disposable overlays narrowed the failure.  In the
[fixture-first result](qemu-fixture-first-result.json), the Marionette script
omitted its initial Baidu navigation and still timed out at the same fixture
probe.  In the [startup-fixture result](qemu-fixture-startup-result.json),
Firefox was launched with the owned fixture as its initial URL and the normal
gate again timed out at that probe.  A separate short graphical capture of
this startup overlay [shows the fixture document rendered](qemu-fixture-startup-open.png)
while a Baidu navigation was already in progress.  The owned HTTP page can
therefore reach and render in Firefox; the remaining failure is at or after
the Marionette navigation/readiness boundary.  These overlays were
diagnostic only and are not part of the signed root.

Two further diagnostic overlays split that boundary.  In the
[title result](qemu-fixture-title-result.json), `WebDriver:GetTitle` returned
after fixture navigation, while the regular readiness script stalled.  In the
[ping result](qemu-fixture-ping-result.json), `GetTitle` again returned but a
minimal `WebDriver:ExecuteScript` reading only `location.href` and
`document.readyState` stalled.  The large readiness script's DOM traversal is
therefore not necessary for the failure.  These two runs did not capture the
returned title or the Marionette request/response boundary.

The [transport excerpt](qemu-fixture-transport-excerpt.log) from the next
[bounded run](qemu-fixture-transport-result.json) shows that fixture
`Navigate` returned, `GetTitle` returned a title other than the expected
fixture title, and the minimal `ExecuteScript` request was fully sent without
a response before the host deadline.  A separate
[15-second title-poll run](qemu-fixture-titlewait-result.json) returned an
unexpected title on all fifteen polls and failed explicitly before sending
any fixture script.  These results point to the fixture navigation not
committing promptly under the current Marionette session; they do not yet
show the actual returned title or the reason for the missing commit.

A host-only [HTTP trace](qemu-fixture-http-excerpt.log) of the unmodified
signed image then recorded a 200 response for the 8100-byte fixture page,
followed by 200 responses for its PNG, audio, and capability JSON resources.
The [gate result](qemu-fixture-hosttrace-complete-result.json) still timed out.
In another run, delaying only the fixture's capability routine from 0.5 to
60 seconds kept Marionette `ExecuteScript` responsive throughout the short
[capture](qemu-fixture-delay-excerpt.log), although the
[diagnostic gate](qemu-fixture-delay-result.json) correctly did not accept
capabilities that had not yet run.  Page work can therefore affect the
failure, but the HTTP trace and title-poll runs also show timing variation;
none of these changed fixtures qualifies as a browser gate pass.

The most precise capability result came from starting Firefox on the owned
fixture before any public navigation.  A diagnostic page added synchronous
step markers and reached `storage`, `wasm`, `worker`, `indexeddb`, `audio`,
`fetch`, and `complete`.  The [guest probe excerpt](qemu-initial-fixture-capabilities-excerpt.log)
shows repeated successful `ExecuteScript` replies but a fail-closed
`false-capability:wasm` result: every other reported capability was true.
The [run result](qemu-initial-fixture-steptrace-result.json) records the
timeout.  An earlier [initial-page run](qemu-initial-fixture-result.json)
reached the same probe phase without step markers.  This distinguishes a
capability validation failure from a transport call that does not reply.

The signed image contains Debian `firefox-esr` 140.16.0esr-1~deb13u1 and no
JIT overlay marker.  A diagnostic-only overlay that skipped the external
public HTTPS preflight, while retaining the owned fixture, queried its
runtime directly: `typeof WebAssembly` and `typeof WebAssembly.instantiate`
were both `undefined` in the [guest excerpt](qemu-wasm-runtime-excerpt.log).
The [diagnostic result](qemu-wasm-runtime-diagnostic-result.json) is not a
functional gate pass because the public check was skipped.  The 39-byte Wasm
module is valid on the host (Node returned 42), and the
[previously installed physical Firefox 143 JIT root](physical-wasm-diagnostic.json)
exposed both APIs and returned 42 for that same module.  The ESR/JIT browser
build difference is a concrete explanation for the QEMU `wasm=false` result;
it does not explain every observed navigation stall or establish a kernel
performance improvement.  The repository documents an opt-in, hash-pinned
Firefox 143 JIT root build for this reason.

## Signed Firefox 143 JIT QEMU follow-up

A second `browser-web` image was built with the documented, hash-pinned
Firefox 143.0.3 JIT overlay. Its ext2 SHA-256 is
`48a01cbccbad28785595abeea32f5cf3cc6f54a213f7d841eff35d029aecb15d`;
the schema-seven manifest SHA-256 is
`92b7b9d82ab809f07eb890d16673c8ce09a1c0edc72eb7b36cf67e9f077b9ce0`.
The root contract verified locally. The image still records Debian
`firefox-esr` in its package lock because the opt-in JIT package is a
separately hashed overlay; its manifest records the overlay marker
`93bc24c92f47df6abccaa280bd2b345c4b52f0da60e4ac6d638fdcedec806c21`.
It used the same Sv39/SMP=4 kernel Image as the ESR run. This is a browser
build comparison, not a kernel A/B.

The [short QEMU desktop run](qemu-jit-desktop-result.json) completed in
76.437 seconds. The visually inspected [minimized capture](qemu-jit-minimized.png)
shows the wallpaper, all three desktop launchers, and the bottom taskbar.
The full [browser run](qemu-jit-browser-result.json) passed the owned fixture
home, its JavaScript/WebAssembly capability check, fixture search and download,
Bilibili home/detail, and actual Bilibili video playback. The decisive guest
[phase excerpt](qemu-jit-browser-excerpt.log) includes these completions. It
then failed the required Baidu search outcome at
`DEBIAN_BROWSER_WEB_FAIL reason=baidu-search-not-pass`: the gate had accepted
its separate `external-captcha` outcome before the subsequent strict
`baidu_outcome=pass` check, and Firefox logged Baidu challenge-site WebGL
fingerprinting warnings. The run does **not** qualify as a seven-group gate
pass. The external search block is distinct from the earlier ESR fixture
failure; the controlled and Bilibili paths now have positive QEMU evidence.

The [independent bounded retry](qemu-jit-browser-retry-result.json) on the
same signed image and kernel reached the same boundary. Its
[phase excerpt](qemu-jit-browser-retry-excerpt.log) again shows completed
fixture navigation/download and Bilibili playback, followed by
`baidu-search-not-pass`. The source gate's captcha marker had a suffix-matching
bug: it checked for `baidu_outcome=external-captcha` only at the end of the
content string, although capability and download fields follow it. The
source fix adds the missing following-space match and a shell-behavior
regression test; it preserves the strict final search pass requirement.
This fix was made **after** the signed image build, so neither QEMU result
contains the corrected marker. A future signed build must include the new
source before it is considered for installation.

## Megrez temporary desktop

The board boot ID was `c6846443-9d90-4ff5-854e-5c4132fcfbbc`; the selected
kernel file remained `/boot/asterinas-route-052656e9b12c.booti` with SHA-256
`052656e9b12ce1586a76e18c75acece62c748bb3e6c9430d764fd0d7436ff116`.
Two nonce-framed serial connections, including a close and reopen, proved UID
0, PID 1 `systemd`, and `/dev/mmcblk0p2` mounted as ext2.  The serial device
was `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`.

The older installed root had PCManFM, LXPanel, and `update-mime-database`, but
no `/usr/share/mime/mime.cache`, desktop profile, or `Desktop` directory.  A
SHA-256-checked bundle staged the new wallpaper, launchers, and profiles under
`/run/asterinas-desktop` and the existing tmpfs-mounted
`/home/asterinas`; MIME data was generated under `/run`.  No MMC partition or
boot menu was written.  The [open capture](megrez-open.png) shows wallpaper,
desktop icons, Firefox, and the bottom panel.  The Files and Terminal panel
buttons launched real windows; the [Terminal capture](megrez-terminal.png)
shows a shell prompt, and the taskbar switched back to Firefox.

The physical minimize check is **not accepted**.  After minimizing Firefox,
PCManFM stayed running and its wallpaper and the panel remained visible, but
the desktop icons disappeared in the
[captured frame](megrez-minimized-icons-missing.png).  Restoring Firefox or
restarting PCManFM brought them back.  The loss repeated after restarting
Firefox while PCManFM was already running, so launch order alone does not
explain it.  This live-overlay result must be repeated on a boot of the signed
image before assigning the defect to Asterinas, Xorg/fbdev, or PCManFM.

A later repeat on the **same old boot and temporary overlay**, after several
Firefox service restarts, showed a different outcome. Firefox was restored and
minimized again after the Files and Terminal windows were closed; the
[new capture](megrez-minimized-retry-icons-visible.png) retained all three
desktop icons, wallpaper, and bottom panel. The earlier loss is therefore
intermittent in this setup, not a deterministic minimize failure. Neither
capture qualifies the signed image's physical behavior or reboot persistence.

## Bounded video measurement

The [raw run record](physical-video-runs.json) uses the same
[300-frame VP8 clip](../2026-09-24-board-video-current-image/result.json) as
the preceding board probe (SHA-256
`1aa863f2a73698241c9daa016c7bfcfa0f94b038ca9f0b29f296c3ca0b65eb95`).
The board display was 1920×1080.  Each browser run played ten seconds of the
1280×720 source; only its display size changed.  The runs alternated large
and small to limit drift, with no long stress interval.

| Displayed video | Dropped frames per 300 | Median | Firefox parent CPU ticks, user + kernel |
| --- | --- | ---: | --- |
| 1280×720 | 112, 118, 116 | 116 | 2438, 2490, 2368 |
| 640×360 | 11, 15, 19 | 15 | 1942, 1621, 2108 |

`getconf CLK_TCK` was 100.  The parent Firefox process used roughly 23.7–24.9
CPU-seconds in the full-size runs; Xorg used 0.86–0.97 CPU-seconds.  Firefox
content processes were not included in these process counters.  The full-size
loss is close to the prior one-run 121/300 observation, but these are
different boots and do not qualify an A/B speedup or a no-regression claim.
The size sensitivity points to video presentation work as a priority for
function-level profiling; it does not by itself isolate decode, scaling,
compositing, X11 copy, scanout, or a kernel cost.

One additional 10-second full-size run on the same older physical boot
[sampled the whole Firefox process tree](physical-video-cpu-split.json), not
only its parent. It ended with 112 dropped frames out of 301 reported frames,
consistent with the previous full-size range. At `CLK_TCK=100`, the Firefox
parent consumed 19.32 s user + 4.89 s system; its RDD media process consumed
6.76 s user + 2.32 s system; Web Content consumed 1.57 s user + 1.44 s system;
Xorg consumed 0.64 s user + 0.34 s system. This identifies parent and RDD
threads as the main CPU consumers during this clip. The sums are CPU time
across four harts, not elapsed latency or a decomposition of kernel functions.
Sampling process counters does not identify a specific copy, compositor,
decoder, syscall, or kernel bottleneck; a function-level profile is still
needed before selecting a performance patch.

Two more bounded runs sampled per-thread CPU counters on the same unmodified
Firefox 143 root: [full-size](physical-video-large-threads.json) and
[half-size](physical-video-small-threads.json). The source was the same 1280×720
300-frame VP8 clip; only its displayed size changed. Full-size dropped
124/300 frames versus 10/300 at 640×360. In the Firefox parent, `Renderer`
used 9.71 versus 7.66 CPU-seconds, and `SwComposite` 7.44 versus 5.77.
These two software-graphics threads consumed 17.15 CPU-seconds during the
full-size run, each approaching a full core over ten seconds. This is a
stronger reason to investigate the rendering/presentation path than the
process total alone. Short-lived RDD decode threads appeared after the initial
thread snapshot and are excluded from these per-thread deltas; the preceding
whole-process sample remains the valid RDD estimate. The samples do not
resolve functions or separate user-space rasterization from waits on the
framebuffer or kernel scheduling.

A temporary, runtime-only Firefox profiler configuration was tested and
removed. `SIGUSR2` terminated the browser with signal 12 rather than writing
a profile. A separate run with `MOZ_PROFILER_SHUTDOWN` played the same clip
(119/300 drops), received a normal `Ctrl+Q`, and exited cleanly, but the
profile file was absent. The [service-status excerpt](physical-profiler-status.log)
records the failed signal path, normal exit, absent profile, restored service,
and removed override. There is no Gecko flamegraph from this experiment.

The current kernel exposes `/dev/fb0` as a direct `IoMem` mapping; it does
not itself composite Firefox's pixels. On RISC-V its firmware framebuffer
uses `CachePolicy::Uncacheable`. Changing this to `WriteCombining` without
hardware qualification is unsafe on Megrez: the board's documented EIC7700
configuration lacks Svpbmt, so the RISC-V PTE code would silently fall back
to a cacheable mapping instead of PBMT_NC. A graphics-memory policy change
requires a board-specific cache/scanout contract and same-clip A/B, while a
DRM path remains a separate later integration task.

The new Sv39 kernel and signed root were not installed on Megrez for this
record.  Reboot persistence, the seven-group physical browser gate, the
minimize defect, and a qualified performance A/B remain open.  The DRM branch
was not merged.

After the short experiments, two new nonce-framed commands used separate
open/close cycles on
`/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`.
Both returned UID 0 and the unchanged boot ID
`c6846443-9d90-4ff5-854e-5c4132fcfbbc`; the first also returned
`/dev/mmcblk0p2 ext2`, `asterinas-browser-web.service` active with PID 5928,
and a visible Firefox X window. The runtime-only profiler service override
was absent. This establishes live local root command control after serial
reopen, not reboot persistence or a boot of the new image. The previous
RockOS-selected Desktop boot file and the RockOS recovery entry were not
modified.

A separate Sv48/SMP=4 release Image was built for a later controlled Megrez
boot: SHA-256
`4d1e0ee4ef2cb23ae6bbbca3ec425b3177013d5bdf1c261bc2e990ade22c9025`.
It was not installed or booted during this record.

## RISC-V user-PC diagnostic prerequisite

[PR #177](https://github.com/TankTechnology/asterinas-riscv/pull/177)
was integrated on `main` as merge commit `c65c40766`. The merged tree passed
`make run_kernel AUTO_TEST=riscv_ptrace_regset TARGET_ARCH=riscv64 SMP=4
FEATURES=riscv_sv39_mode RELEASE=1`: `TRACEME` and external
`ATTACH`/`DETACH` returned 33 checks passed and zero failed. The
[QEMU assertion excerpt](ptrace-regset-qemu-excerpt.log) retains the individual
checks and terminal marker. `cargo fmt --all --check`, `git diff --cached
--check`, and the x86-64 release kernel build also passed; the latter's
[build excerpt](ptrace-regset-x86-build-excerpt.log) records the release build
and ISO creation. This adds stopped-user-thread register reads for later PC
sampling, not a Firefox profile or a measured speedup. The physical board
still boots the older `052656e9b12c...` kernel, so this diagnostic has not
yet been exercised on Megrez.

The merged tree also passed the Megrez-oriented Sv48/SMP=4 release gate,
`make run_kernel AUTO_TEST=riscv_ptrace_regset TARGET_ARCH=riscv64 SMP=4
RELEASE=1`: 23 `TRACEME` and 10 external `ATTACH`/`DETACH` checks passed,
zero failed. The [Sv48 terminal excerpt](ptrace-regset-sv48-qemu-excerpt.log)
records this distinct run. Its uninstalled Image is 6,082,688 bytes, SHA-256
`6694c4c7ff5aeb715c5c3acf9a9c2f0f6318eaba7d7848a61b9d88bc63333180`.
QEMU Sv48 coverage does not establish the board's boot or desktop behavior.

## Megrez ptrace canary and bounded video PC attribution

The Sv48 Image above was copied to RockOS `/boot` under the new name
`asterinas-ptrace-6694c4c7ff5aeb71.booti`; the old Desktop kernel and
RockOS-default menu were left intact. A one-time U-Boot `booti` used the
existing Stage1 `stage1-cfec77d41d68.cpio`, the prepared DTB
`megrez-465cb129333c.dtb`, and the preceding Desktop boot arguments plus a
300-second userspace sync-reboot deadline ahead of the 420-second kernel
fallback. U-Boot checked CRC32 `ab15b580` (new Image), `1f97beff` (Stage1),
and `40ed4c65` (DTB) before each of two boots. The
[canary excerpts](physical-ptrace-canary-excerpt.log) show kernel entry, the
root debug console, `/dev/mmcblk0p2` as ext2, and active graphical/desktop
services on both boots. The first boot ID was
`8cf172d1-9498-40b3-94fc-777ff382171e`; the second was
`afc83897-a872-4d9c-8e6b-985b73dcf83f`. Fresh, nonce-framed serial
connections verified UID 0 and each boot ID. Firefox remained active after
the first pilot attach/detach. The first boot later appeared at the U-Boot
prompt after the 300-second safety deadline; that transition was not logged
continuously, so its exact reboot cause is not independently proven. The
second boot confirms the same canary can start after an actual reset. No
signed new desktop root was installed; both boots used the older board root.

On the first boot, the [bounded PC sampler](../../../../tools/riscv/debian/rootfs/thread_pc_sampler.py)
read the `Renderer` and `SwComposite` threads with RISC-V
`PTRACE_GETREGSET(NT_PRSTATUS)`. A three-sample idle pilot returned nonzero
PCs and left Firefox running. The active run took 40 samples per thread at
200-ms intervals while the same 300-frame, ten-second, 1280×720 VP8 clip
played. The [raw PCs](physical-pc-active.jsonl),
[memory maps](physical-pc-active.maps.gz), and
[baseline/sample/baseline metrics](physical-video-pc-ab.json) are retained.
The uncompressed maps file SHA-256 is
`3977994a89e629f1a1ba81d3f66cfe482c0fc6063b9fe08e6636aa37566ec5f9`;
the JSONL SHA-256 is
`ffb727c75c5c1ec373117ebf639719f2931e3cc3f37446c69f9e7bceefe01634`.

| Run | Sampler | Dropped / 300 | Playback wall time |
| --- | --- | ---: | ---: |
| Baseline A | off | 112 | 10.287 s |
| Sample | 40 × 2 thread stops | 116 | 10.211 s |
| Baseline B | off | 111 | 10.411 s |

The sampler spent 129.1 ms stopping/detaching `Renderer` and 145.5 ms on
`SwComposite` across the 7.8-second sampling window. These are probe costs,
not the video's total overhead. The dropped-frame result is within the
preceding short-run range, but one A/B/A triplet does not prove negligible
sampling bias or any speedup.

The board's `/usr/lib/firefox/libxul.so` SHA-256 was
`54076bf72d585b3591aa0edb814fee6a6012c6a9545aca43cd989359da23d1b7`,
matching the signed-image copy. Its Build ID is
`1c9f58ed7ccb6730d65e3957f560dc732b577218`, which matches the RISC-V
`firefox-dbgsym` 143.0.3-1 package at the
[Debian snapshot binary record](https://snapshot.debian.org/mr/binary/firefox-dbgsym/143.0.3-1/binfiles)
(archive SHA-1 `9fd945c6bbc6f6ba4e4855580e026dec88f4ef25`). Using each PC's
`/proc/maps` file offset and `riscv64-linux-gnu-addr2line` against that exact
debug file gives:

| Thread | `linear_row_yuv<false>` | `linear_blit<true>` | Other `libxul` | `libc` |
| --- | ---: | ---: | ---: | ---: |
| `Renderer` | 26 | 4 | 2 | 8 |
| `SwComposite` | 25 | 3 | 0 | 12 |

Thus 51 of 60 `libxul` samples landed in SWGL's YUV row conversion routine.
The 20 `libc` PCs were not used to infer active CPU time. This is concrete
user-space presentation-path evidence for the native-size video problem; it
does not measure kernel PCs, stack ancestry, or the impact of a candidate
optimization. The leading next comparison is a targeted SWGL/YUV or physical
display-path variant with the same clip and A/B/A gate, after the signed
desktop root and recovery path are physically qualified. A speculative
framebuffer cache-policy switch is not justified by these samples.

## Current-main signed root and physical daily-use gate

The current `main` browser-web image was rebuilt with the pinned Firefox 143
JIT overlay. The 2-GiB ext2 SHA-256 was
`7710b74f10fd47f9ac5caa0ea570c4b3993bb4fe2d58501e37db6a7e771f678a`;
the manifest SHA-256 was
`cd17d518ab113d4b692c7aceec3dd9007caeb6a0d4a44b95120ef017784aa77e`.
The root contract and `e2fsck -fn` passed. The
[72.838-second QEMU desktop gate](qemu-current-main-desktop-result.json)
completed six captures. Visual review of the
[minimized state](qemu-current-main-minimized.png) found the wallpaper,
three launchers, and bottom panel.

Before installing it on Megrez, RockOS made a complete 4-GiB backup of
partition 2 at
`/home/debian/asterinas/backups/p2-before-browser-web-20260925.img`
(SHA-256
`a2ecd0582e66ba209772e316883ae3ddbabc66c2a1d170ef5b037594ea0f8733`).
The recovery script's checksum and unmounted-partition check passed; no
restore was needed. The new 2-GiB image was staged on RockOS partition 3,
checksum-verified there, written to the start of partition 2, and compared
byte-for-byte with the source. The RockOS-default boot selection and older
Asterinas menu entry were preserved. One-time U-Boot commands selected the
ptrace Image SHA-256 `6694c4c7ff5aeb715c5c3acf9a9c2f0f6318eaba7d7848a61b9d88bc63333180`.
The Stage1 and DTB CRC32 values remained `1f97beff` and `40ed4c65`.
The host serial device was
`/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`.

With the persistent provisioned home, a fresh UID-0 serial check identified
boot `f693e9b9-0223-455d-8c7d-6221267c25bf`, graphical services, and
Firefox. The [1920×1080 capture](physical-current-main-desktop.png)
shows Firefox with the wallpaper, launchers, and panel. Another one-time boot,
`33baa8e0-c8e5-466f-9fd2-e9037c8f13ce`, retained the same persistent home:
the [minimized capture](physical-current-main-minimized.png) shows all three
launchers, wallpaper, and panel, and clicking the Firefox panel item restored
the window to `Normal`. The earlier old-root intermittent missing-icons result
did not reproduce in these signed-root captures. A separate `--volatile-home`
boot covered the pre-provisioned home and produced an empty desktop; that
is an overlay configuration effect, not evidence of a framebuffer regression.

The isolated physical smoke boot `911b49f8-fb23-407d-b02d-b03ed2c6ca1c`
ran the [daily-use gate](physical-current-main-daily-use-result.json) once,
with a 30-second per-phase timeout. The
[raw capture archive](physical-current-main-daily-use.tar.gz) has SHA-256
`1d0f6c87200fcde70a353013f8d13d96f1786cccb5f10134c22081970560ee74`.
All seven functional groups passed: document, storage, execution,
rendering/media, navigation, download, and contexts. The single run measured
48.307 seconds from Firefox exec to first window, keyboard next-frame p95
10 ms, pointer next-frame p95 89 ms, scroll next-frame p95 65 ms, local
navigation command 156.491 ms, and response-to-DOM 187 ms. Context opening
took 718.246 ms and was classified `slow` against its 500-ms target; total
open/select/return/close time was 884.983 ms. These are smoke measurements
with synthetic input and distinct browser/guest clock domains, not a Linux
comparison or a twofold speedup claim.

The generic browser-web autogate also ran on the physical image after Baidu
home, but its Marionette fixture check rejected the board's physical-LAN URL
because that gate enforces a frozen QEMU-slirp URL. It is not a failed
physical browser capability. A later invocation in the same boot was rejected
before any workload because the autogate had already written the unique
first-window timeline marker. The isolated daily-use boot masked the autogate
in `/run` before starting `basic.target`, avoiding that collision. A source
follow-up makes the autogate exit successfully in explicit `basic-only` mode;
the separately rebuilt and tested image is recorded below. After the final
bounded boot's safety reboot, a fresh nonce-framed
serial login proved RockOS UID 0, Linux `6.6.87`, and partition 2 unmounted.

The performance samples keep kernel and user CPU time separate but do not
attribute kernel PCs. During the smoke workload, Firefox consumed multiple
hart-seconds over each roughly 2.4-second interval. For example, in the
172.131–174.668-second guest interval, the main Firefox thread consumed
1.62 s user + 0.46 s kernel, and `Renderer` consumed 1.15 s user + 0.28 s
kernel; their run-queue waits were 276 ms and 160 ms. This interval overlaps
several test actions, so it cannot be assigned wholly to context opening.
No patch has yet been shown to halve a relevant latency or double throughput.
The video-PC evidence above makes SWGL YUV conversion a concrete user-space
target. Context opening and its CPU/run-queue interval are a second target for
a short controlled A/B before changing scheduling, memory, or the display
driver.

## Basic-only autogate fix and signed-root repeat

The physical-LAN fixture failure above exposed a startup contract mismatch:
the `ASTERINAS_BROWSER_WEB_BASIC_ONLY=1` setting disabled Firefox's background
fetchers but did not stop the QEMU-specific browser-web evidence service.
The evidence script now emits `DEBIAN_BROWSER_WEB_SKIP reason=basic-only` and
exits successfully before network or fixture validation in that explicit
mode. Its normal mode still runs the full checks. A shell-behavior regression
test first failed against the old script, then passed with the guard; all 88
browser-web unit tests passed.

The corrected, signed Firefox 143 root has ext2 SHA-256
`d4f4e88fb20a8938e7270f4ceaf9c79855ba3d038acbd6f1030fe269dfca9bd4`,
manifest SHA-256
`c6afd5b3c0bda56a71b8ade1bd085f49a5f607ea3aebdcb66403d84dda378771`,
and the same package-lock SHA-256
`aa92c0471dbd9c08b3adfce97235f78dc625054296fe37adce5afa0a77ce9966`.
Its root contract and `e2fsck -fn` passed; `debugfs` confirmed the installed
evidence script contains the guard. The
[73.124-second QEMU desktop gate](qemu-basic-only-desktop-result.json)
completed all six captures; the visually inspected
[minimized frame](qemu-basic-only-minimized.png) retains the wallpaper,
launchers, and panel. This QEMU boot did not set `basic-only`, so its normal
browser evidence path remained enabled.

RockOS staged this exact 2-GiB image and verified its SHA-256. The install
script rechecked the original 4-GiB backup and unmounted partition 2, then
reported `INSTALL_PASS` after a byte-for-byte readback of the written image.
The old menu/kernel and RockOS default stayed in place. A one-time boot of
the same ptrace Image, Stage1, and DTB entered Asterinas with boot ID
`52199f0b-0ba0-4809-8ba9-74e636f20473`; a fresh nonce-framed serial
command proved UID 0 and ext2 root. With the persistent home and full
physical network settings, the `basic-only` autogate service finished with
`Result=success`, `ExecMainStatus=0`, while Firefox stayed active. A manual
restart of only that evidence service gave the same status. The first-window
timeline marker was still absent before the separate daily-use gate, and no
full-gate diagnostic log appeared.

The [physical daily-use repeat](physical-basic-only-daily-use-result.json)
passed all seven functional groups once. The
[raw archive](physical-basic-only-daily-use.tar.gz) has SHA-256
`7108c92d35d236a2a8c80d75432a4350866139e46502a91aa25f35650c42f3c3`;
all six declared artifact hashes and sizes verified after upload. The run
measured 68.335 seconds from Firefox exec to first window; keyboard and
pointer next-frame p95 were 9 and 49 ms, and scroll next-frame p95 was
27 ms. Local navigation command time was 142.8 ms and response-to-DOM was
246 ms. Context opening took 1,335.569 ms, again `slow`; total context
open/select/return/close time was 1,664.064 ms. These values differ
substantially from the preceding isolated smoke boot and should be treated as
short-run variability, not an A/B speedup or regression attributed to the
basic-only guard. The physical
[minimized screenshot](physical-basic-only-minimized.png) shows the persistent
wallpaper, three launchers, and bottom panel. Clicking its Firefox taskbar
item restored the browser while the service remained active.
After the bounded reboot, a fresh serial login reached RockOS root on Linux
`6.6.87`; partition 2 was again unmounted and the RockOS default remained
selected. The console was not logged continuously through the reset, so this
records recovery rather than attributing the reset to one specific fallback.

One separate opt-in QEMU [syscall startup profile](qemu-basic-only-syscall-startup-result.json)
on the corrected signed root reached Firefox's Marionette port in 30.566
host seconds. The [raw serial log](qemu-basic-only-syscall-startup.log.gz) is
178,467 bytes and contains 516 synchronous syscall-profile lines, so this run
is diagnostic rather than a latency comparator. Its last Firefox PID-189
snapshot recorded `mprotect=1084/52`, `sched_yield=338/16`, and
`openat=1995/74` (completed calls / cumulative elapsed jiffies); OSTD uses
1,000 jiffies per second. The much larger `futex=2236/124241` total includes
scheduled-out waits and cannot be interpreted as kernel CPU time. On this
short current-root trace, the direct `mprotect` and `sched_yield` bills remain
too small to justify either as the sole target for a twofold Firefox gain.

Three otherwise matching, uninstrumented QEMU startup controls reached the
same Marionette boundary in [33.755](qemu-startup-control-1-result.json),
[31.998](qemu-startup-control-2-result.json), and
[32.744](qemu-startup-control-3-result.json) host seconds. The corresponding
raw serial logs are retained as
[run 1](qemu-startup-control-1.log.gz),
[run 2](qemu-startup-control-2.log.gz), and
[run 3](qemu-startup-control-3.log.gz); each uncompressed SHA-256 matches its
result JSON. The one syscall-diagnostic run at 30.566 seconds was below this
small control range. These runs show no obvious large startup penalty from the
diagnostic but do not quantify its overhead: there is only one instrumented
sample, and host wall time includes boot and QEMU variation.

## Persistent-home repeatability and context-open CPU diagnostic

The first opt-in context CPU attempt used a rebuilt Stage1 on the same signed
root. Its daily-use gate stopped after `session` and `samplers-ready` with
`phase-value-invalid`, before the context phase. A fresh root serial command
found `/home/asterinas/Downloads/asterinas-browser-quality.bin` on the
persistent HOME: regular file, UID 1000, 262,144 bytes, and the exact fixture
SHA-256 `2312394bd99545d9de131c24efb781e765ac1aec243f2ed9347597a793a415e9`.
The previous successful gate had left it there. The test-owned file was
verified and removed for a second attempt, but its `/run` result was not
uploaded before the bounded reboot; it is not counted as a passing run.

The gate now clears only a prior download with that verified type, owner,
length, and digest before triggering a new one; unexpected content or a
symlink remains untouched and fails closed. It also has an opt-in
`--context-cpu-diagnostic` that brackets `WebDriver:NewWindow` with procfs
CPU snapshots without changing the ordinary performance result schema.
The updated Stage1 SHA-256 was
`ea446515661f2380568426b3c37da0b6a0698fc51f88d32c62807fce04890962`;
U-Boot verified its CRC32 `64e4ff7c`, plus the unchanged ptrace kernel Image
CRC32 `ab15b580` and DTB CRC32 `40ed4c65`. The one-time boot retained the
RockOS default, used persistent HOME and the isolated root debug console,
and armed 360-second userspace / 480-second kernel reboot limits. Its boot ID
was `6a18b44a-b27b-4137-87df-50fa1ef78f51`, established through fresh
nonce-framed UID-0 serial commands.

The [uploaded archive](physical-context-cpu-repeat.tar.gz) has SHA-256
`95db09739489575c7f58ceea08695a0fb03493ab96367194121ffda62fcbf7ea`
and contains the gate log, result, and all six captured artifacts. Every
artifact matched the [result manifest](physical-context-cpu-repeat-result.json)
by size and SHA-256. The gate passed all seven functional groups; its two
slow categories were input and context switching. In this one instrumented
run, keyboard next-rAF was 731 ms, pointer next-rAF 128 ms, and
[`NewWindow` took 823.481 ms](physical-context-cpu-repeat-context.json).
The exact context-open snapshot interval lasted 827.509 ms; its two procfs
reads cost 8.809 ms. Firefox's parent process accumulated 860 ms user and
420 ms kernel CPU across its threads, while Xorg accumulated 20 ms user and
10 ms kernel CPU. The interval contains 1,683 system context switches and
zero major faults in the two observed processes. Firefox CPU can exceed the
wall interval because multiple threads run in parallel. This short process
snapshot excludes Firefox child processes and cannot identify a kernel
function or establish a speedup; the anomalous input latency also prevents
using this diagnostic run as a stable performance baseline.

After the archive upload, a fresh nonce-framed Asterinas root command
reported gate and upload statuses both zero and the archive SHA-256 returned
by the host. An authenticated software reboot reached the existing U-Boot
menu; RockOS entry 1 then booted successfully. A new root serial login
verified RockOS boot ID `9b0a4e23-d3a4-4c9e-bc82-66bff9e717dd`, its ext4
root on `/dev/mmcblk1p3`, unmounted Asterinas partition 2, and unchanged
`default l0` selector. The experimental Stage1 was verified as unreferenced
by the menu and removed from `/boot` after recovery; its host copy and SHA-256
remain in the experiment record. This establishes recovery for this boot,
not persistent Asterinas root-console access after an Asterinas menu reboot.

## Current desktop menu candidate, not promoted

The installed `/boot/extlinux/asterinas.conf` still selects the older
`asterinas-485b9079c204.booti` kernel and `stage1-d62ab8325e03.cpio` for
Desktop, with `--volatile-home`. Its SHA-256 is
`02280720efe7a1ad0ac084cdc20429406631e12d2e16f05638544bab0883fb26`.
The RockOS vendor `/boot/extlinux/extlinux.conf` remains SHA-256
`eb5f39a6e2db71ccc93ae005c488fd9f7ef353e75426e5a51e8923a9cbf2ebc5`
and `default l0`.

The menu builder now accepts the physically tested persistent-home
`--debug-console=isolated-root` mode. A [candidate manifest](megrez-current-desktop-menu-manifest.json)
and [selector](megrez-current-desktop-menu.conf) were prepared from the exact
RockOS vendor file, current ptrace kernel, existing Basic/Probe Stage1,
prepared board DTB, and the repeatable daily-use Stage1. The candidate
selector SHA-256 is
`5b2bd5c2378b164ff4028d3211b1c8f4f7cb74db1c7e94689bdda52d8ce37f13`.
RockOS verified the two new immutable files and the canary selector at
`/boot/extlinux/asterinas-menu-5b2bd5c2378b.conf`; the publisher reported
`ASTERINAS_MENU_CANARY_READY`. The active selector was verified unchanged
afterwards, and partition 2 remained unmounted.

To create space on the nearly full boot partition, the unreferenced
`asterinas-790ab694-34bc1cc0.Image` was copied byte-for-byte to
`/home/debian/asterinas/backups/boot-archive-20260925/` on RockOS, verified
with SHA-256
`f333402e9102e9094cead70cee6ed59e161fb0d226bff461e1a66cc1fca73607`,
and only then removed from `/boot`. The candidate added about 7.2 MB; the
boot filesystem initially reported zero non-root available blocks, with 13,656
1-KiB blocks free for root.

Two more unreferenced historical images were then archived to the same RockOS
backup directory. `asterinas-da3e516e-26230a23.Image` has SHA-256
`26230a23670778c8a5e87f0d67fdea7a6c2f49fa9bea6d34c3c7159638a81393`;
`asterinas-firefox-readahead-sv48.booti` has SHA-256
`eab648cf8b372a4903f07c75a1b8eef72bcc92523d3ebdf7cd6bb82ebebcab15`.
Each was checked for selector references, copied and byte-compared before its
`/boot` copy was removed. `/boot` now has 13,808 KiB available to non-root
users (97% used). A fresh RockOS UID-0 command on boot ID
`9b0a4e23-d3a4-4c9e-bc82-66bff9e717dd` verified the vendor and active
selectors, canary, kernel and Desktop Stage1 hashes unchanged, with partition
2 unmounted. The backups retain both historical images for restoration.

The canary has **not** passed the menu's per-identity physical cycle gate and
has **not** replaced `/boot/extlinux/asterinas.conf`. Its unbounded Desktop
entry has no automatic reboot fallback, so the earlier bounded one-time
boots do not establish recovery from a hard hang in that menu entry. The
repository's promotion gate still requires three cycles each for RockOS,
fallback, Basic, and Probe, plus two Desktop cycles for this exact selector.
