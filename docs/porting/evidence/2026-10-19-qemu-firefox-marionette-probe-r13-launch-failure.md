# Firefox Marionette short probe r13 launch failure

r13 used the bounded futex wake-pairing instrumentation. It did not reach Asterinas userspace: the serial trace ends at `Starting kernel ...` after U-Boot loaded the kernel, DTB, Stage1 and bootargs.

- lifecycle: `phase=finished`, `status=failed`, `reason=protocol`
- no `ASTERINAS_BOOT_PHASE`, desktop, Firefox greeting, or futex marker was emitted
- no Firefox acceptance data was produced

This is a launch/protocol failure, not evidence about Firefox or the futex wake path. No retry is scheduled.

| Artifact | SHA-256 |
|---|---|
| `desktop-m4-drm.serial.log` | `e2dc4b00f30e422a152949376e170ac20ea251e87e89be606f9055fb1c572281` |
| `lifecycle.json` | `15e7e7f515a35b1abb80016542c725bcaa6251f7f5a1cd87c6cdf49c6c0a4d2d` |

Current kernel SHA-256: `abeeb0faa1beb9b7ef4de1aee0b5be265c77f9dd1f65ea07e11aa6586952554d`
U-Boot SHA-256: `18c34ed87dd053ad012a0d3f8acef7445d40b0199c4da54ec04ba8cbba841710`
DTB SHA-256: `d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c2`
Stage1 SHA-256: `ce7fb996e9a6ac48160d81c29347a68d88e99204adcc147051df76f645031c84`
Rootfs SHA-256: `ffa0e4f685b26c22fff5ff683d924ec89450cccfb9298c3fdd5e8102b28eaa3a`
Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r13-container/`
