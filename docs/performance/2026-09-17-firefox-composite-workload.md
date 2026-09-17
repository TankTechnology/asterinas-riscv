# Firefox composite workload qualification

## Scope

This milestone adds a deterministic, phase-labelled Firefox workload for
finding repeated kernel cost classes. It is measurement infrastructure, not a
claim that page faults, SMP synchronization, software rendering, or Firefox
performance are fully optimized.

The workload uses one Firefox session and one exact local fixture origin. Its
ordered phases are `warmup`, `interaction-layout`, `canvas-image`,
`concurrent-resources`, `navigation-history`, `multi-context`, and `cooldown`.
The resource phase distinguishes a cold `no-store` pass from a cache-eligible
fill and repeated read of the same URLs. Navigation uses exact local pages and
actual history back/forward operations. Multi-context work is capped at three
iframes and verifies cleanup before the terminal snapshot.

## Evidence contract

Three fixed modes are available:

| Mode | Scale | Deadline | Intended use |
| --- | ---: | ---: | --- |
| `smoke` | 1 | 30 s | host/QEMU regression |
| `profile` | 4 | 120 s | repeated physical attribution |
| `stress` | 12 | 300 s | bounded tail/stability investigation |

The capture validates an append-only phase protocol and writes an atomic
checkpoint after phase progress. In parallel it samples Firefox and Xorg CPU
ticks, minor/major faults, RSS, optional `MemAvailable`, system CPU/load, and
Firefox per-thread `schedstat`. Firefox and Xorg PID start times must remain
stable. The collector closes only its transport socket; it does not send
`WebDriver:DeleteSession`, restart Firefox, reboot the guest, or rewrite the
root partition.

Interpret the evidence by clock domain:

- phase durations and rAF samples are browser-monotonic synthetic evidence;
- CPU, fault, memory, runnable-wait and dispatch intervals are guest-monotonic
  procfs evidence;
- USB input through physical HDMI scanout is not measured by either clock.

Missing compatible procfs fields are unsupported, not zero. The known Firefox
negative `fetchStart` issue is retained as invalid navigation evidence and is
not clamped. Public websites are final acceptance inputs, not attribution
workloads.

## Host qualification

The implementation was developed on `codex/firefox-performance-m2` in these
reviewable commits:

- `3b0829a7c` defines the strict workload snapshot protocol;
- `d49747469` adds the deterministic local fixture workload;
- `681dd4ea2` adds process fault/RSS and memory evidence;
- `84d6eb5c0` adds one-session capture and atomic checkpoints;
- `c36dc4628` packages the tools into Stage1 and the development overlay;
- `bc4db3a3f` makes cache reuse and history operations explicit;
- `bd77ff592` packages the input-identity helper required by older frozen roots.
- `ee99b42ef` rejects a document identity change during capture;
- `26d5921c9` gives page setup and the bounded workload separate deadlines.
- `31dfb0e53` explicitly selects the verified Firefox window before navigation.
- `a6b23ecbf` makes history completion observable and retains failure detail;
- `2a5a2362b` creates deterministic same-document history entries.
- `80f9894dd` waits for in-flight fixture handlers before publishing evidence.

The final `tools/riscv/firefox_fast_check.sh` run passed 253 tests and emitted
`FIREFOX_FAST_CHECK_PASS`. The focused protocol, fixture, and
development-overlay run also passed 45 tests; it is a subset and is not added
to the 252-test total.

The final deterministic build products are:

| Artifact | SHA-256 |
| --- | --- |
| Stage1 initramfs | `7469602695184417dd6a62bd4ea29e97f750f759899a1cddfca0efd814e5ec2b` |
| browser-web development ext2 | `b1c0da81c34a44866112fa225490845d3ac8abcafc5d00502c5baee588fb27ad` |
| Sv39/SMP=4 kernel used by QEMU | `5444c9eb40e10d26278affb00f69bb8c212ce94091204cf94a6209899d2f588c` |
| four-hart QEMU DTB | `fbf606c939b855f06a11d66f3206be46552ba7ba0fe58240231d3b50a891aef8` |
| QEMU U-Boot | `ff9abcc4d04ecae76645ddd8f42a61d314938a66f98e91878faedb2633d76b62` |

Both workload Python tools and `/usr/lib/asterinas/desktop-input-identity` are
present in the resulting artifacts with executable mode. The development
overlay path performs no package download or debootstrap rebuild.

## QEMU qualification history

