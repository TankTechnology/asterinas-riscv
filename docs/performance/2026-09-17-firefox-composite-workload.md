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

The capture validates exact per-mode work counts and an immutable completed
phase prefix, then writes an atomic checkpoint after phase progress. Both
procfs samplers complete an initial snapshot before the workload starts and a
terminal snapshot after it ends; artifacts that do not cover the complete
workload interval are rejected. In parallel it samples Firefox and Xorg CPU
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

The final `tools/riscv/firefox_fast_check.sh` run passed 273 tests and emitted
`FIREFOX_FAST_CHECK_PASS`. A separate focused protocol/security run passed 79
tests, and the final independent review found no blocker for v9 qualification.

The final deterministic build products are:

| Artifact | SHA-256 |
| --- | --- |
| Stage1 initramfs | `988b562b735997490c14b87e0c982164f18d4c45fec82b8f7b57a612f40a19d1` |
| browser-web development ext2 | `9ff81f7e6e9abe5ccef0b7d49190e1a4cb3d7d56fada906277d0a3dce5982671` |
| browser-web root manifest | `486a678ba7e884e839f4c89a61c412f5b42fc7fd134a9c31a646f6d9e98a7946` |
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

## Initial four-hart local QEMU smoke result

The initial local run used Stage1
`7469602695184417dd6a62bd4ea29e97f750f759899a1cddfca0efd814e5ec2b`,
development ext2
`b1c0da81c34a44866112fa225490845d3ac8abcafc5d00502c5baee588fb27ad`,
the unchanged kernel/DTB/U-Boot hashes above, manifest
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

## Post-review QEMU requalification

The then-current source was repackaged into Stage1
`a21514ceaafb8b8a00f5a5930c95e0d9968b4cad8755de9426d6a150c6ddaf89`
and development ext2
`c38de4ec7df318759faee75b689d63c9e2b83be8d8e3409b26a98f61b9567ea3`,
without rebuilding Debian or downloading packages.
The first boot reached Xorg, Firefox PID 223, and Marionette, but Python then
reported that `/usr/lib/python3.13/ipaddress.py` contained NUL bytes before the
workload began. Offline extraction from both the immutable input image and the
failed run image produced the same valid 81,635-byte file with zero NUL bytes
and SHA-256
`9c6bb01db0ed80900671b0ffc853eacda6131b9bdeee95e6882a71e2801840fa`.
This is a transient guest read/page-cache stability signal, not a workload
result; the failed transcript is retained in
`target/firefox-composite-workload-v7/qemu-smoke/`.

An unchanged retry in `target/firefox-composite-workload-v7/qemu-smoke-v2/`
passed all seven exact phases. Firefox PID 222 and Xorg PID 164 retained start
times 1141 and 810. The fixture accepted the exact 23-request smoke ledger with
no truncation. The system sampler covered guest monotonic
42,453,112,700--46,950,913,900 ns and the thread sampler covered
42,461,014,000--46,951,341,100 ns; both contain the complete observed workload
window of 42,557,490,400--46,938,751,100 ns. This directly verifies the new
start handshake, terminal stop, and coverage rejection contract.

| Phase | Browser duration |
| --- | ---: |
| warm-up | 429 ms |
| interaction/layout | 413 ms |
| canvas/image | 389 ms |
| concurrent resources | 806 ms |
| navigation/history | 1012 ms |
| multi-context | 1065 ms |
| cooldown | 25 ms |

The post-review artifact hashes are:

| Artifact | SHA-256 |
| --- | --- |
| `browser-composite-capture.json` | `925d7093faed003b3531524afab121df45205571d4ac41da0c5bf917d07cff6d` |
| `browser-composite-checkpoint.json` | `ed72d8bb810d99465fc1eedad3f7a182c906da2a91655336dd27f5538c6df337` |
| `browser-system-time.json` | `64d01abce2b0984a325575f1db1036fdd91f5190f3be5d63817a35f355d3a769` |
| `browser-thread-time.json` | `313b0d30e2a0757a23a422c8e1e856a926265f37529b0c3c85c81a1814740e82` |
| `result.json` | `1046e45157bfdd10a4f9b085a57926198ea541dd133d0c0255d00b51eb1a4620` |

