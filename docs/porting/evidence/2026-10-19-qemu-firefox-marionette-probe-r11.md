# Firefox Marionette short probe r11

r11 enabled `asterinas.syscall_diag=1` for the Probe boot and raised the process/child snapshot limit to 64 KiB so diagnostic history is not truncated.

## Runtime boundary

```text
FIREFOX_DRM_PROBE_BOUNDARY name=greeting status=pass
FIREFOX_DRM_PROBE_BOUNDARY name=new-session status=fail type=TimeoutError
FIREFOX_DRM_PROBE_SYSCALL pid=125 sequence=31769,number=98,entered_jiffies=255785
FIREFOX_DRM_PROBE_CHILD pid=397 name=Socket Process syscall=sequence=1012,number=73,entered_jiffies=185527
FIREFOX_DRM_PROBE_CHILD pid=416 name=WebExtensions syscall=sequence=5089,number=73,entered_jiffies=333278
FIREFOX_DRM_PROBE_CHILD pid=450 name=file:// Content syscall=sequence=2671,number=73,entered_jiffies=255284
```

On RISC-V generic syscall numbering, 98 is `futex` and 73 is `ppoll` (confirmed by `kernel/src/syscall/arch/generic.rs`). The parent is blocked in an active futex wait while all three direct children are blocked in ppoll. This is the first valid syscall identity for the NewSession stall; it rules out a missing Marionette TCP connection and narrows the next investigation to Firefox's renderer handshake plus Asterinas futex/ppoll wake paths. It does not yet prove which wake event is missing.

Firefox's browser log still reports `NewRenderer::Run is slow`, and no scene, screenshot, or recovery step was attempted.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| `desktop-m4-drm.serial.log` | `451bdf29ebf63d4f39b3b4025dfd8e4825e95569ef823642cbd5f88be3b2b2af` |
| `result.json` | `7e01255e29ff3176cddd3adc3754a734c57aa0de83be8bd19f8f3d14190219d8` |
| `lifecycle.json` | `e7432f7d3f881491f7ecc7331bd7f43ff454d9ff3d7c8769f29a055e88eec817` |
| guest probe receipt | `4bb048a88bfe7ce613ca979e37e7a06fd8e70602add88201ce296e3b2151c2b3` |
| rootfs | `ea0188ed960d63ac5570e0dfb6d86c855d073c92460467a27945c06acc2a1c7b` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r11-container/`