The first four-hart browser-web attempt used Stage1
`b0a18932a6132992e203a7404fe467e66a05434fe6b2c448eb0c844f1f5ec51d`
and overlay
`a0fe4b4c05cdf4ab7982238d7d5717171d4a592e031dc028b7e3b6bebb18a803`.
All ten network layers passed, but Firefox never reached readiness and the
bounded gate ended after 900 seconds with `browser-timeout:firefox-launch`.
The serial log repeatedly showed that the frozen root lacked
`/usr/lib/asterinas/desktop-input-identity`; therefore this was a deployment
overlay defect before the composite workload ran, not a workload or kernel
performance result. Commit `bd77ff592` fixes that exact omission.

The rebuilt standard browser-web run then reached Firefox PID 159, established
Marionette, completed the Baidu-home probe, and entered the local fixture. It
failed the older broad capability gate because that Firefox build reported
`false-capability:wasm`. This proves the input-helper deployment repair but is
not a composite result: no workload resource request occurred. The dedicated
local smoke path therefore masks the external evidence service, starts the same
Firefox unit after Xorg readiness, and invokes the composite collector directly.

The first dedicated local attempt exposed a gate-control issue before Firefox:
the general physical-input helper tried to restart an already active desktop
unit and returned status 127. The second started Firefox directly and reached
Marionette, then exposed a capture-budget bug: the 30-second smoke budget was
being consumed by QEMU page setup while Firefox still reported
`about:interactive`. Commit `26d5921c9` preserves the 30-second workload bound
and adds a separate 120-second document-setup bound.

## Four-hart local QEMU smoke result

The final local run used the hashes above, manifest
`26e0a77f07d7627b3eaece213e6a706cdd2e64f411e74f77e4167760b6257959`,
and a four-hart Sv39 QEMU guest. It completed all seven ordered phases and
published both `ASTERINAS_BROWSER_COMPOSITE_PASS mode=smoke phases=7` and a
passing host result. The writable run-root hash was
`9ba0593ca20fad6a855eb2ea91333c718a6bfdb611cd5f105ad933541c1e38d6`.
Firefox PID 222 and Xorg PID 164 retained start-time identities 1060 and 724.

| Phase | Browser duration | Key bounded counts |
| --- | ---: | --- |
| warm-up | 123 ms | 128 operations, 1 request |
| interaction/layout | 676 ms | 528 operations, 3 of 8 rAF samples over 50 ms |
| canvas/image | 447 ms | 200 operations, 4 images |
| concurrent resources | 1179 ms | 24 fetch attempts |
| navigation/history | 1077 ms | 8 operations, 2 document requests |
| multi-context | 976 ms | 2 contexts, 2 resource requests |
| cooldown | 18 ms | zero remaining contexts |

The fixture observed 23 workload network requests: 17 binary resources, four
images, and two context resources. The browser attempted 24 fetches in the
concurrent phase, while the fixture saw only the eight cold and eight initial
warm binary requests; the repeated warm URLs were served from Firefox cache.
The request ledger was not truncated. QEMU serialized the observed requests
(`workload_max_active=1`), which is a follow-up hypothesis rather than a
physical-kernel conclusion.

The 15.125-second system sample recorded 14.97 s user and 3.77 s kernel CPU for
Firefox, 330 ms user and 70 ms kernel CPU for Xorg, average aggregate CPU busy
fraction 42.5%, average runnable count after each interval 2.53, and 25,606
context switches. Firefox RSS moved from 74,568 to 74,881 pages and peaked at
76,044 pages. `MemAvailable` moved from 1,457,968 to 1,443,724 KiB, with a
1,435,384 KiB minimum. The serialized minor and major fault deltas were both
zero, but current Asterinas `/proc/<pid>/stat` explicitly implements those
fields as placeholders. They are therefore unsupported evidence, not proof of
zero faults or low fault cost.

The 17.285-second thread ledger recorded 20.872 s Firefox thread runtime,
20.000 s runnable wait, and 15,387 dispatches. The main thread contributed
5.052 s runtime, 6.293 s wait, and 1,950 dispatches. The ledger explicitly
reports `thread-churn` and `thread-affinity-unavailable`; physical HDMI scanout
is unsupported. These high waits are useful evidence that system-emulated QEMU
is not a physical performance baseline.

The retained artifacts and SHA-256 identities are:

| Artifact | SHA-256 |
| --- | --- |
| `browser-composite-capture.json` | `b49b251e7e9f7b77cd3a6a4f85ddf970d692f16a995ea47b84a4af69074520bb` |
| `browser-composite-checkpoint.json` | `238ffa64f3138ead2ee9784f7b170714833fcdf2340dad0249c056d5d82224b6` |
| `browser-system-time.json` | `f09e12f5c7774123e0a6fbc59e893112c78396c42aa0c765db42dd6ed6835b40` |
| `browser-thread-time.json` | `a943a11ecd0498cf0340e1a7cb808bacc041492e9815bb86775862bf7185417c` |