The final v8 package was then rebuilt after adding the evidence-manifest
validator and requalified in
`target/firefox-composite-workload-v8/qemu-smoke/`. The fixture again accepted
exactly 23 requests. The system sampler covered guest monotonic
38,909,647,300--42,699,017,300 ns and the thread sampler covered
38,910,672,300--42,699,505,600 ns; both contain the complete workload window
39,012,602,000--42,693,752,600 ns. The retained artifact hashes are:

| Artifact | SHA-256 |
| --- | --- |
| `browser-composite-capture.json` | `e1398640ac920b7f7abb70a2476736dc626c992750f5e6fed4771f8309222aae` |
| `browser-composite-checkpoint.json` | `7fa13b224693034c5ef255c73dfcc30cc34c7768493b8e4ac3759c85d237dbb7` |
| `browser-system-time.json` | `a180c85d3e7f6e6654a10fa184dcf1d2a236a8af3e8eed722739853f11b4d38f` |
| `browser-thread-time.json` | `6bc67ca75043a252cf22796c8b3df1dbcece5248222af20f4163803b3139082c` |
| `result.json` | `ab0f04cced140da65b7b3636583253e736d74aab90024bcc0601d4ba3f98b657` |

That v8 result remains useful qualification history, but its URL space was
shared between repeated captures. It is superseded for physical attribution by
v9, which gives every capture a random 128-bit run ID and binds that ID into
every workload URL, snapshot, checkpoint, fixture record, and manifest entry.
Each profile run now has an independently cold URL namespace.

## Final nonce-bound QEMU qualification

The v9 QEMU smoke used the final Stage1, development ext2, kernel, DTB, and
U-Boot identities listed above. Firefox PID 223 and Xorg PID 164 retained start
times 1052 and 719. Run ID
`a3b53bb96852d5d050071dfaccffbcfe` completed all seven phases:

| Phase | Browser duration |
| --- | ---: |
| warm-up | 221 ms |
| interaction/layout | 524 ms |
| canvas/image | 337 ms |
| concurrent resources | 751 ms |
| navigation/history | 844 ms |
| multi-context | 771 ms |
| cooldown | 25 ms |

The fixture accepted exactly 23 requests without truncation. Its observed
maximum concurrency was one. The workload interval was
38,022,162,800--41,914,039,200 ns; the system sampler covered
37,943,018,600--41,918,480,000 ns and the thread sampler covered
37,952,066,500--41,918,874,400 ns. Both samplers contain the entire workload
interval. Manifest creation and a separate verification pass both succeeded.
The retained evidence is in
[`evidence/firefox-composite-v9/qemu/`](evidence/firefox-composite-v9/qemu/).

| Artifact | SHA-256 |
| --- | --- |
| `browser-composite-capture.json` | `6288643784400f1494c64bc68aa91086acf958f96a52cb5c8ea494ef2561f6e2` |
| `browser-composite-checkpoint.json` | `892d0a62f0b4897b073576a0d98fe65582dec6a7313f326886c00068dfcab5a2` |
| `browser-system-time.json` | `5c0d451991210f6ebcca9581905d32b579d8e672a4b82de1cf4b702d5233cea7` |
| `browser-thread-time.json` | `87d0f3b9f3cc36f8bce141fd2f3239222ef371c956d7644c90a5d250a56cdd7e` |
| `evidence-manifest.json` | `e962850cbd2dd87172bda29a47a40a49ae6cd7b27e874a5349e20b79458fd598` |
| `network-fixture.json` | `1a8138a22524050b4985ba4a210d0b2f0d6af93bf99c6ef1159c2e9e963e34dd` |
| `result.json` | `b28809dbefc268cff50c750fcce4dcaa1259b7173aae0e920e4e72eb81153123` |

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

