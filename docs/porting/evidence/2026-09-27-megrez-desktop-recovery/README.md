# Megrez desktop boot and recovery, 2026-09-27

This is a failed physical qualification, not a desktop or HDMI pass. The
working tree was `main` at `4009be3ba` during the board runs. The immutable
artifacts staged on RockOS partition 3 were:

| Artifact | SHA-256 |
| --- | --- |
| Release kernel | `8ba4b6cb9819a70671268065209f8b2400f67ffc0dbd59d62ad9d7fc584920ba` |
| Stage1 | `ea446515661f2380568426b3c37da0b6a0698fc51f88d32c62807fce04890962` |
| Prepared DTB | `465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba` |

The installed partition-2 root was derived from signed image
`d4f4e88fb20a8938e7270f4ceaf9c79855ba3d038acbd6f1030fe269dfca9bd4`.
It had mutable files from prior experiments, so that image hash is **not** a
claim about the current partition's bytes. The persistent boot selector hash
before and after the first boot was `02280720efe7a1ad…`; no selector was
changed.

The first menu attempt stopped in U-Boot with
`bootarg overflow 1175+0+0+1 > 1024`, before kernel entry. The desktop menu
publisher had accepted a 1175-byte argument line. Commit `c0eae412b` rejects
lines above the board's 1023-byte payload limit, adds the boundary regression,
and makes persistent HOME explicit in the desktop plan. Forty related unit
tests and Python compilation passed.

A direct boot with shortened arguments then reached the opt-in UID-0 debug
console. The desktop service looped with `display-provider-failed` because a
leftover partition-2 drop-in forced `ASTERINAS_DISPLAY_PROVIDER=drm`, while
the current runtime provider list contained only fbdev. A temporary `/run`
override to fbdev let Xorg, Firefox, Openbox, PCManFM, and LXPanel start.
That is process-level evidence only; no physical monitor image was captured.
The software timer returned this first boot to U-Boot, and a new RockOS boot
and root shell were observed. The stale drop-in was backed up byte-for-byte
to partition 3 and removed from partition 2 while it was unmounted otherwise;
its SHA-256 was
`a65f152b649a5dffbb9562f19a9d37781b9bfac23a126361437f97434323bfdd`.

The next direct boot used the same kernel, Stage1, and DTB, persistent HOME,
`console=ttyS0 loglevel=info`, and a 480-second software reboot deadline.
It entered the kernel and emitted `ASTERINAS_DEBUG_CONSOLE_READY uid=0` about
80.6 host seconds after starting the RockOS software reboot. The log contains
`ASTERINAS_SOFTWARE_REBOOT_ARMED`, but stops near guest uptime 5.7 seconds,
after network diagnostic lines. No desktop-ready marker or panic was seen.
A subsequent nonce command received no serial bytes; ICMP and ARP from the
host also failed. A passive serial listener saw no fresh OpenSBI output during
the full bounded recovery window. The guest's timer does not establish
recovery from this apparent hard hang. The board currently needs an operator
reset before further physical tests.

Local raw transcripts and JSON results are under
`.local-test/desktop-goal-20260927/`; they include the boot arguments and
artifact checks. The next run must first verify a fresh RockOS login, use a
quiet serial configuration, confirm nonce-framed root access after reconnect,
and perform a separate software-reboot check. A board-level watchdog is a
candidate for diagnostic hard-hang recovery, but its timeout and interaction
with a normal desktop session must be validated before enabling it there.