This qualifies the local `smoke` path. It is not a physical performance result
and does not select a kernel optimization by itself.

## Physical attribution procedure

Use one stable physical boot and run three `profile` captures into distinct
empty directories. Verify Firefox/Xorg start times and zero leaked extra
contexts between runs. For each phase compare duration, Firefox user/kernel
CPU, minor/major-fault growth, RSS, system memory, total/main-thread runnable
wait and dispatches, CPU busy fractions, and fixture concurrency. A candidate
bottleneck must recur in at least two of three runs and exceed the relevant
sampling resolution before a focused kernel A/B change is justified.

The earlier evidence remains the boundary: the fault-around correctness bug
and RISC-V SSIP acknowledgement race are fixed, but general page-fault and SMP
costs are not declared solved. Previous ten-second Firefox samples showed only
269--326 ms total runnable wait and a roughly 1.3% median global/local I-cache
flush difference with overlapping runs. Those results do not identify scheduler
starvation or RFENCE as the dominant bottleneck, and this workload intentionally
collects evidence before changing either subsystem.

## Three-run physical profile result

One Megrez boot used the Stage1 and kernel hashes listed above and physical DTB
`465cb129333c2f3d5898af09687044184e0ca73269dcfbbe85d9c1bc9806aef4`.
Firefox PID 78 and Xorg PID 98 retained start-time identities 2483 and 2548
before, between, and after all three runs. Each run completed all seven phases
and emitted `ASTERINAS_BROWSER_COMPOSITE_PASS mode=profile phases=7`; no extra
context survived cooldown. The experiment did not rewrite the root partition.
After evidence collection the synchronized reboot completed, the original
extlinux configuration hash
`5af16fb2040c1bc10ddb4d1044caa44b5f8f45c019703b139234eeb4c6ab5fcd`
was restored, and the board was left running RockOS.

Exact browser-monotonic phase durations were:

| Phase | Run 1 | Run 2 | Run 3 |
| --- | ---: | ---: | ---: |
| warm-up | 1764 ms | 463 ms | 403 ms |
| interaction/layout | 4835 ms | 5041 ms | 4966 ms |
| canvas/image | 670 ms | 603 ms | 613 ms |
| concurrent resources | 2468 ms | 2129 ms | 2198 ms |
| navigation/history | 2396 ms | 994 ms | 1017 ms |
| multi-context | 1159 ms | 1026 ms | 939 ms |
| cooldown | 134 ms | 122 ms | 121 ms |

Run 1 includes first-use cache and history cost. Runs 2 and 3 are the stable
repeated comparison: their complete active windows were 10,176 and 10,178 ms.
The interaction/layout phase remained approximately five seconds in all three
runs. Its 32 frame samples had median durations of 40, 42, and 40 ms; p95 was
56, 125, and 130 ms; maxima were 165, 202, and 166 ms. This confirms a repeated
software-rendered interaction cost rather than a one-run network outlier.

The following additive phase values are apportioned by overlap with the
one-second procfs intervals. They are attribution estimates at that resolution,
not browser-clock measurements. `FF CPU` is Firefox user plus kernel CPU;
`all wait` is summed runnable wait across Firefox threads; `main wait` is TID
78 only; and `busy` is aggregate system CPU busy fraction.

| Phase | FF CPU, runs 1/2/3 | All wait, runs 1/2/3 | Main wait, runs 1/2/3 | Busy, runs 1/2/3 |
| --- | ---: | ---: | ---: | ---: |
| warm-up | 2109/595/402 ms | 3208/619/481 ms | 535/37/45 ms | 54/60/50% |
| interaction/layout | 4520/4283/4008 ms | 3028/3761/3168 ms | 155/221/159 ms | 53/49/48% |
| canvas/image | 964/752/846 ms | 706/434/419 ms | 71/64/62 ms | 58/55/55% |
| concurrent resources | 3548/2820/2900 ms | 2029/1889/1932 ms | 239/277/272 ms | 58/58/56% |
| navigation/history | 3273/1461/1614 ms | 2731/1430/1521 ms | 300/152/179 ms | 56/64/66% |
| multi-context | 1361/1012/1121 ms | 1028/1120/1139 ms | 75/111/122 ms | 53/48/60% |

The complete active windows provide the less granular cross-run comparison:

| Metric | Run 1 | Run 2 | Run 3 |
| --- | ---: | ---: | ---: |
| Firefox user/kernel CPU | 9735/6116 ms | 7779/3104 ms | 7615/3292 ms |
| Xorg user/kernel CPU | 246/43 ms | 216/33 ms | 216/41 ms |
| Aggregate CPU busy | 54.6% | 53.2% | 53.1% |
| Per-core busy, CPU 0/1/2/3 | 44.7/70.4/70.0/33.0% | 53.7/78.7/58.0/22.3% | 53.7/77.6/57.3/23.6% |
| Average runnable count | 2.11 | 3.01 | 2.80 |
| Context switches | 36,610 | 31,060 | 30,074 |
| Firefox thread runtime/wait | 15,632/12,775 ms | 10,828/9199 ms | 10,769/8686 ms |
| Firefox thread dispatches | 17,164 | 14,231 | 13,809 |
| Main-thread runtime/wait | 7241/1378 ms | 4437/857 ms | 4305/840 ms |
| Main-thread dispatches | 1950 | 1404 | 1326 |
| Firefox RSS range | 70,027--72,832 pages | 72,457--72,812 pages | 72,753--73,262 pages |
| Minimum `MemAvailable` | 9,630,628 KiB | 9,633,136 KiB | 9,619,248 KiB |

The repeated wait is not concentrated in the main thread. The largest named
wait contributor in every run was `glean.dispatche` at 3.144, 2.730, and
2.602 seconds, followed by compositor, WebRender, IPC, renderer, and other
Firefox workers. During interaction/layout, the main thread waited only
155--221 ms of an approximately five-second phase. The evidence therefore does
not support main-thread runqueue starvation as the dominant interaction
bottleneck. CPU 1 was the busiest core over the stable runs, while individual
resource and navigation intervals drove one core to 86--99%; the workload is
using multiple cores but still has repeated worker wakeup and software-render
cost.

The fixture recorded 188 workload requests without truncation: 99 cold binary
resources, 32 warm binary resources, 48 images, and nine context resources.
Only the first run fetched the 32 cache-eligible warm resources; runs 2 and 3
reused Firefox cache. Maximum fixture concurrency remained one in all runs.
This is a repeatable follow-up for the network/browser connection path, but the
fixture ledger alone cannot assign it to the kernel.

Current Asterinas source marks the `minflt`, `cminflt`, `majflt`, and `cmajflt`
fields in `kernel/src/fs/fs_impls/procfs/pid/task/stat.rs` as untracked
placeholders and emits zero. Consequently, the zero fault deltas in all 12
JSON artifacts do **not** answer whether the earlier fault-around repair made
Firefox fault handling cheap. Accurate per-process fault accounting is a
prerequisite for that A/B experiment.

The retained physical artifact hashes are:

| Run | Capture | Checkpoint | System | Thread |
| --- | --- | --- | --- | --- |
| 1 | `80f40d48c4d14e0df0b38627b9dca233e0c42afd20a836679bc90bd4102b93a1` | `b7ddc8eda72d037b3c9c655794b06a3031e6a7dff7cc52317a3941148d38964e` | `257f8a3332d5fb0c7dc6c88a52e48660d87f0b8b7f0298bc962a8dae170d93b0` | `9bfc1c40c335754fa93723943fb9e15fd9037ba6dea5ed2eed748130daaed3a8` |
| 2 | `e92374c849660a7afec9d214d6514e564b9002b8985aa196ae8508f142fbbb24` | `1f675f33dd2e6135283e4c6921bca9213aeacd512f7291c5591263d354bbc231` | `611cf12248758d69a8b014f88a20d32888a9ff5ce461c300c17655d7067ef130` | `da5db2a891a53b0cb5ce7342175889118e2172ad803dac5ba01214d275dd13ab` |
| 3 | `62671459c6229696da9162654d91dfeccc565cedc08351796128c9a3f6236eb3` | `b5dc8e424ac0fb30f5a821f0ac4cf68e52cb2ae3ea97f0920c65bd8afed32bbe` | `c809b49c58a99a6230628c3514cc4774a664976a749df244362ad516a049728e` | `e33f7061eb45d3913df3ded4607d44cc1f4a582099e7491a90fdaf01cb968610` |

The next focused experiment should first disable Firefox telemetry for an A/B
run, because `glean.dispatche` is the largest repeatable wait source. If the
five-second interaction wall time and 40--42 ms median frames remain, the next
slice is software-compositor/WebRender scheduling rather than a broad kernel
scheduler change. Page-fault optimization remains blocked on real accounting,
not declared complete. No VM, scheduler, locking, TLB, or page-cache behavior
is changed by this qualification commit.