## Superseded v8 three-run physical profile

This section records the v8 diagnostic history. Its shared URL namespace let
runs 2 and 3 reuse run 1's cache, so its request totals and thread ranking must
not be used as the final bottleneck attribution. The nonce-bound v9 result in
the next section replaces it.

One Megrez boot used Stage1
`f9148e929c451383767ac2a3fbc0b5cb574eda09668ce517c06ebe13616fc404`,
the kernel hash listed above, and physical DTB
`465cb129333c2f3d5898af09687044184e0ca73269dcfbbe85d9c1bc9806aef4`.
Firefox PID 73 and Xorg PID 99 retained start-time identities 549 and 704
before, between, and after all three runs. Each run completed all seven phases
and emitted `ASTERINAS_BROWSER_COMPOSITE_PASS mode=profile phases=7`; no extra
context survived cooldown. The experiment did not rewrite the root partition.
The original `/boot/extlinux/extlinux.conf` and
`/boot/extlinux/asterinas.conf` were not modified; their post-test SHA-256
values remained
`eb5f39a6e2db71ccc93ae005c488fd9f7ef353e75426e5a51e8923a9cbf2ebc5`
and `5af16fb2040c1bc10ddb4d1044caa44b5f8f45c019703b139234eeb4c6ab5fcd`.
After the synchronized diagnostic reboot, the board was explicitly returned to
RockOS and verified over SSH as Linux 6.6.87 on RISC-V.

Exact browser-monotonic phase durations were:

| Phase | Run 1 | Run 2 | Run 3 |
| --- | ---: | ---: | ---: |
| warm-up | 657 ms | 364 ms | 367 ms |
| interaction/layout | 5968 ms | 4691 ms | 4851 ms |
| canvas/image | 1417 ms | 669 ms | 618 ms |
| concurrent resources | 2546 ms | 2092 ms | 2125 ms |
| navigation/history | 2423 ms | 1240 ms | 1392 ms |
| multi-context | 814 ms | 854 ms | 726 ms |
| cooldown | 112 ms | 97 ms | 114 ms |

Run 1 includes first-use cache and history cost. Runs 2 and 3 are the stable
repeated comparison: their complete guest-observed active windows were 10,098
and 10,350 ms. The interaction/layout phase remained approximately five
seconds in all three runs. Its 32 frame samples had median durations of 42.5,
38.5, and 44 ms; p95 was 310, 112, and 124 ms; maxima were 440, 114, and
128 ms. This confirms a repeated
interaction/layout and frame-delay cost rather than a one-run network outlier;
these counters do not by themselves split JavaScript, layout, painting, and
compositor time.

The procfs sampling interval is two seconds. The following complete-window
values apportion only boundary intervals by guest-monotonic overlap. They are
attribution estimates at that resolution, not browser-clock measurements:

| Metric | Run 1 | Run 2 | Run 3 |
| --- | ---: | ---: | ---: |
| Firefox user/kernel CPU | 10,571/7211 ms | 7620/3106 ms | 7432/3168 ms |
| Xorg user/kernel CPU | 217/39 ms | 223/30 ms | 216/30 ms |
| Aggregate CPU busy | 55.9% | 50.0% | 48.9% |
| Per-core busy, CPU 0/1/2/3 | 87.3/74.5/39.2/22.6% | 60.6/82.5/41.3/15.5% | 54.8/87.7/37.9/15.0% |
| Average runnable count | 2.87 | 2.23 | 2.24 |
| Context switches | 38,130 | 29,298 | 28,762 |
| Firefox thread runtime/wait | 17,660/16,986 ms | 10,694/10,067 ms | 10,576/9896 ms |
| Firefox thread dispatches | 18,652 | 14,367 | 14,349 |
| Main-thread runtime/wait | 9497/2311 ms | 4739/693 ms | 4402/532 ms |
| Main-thread dispatches | 2095 | 1356 | 1217 |
| Firefox RSS range | 71,379--74,648 pages | 73,279--74,074 pages | 72,789--72,980 pages |
| Minimum `MemAvailable` | 9,634,140 KiB | 9,626,336 KiB | 9,623,352 KiB |

