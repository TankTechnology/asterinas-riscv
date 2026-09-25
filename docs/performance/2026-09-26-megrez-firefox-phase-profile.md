# Megrez Firefox display phase sample

This follow-up to the [P0 scanout observation](2026-09-25-megrez-firefox-display-p0.md)
used an opt-in, bounded timing probe in the firmware scanout copy path. The
probe samples up to four rows per present, timing the GEM VMO read and the
firmware-framebuffer write including its device synchronization separately.
It does not timestamp every row or claim that the sampled rates are exact
whole-frame rates.

The selected physical boot ran commit `1d10fb81d`, Image SHA-256
`90b0065a23402e9ea31083392b9379d71d43c6c33ce5b5e959521310df4be4cd`,
with `asterinas.drm_phase_profile=1`. Its boot ID was
`590f2b7a-35ab-4095-b9f9-702ae52f1e30`. The prepared DTB and Stage1
identities were unchanged from the [handoff diagnostic](2026-09-25-megrez-dc-handoff.md).
The new files were uploaded to RockOS partition 3 under
`/home/debian/asterinas/drm-phase-1d10fb81d/` with size and SHA-256 checks
before and after publication; U-Boot checked size and CRC32. The default
RockOS boot entry and prior candidate files remain available. Firefox and
1920 × 1080 Xorg started, the 420-second software recovery timer was
disarmed, and closing/reopening the serial port verified UID 0 on the same
boot. Firefox's service had `MainPID=128`, `NRestarts=0`, and
`SubState=running` after the sample.

The X11 session requested navigation to the local
`file:///usr/share/doc/firefox/copyright` page, waited three seconds, then
sent ten `PageDown` keys about 0.6 seconds apart and waited three seconds.
The snapshots were 15 seconds apart. The window title was `Mozilla Firefox`,
but the URL, visible frames, and physical input were not independently
confirmed. These counters therefore show work attribution, not trusted-input
latency or a browser speedup.

| Counter, before → after | Delta |
|---|---:|
| Dirty presents, 63 → 168 | 105 |
| Dirty bytes, 139,394,568 → 498,629,712 | 359,235,144 bytes |
| Dirty present time, 1,349,352,000 → 4,710,572,000 ns | 3,361,220,000 ns |
| Sampled rows, 199 → 515 | 316 |
| Sampled row bytes, 808,860 → 2,142,480 | 1,333,620 bytes |
| Sampled GEM read time, 4,751,000 → 12,646,000 ns | 7,895,000 ns |
| Sampled framebuffer write and sync, 4,158,000 → 10,996,000 ns | 6,838,000 ns |
| Xorg all-thread CPU run time | 3,503,673,000 ns |
| Firefox all-thread CPU run time | 3,917,151,000 ns |

The sampled row phases split 53.6% GEM reads and 46.4% framebuffer writes
including synchronization. The full dirty-present counter averaged 32.01 ms
per call. Its 3.361 s increase is close to Xorg's 3.504 s CPU increase,
which supports the scanout copy as the dominant Xorg cost in this window.
It does not prove that every Xorg cycle occurred inside the copy path, nor
that eliminating the copy will halve Firefox's own CPU use. Both the read
and write legs should be removed by P1 direct scanout; optimizing only one
leg has limited upside. A correct non-coherent DMA mapping and buffer
lifetime/flip contract remain prerequisites before any controller write.

The six-stage QEMU firmware-display gate passed with this Image on the
1920 × 1080 Megrez contract approximation. The raw short-run counters are in
`/home/ubuntu/.codex/asterinas-phase-profile-20260926/firefox-phase-sample.txt`.
