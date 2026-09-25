# Direct GEM-page copy into the firmware framebuffer

This is an interim software-display improvement, not native EIC7700
scanout or GPU acceleration. With `asterinas.drm_direct_copy=1`, the
firmware backend walks committed GEM pages and writes each page chunk
directly into the bootloader framebuffer, then performs the same device
synchronization as the original path. It removes the intermediate scratch
row and its extra memory copy. The default path is unchanged when the flag
is absent. The selected physical boot also used
`asterinas.drm_phase_profile=1` to sample up to four rows per present.

The RISC-V/Sv39/SMP4 release Image SHA-256 was
`1b03b433c17994c0143412eb95186e946077438cd2e337534e79c78a06411e12`
at commit `8d8c60e8d`. Its registered QEMU profile
`megrez-sv48-svade-drm-firmware-direct-copy` passed all six firmware-display
stages, including pixel checks for `SETCRTC`, `PAGE_FLIP`, and `DIRTYFB`.
That test exercises the flag and a 1920 × 1080 Megrez contract approximation;
it does not model EIC7700 DMA coherency.

The new candidate was published as separate, hash-verified files under
`/home/debian/asterinas/direct-copy-8d8c60e8d/` on RockOS partition 3.
U-Boot checked the loaded artifacts' byte counts and CRC32. The default
boot entry, prior candidate, Stage1, DTB, and root filesystem were left
untouched. The selected Asterinas boot ID was
`beced7f9-af44-4381-8c51-3afae824af6a`; the root serial console returned
UID 0 with that ID, and a closed/reopened connection verified the same ID.
Xorg reported 1920 × 1080, Firefox's service had `MainPID=125`,
`NRestarts=0`, and `SubState=running`, and the 420-second recovery timer was
disarmed after admission.

The session requested the same local Firefox URL and ten `PageDown` keys
used in the [phase profile](2026-09-26-megrez-firefox-phase-profile.md).
The before/after snapshots were 14 seconds apart. The window title remained
`Mozilla Firefox`, but the loaded URL, visible frames, and physical input
were not independently confirmed. Firefox CPU use differed greatly between
the two runs, so these data do **not** establish user-visible latency or a
browser-wide speedup.

| Counter | Original row copy | Direct page copy |
|---|---:|---:|
| Dirty presents in window | 105 | 107 |
| Dirty bytes in window | 359,235,144 | 346,828,828 |
| Dirty present time in window | 3,361,220,000 ns | 2,745,243,000 ns |
| Mean dirty present | 32.01 ms | 25.66 ms |
| Dirty present time per copied byte | 9.36 ns | 7.92 ns |
| Xorg all-thread CPU increase | 3,503,673,000 ns | 3,005,260,000 ns |

The roughly 15% lower time per copied byte suggests the extra scratch copy
was material, but the windows are short and the amount and type of Firefox
work may differ. The direct path still moves every changed pixel through the
CPU and remains much too slow for the target of at least 2× improvement in
trusted-input-to-visible-frame p95 latency. The next P1 milestone is a
DMA-safe, user-mappable display pool followed by fixed-mode scanout/flip;
the current write-back VMO mapping is not safe to hand to this non-coherent
display controller without a demonstrated synchronization protocol.

Raw local serial/counter evidence is at
`/home/ubuntu/.codex/asterinas-direct-copy-20260926/firefox-direct-copy-sample.txt`;
the QEMU gate result is under
`target/qemu-uboot/display-probe/direct-copy/evidence/`.