The repeated wait is not concentrated in the main thread. The largest named
wait contributor in every run was `glean.dispatche` at 4.292, 3.416, and
3.819 seconds. `IPDL Background` followed at 2.785, 1.633, and 1.586 seconds;
the compositor, WebRender backend, software-rendering worker, and renderer also
recurred. Across the stable runs the Firefox main thread waited only 0.53--0.69
seconds, while aggregate CPU busy remained 49--50%. The evidence therefore
does not support global CPU saturation or main-thread runqueue starvation as
the dominant bottleneck. CPU 1 was the busiest core and CPU 3 was lightly used;
the evidence does not yet assign the frame delay to one renderer/compositor or
kernel wakeup path.

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

All 12 guest JSON artifacts and the exact host fixture ledger are retained
under `target/firefox-composite-physical-final-v8/evidence/`. The deterministic
manifest binds their canonical paths, byte sizes, SHA-256 identities, process
identity, sampler coverage, and exact per-run fixture record slices. Independent
creation and verification both passed; the manifest SHA-256 is
`28ffdeb27612fabe0cc89a123a9742a2fcb6c991a0122c863ff01b57f1f3cc0e`
and the fixture SHA-256 is
`48cd78567295ce06988a65f9f76eec30f152a08d31f136f4cf50332a2250fb1e`.

| Run | Capture | Checkpoint | System | Thread |
| --- | --- | --- | --- | --- |
| 1 | `98a09c08b6afc1733b943cf35c3b4c1fa88e992befc53d35a383eb549483b995` | `5d031c167d8c3b9f55fd5207b29613f814b8958c91e897f1872340c46a3db2bc` | `b8e7bf9c7aec50c33c81f809f0cb47f96a6ea48a8739f24306afc3e306a72c26` | `f6f42cc9d3c93ab21153e6f114223040471033438685a55401f92eced106aec1` |
| 2 | `3048ab76af2bb7e821094871fa79a04e3e9408df99ad101344089c9c2bda5e95` | `2ac9d816ec24051d9d89935d38e297fefc889425f6ec7d43297cef63640c468a` | `58f8be9e239525c13f8149ff20772a8acd5d11b8ba85204a59f7d9bf07e04ef2` | `698f253a54fd3b10812f12be2092defaf85460bb4000ea62c924f6adf0ac23e8` |
| 3 | `85ed1c57420b22864e04700c34fa53ba4726ea8d23a49e635450f3efa0820a32` | `85fa2580e57051c09198a1498fe42d840dabf47a54ed6761f7c0d0bf276094c7` | `376c2d10ca765b01d1764e872f12b77ed5c945d16fb4fb61164876cbee2afb03` | `c0c92a4a7b303fc55b490f791a78d1493f543904ec9f206d2b9bc25d2568d6c0` |

The next focused experiment should first disable Firefox telemetry for an A/B
run, because `glean.dispatche` is the largest repeatable wait source. If the
five-second interaction wall time and 38.5--44 ms median frames remain, the next
slice is software-compositor/WebRender scheduling rather than a broad kernel
scheduler change. Page-fault optimization remains blocked on real accounting,
not declared complete. No VM, scheduler, locking, TLB, or page-cache behavior
is changed by this qualification commit.

## Final nonce-bound three-run physical profile

One Megrez boot used Stage1
`988b562b735997490c14b87e0c982164f18d4c45fec82b8f7b57a612f40a19d1`,
the kernel hash listed above, and the already-installed physical root image.
Firefox PID 73 and Xorg PID 96 retained start-time identities 521 and 678
before, between, and after the accepted runs. Each run completed all seven
phases and emitted `ASTERINAS_BROWSER_COMPOSITE_PASS mode=profile phases=7`;
no extra context survived cooldown. The run reused partition 2 and did not
deploy or reimage the 2 GiB root filesystem. The original
`/boot/extlinux/extlinux.conf` and `/boot/extlinux/asterinas.conf` were not
modified; their post-test SHA-256 values remained
`eb5f39a6e2db71ccc93ae005c488fd9f7ef353e75426e5a51e8923a9cbf2ebc5`
and `5af16fb2040c1bc10ddb4d1044caa44b5f8f45c019703b139234eeb4c6ab5fcd`.
After the experiment, the board was explicitly returned to RockOS and verified
over SSH as Linux 6.6.87 on RISC-V.

One otherwise completed browser attempt was rejected before final evidence was
assembled because a procfs sampler did not cover the full workload interval.
The fixture was restarted to discard that attempt's ledger, and three new runs
were captured. This is an exercised quality gate, not a kernel crash or an
accepted performance observation.

Exact browser-monotonic phase durations and complete guest-observed windows
were:

| Phase | Run 1 | Run 2 | Run 3 |
| --- | ---: | ---: | ---: |
| warm-up | 360 ms | 358 ms | 354 ms |
| interaction/layout | 4680 ms | 4837 ms | 4690 ms |
| canvas/image | 652 ms | 671 ms | 659 ms |
| concurrent resources | 2695 ms | 2656 ms | 2613 ms |
| navigation/history | 1192 ms | 1056 ms | 1160 ms |
| multi-context | 845 ms | 914 ms | 913 ms |
| cooldown | 110 ms | 93 ms | 97 ms |
| observed workload window | 10,760.682 ms | 10,709.471 ms | 10,714.461 ms |

The interaction/layout phase is the largest stable component at 4.68--4.84
seconds, followed by concurrent resources at 2.61--2.70 seconds. This pattern
recurs under three cache-isolated run IDs, so it is not a first-run cache or
public-network outlier. The browser counters alone still cannot split
JavaScript, layout, painting, software rendering, and compositor costs.

The procfs sampling interval is two seconds. The following values use every
full covering interval and are deliberately not apportioned to sub-phase
browser timestamps:

| Metric | Run 1 | Run 2 | Run 3 |
| --- | ---: | ---: | ---: |
| Firefox user/kernel CPU | 7590/3180 ms | 7140/3120 ms | 7260/3090 ms |
| Xorg user/kernel CPU | 230/40 ms | 190/30 ms | 210/30 ms |
| Aggregate CPU busy | 48.22% | 46.69% | 47.29% |
| Average runnable count | 2.667 | 3.000 | 3.000 |
| Context switches | 29,326 | 29,078 | 30,200 |
| Firefox thread runtime/wait | 10,807/8569.990 ms | 10,265.604/8087.670 ms | 10,369.122/8627.747 ms |
| Firefox thread dispatches | 13,468 | 13,159 | 13,774 |
| Main-thread runtime/wait | 4243.267/1444.911 ms | 4163.463/1323.986 ms | 4214.757/1306.938 ms |
| Main-thread dispatches | 1340 | 1325 | 1401 |
| Minimum `MemAvailable` | 9,616,524 KiB | 9,603,568 KiB | 9,603,936 KiB |

Firefox RSS ranged from 72,888 to 73,129 pages in run 1, 73,049 to 73,868
pages in run 2, and 73,795 to 74,426 pages in run 3. The main thread retained
affinity to CPUs 0--3 in every run. Run 2 records `thread-churn`; the other two
runs have no thread-sampler limitation. Physical HDMI scanout remains
unsupported by this evidence contract.

Wait is distributed rather than dominated by one telemetry thread. Repeated
large contributors include `WRRenderBackend` (about 1.49--1.55 seconds), the
Firefox main thread (1.31--1.44 seconds), `IPDL Background` (1.23--1.39
seconds), `IPC I/O Parent` (1.27--1.30 seconds), `Socket Thread` (about 0.82
seconds), and `Compositor` (0.70--0.88 seconds). Aggregate CPU busy remained
below 49% while roughly three tasks were runnable. The evidence therefore does
not support global CPU saturation, one bad affinity mask, or one Firefox
telemetry thread as the dominant explanation. It narrows the next A/B work to
renderer/compositor/IPC wakeup latency and the serialized resource path.

The fixture recorded 252 requests without truncation: 84 for each independent
run ID. Each run fetched its own 32 cache-eligible warm resources, eliminating
the v8 cross-run cache reuse. Maximum observed fixture concurrency nevertheless
remained one in every run. That repeatable serialization is a browser/network
scheduling lead, but this ledger alone does not assign it to the kernel.

Current Asterinas source marks the `minflt`, `cminflt`, `majflt`, and `cmajflt`
fields in `kernel/src/fs/fs_impls/procfs/pid/task/stat.rs` as untracked
placeholders and emits zero. Consequently, the zero fault deltas in all 12
JSON artifacts do **not** answer whether the earlier fault-around repair made
Firefox fault handling cheap. Accurate per-process fault accounting remains a
prerequisite for that A/B experiment.

All 12 guest JSON artifacts and the exact host fixture ledger are retained in
[`evidence/firefox-composite-v9/physical/`](evidence/firefox-composite-v9/physical/).
The deterministic manifest binds canonical paths, byte sizes, SHA-256
identities, process identities, sampler coverage, run IDs, and exact per-run
fixture record slices. Independent creation and verification both passed. The
manifest SHA-256 is
`d4576eb1f8db479e4ab44f8c4b5b65216f09732da167faca3926219fd68dc8a4`
and the fixture SHA-256 is
`43f8ac460cc8a9ca92090476caf4504ef8c85d4aa62979480a52b598a7905d94`.

| Run | Run ID | Capture | Checkpoint | System | Thread |
| --- | --- | --- | --- | --- | --- |
| 1 | `2b2035ff669df7087862b5bf9145527b` | `5b0d867149eebc956bf224055e7a4eb5fcec69e545d7d9c4b3460c1c265127e8` | `a3b50815633d427ec87afa5ea215e0e4f71331190f020cbb9cd050ba882e9002` | `5a3871225d0590072729941891a8d1bba3132c010e7cb1518840abec806eea07` | `83b44459de0f6a27228bf3fe2c465b42fcd057f078e6cf34584e719124096c3f` |
| 2 | `85c04880e6903145c7433b8baa4965a4` | `48351c8a7acfd59b5b7f7260717a5a49792d3922d3b7d3728fb67d6d9a203da5` | `bc841a85c6da8db258bc1e878e8fec43ee77428aa6dc9ca47fa0ab287b855bdb` | `8ef8d315b2d284a5d764be1e9a1b7ea441d064a9cc0a6bc42a2c0336d93c8d9b` | `4e2617b5b11ea82d3a8eabef9764e747413d7ec3da16611ea8ae0175b4d1f3a9` |
| 3 | `8d730c8e3a6367f50b0cbf7178fd2642` | `81c2c691813aecc4317a7ba3e40a34a73382db7280ba55d5cb56b749f69c5c1e` | `768dd3a325972a25811850c4540b2f058df220c2abef1f97fb1edbf468902e92` | `31927de66ccbf17f14628348505a83d1b852ac6ccfbdac846e058c289013ea15` | `418cbdabd75f7ef305a855bce2ba8035acb1837de67ac938c9f604370097abff` |

This qualification commit changes measurement and validation infrastructure;
it does not yet change VM, scheduler, locking, TLB, page-cache, compositor, or
network behavior. It replaces a stale telemetry-thread hypothesis with a
reproducible target: measure renderer/compositor/IPC wakeups at finer resolution
and A/B the serialized resource path before selecting a kernel optimization.
The requested five-fold end-to-end acceleration is not claimed by this result.
