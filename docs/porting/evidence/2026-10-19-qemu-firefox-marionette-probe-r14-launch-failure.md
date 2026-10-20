# Firefox Marionette short probe r14: launch boundary

## Boundary

This was one bounded probe-only launch with a 360-second host timeout.  It did
not enter Asterinas userspace, so it produced no Firefox greeting, NewSession,
process/thread, or guest probe receipt.  The serial transcript ends after
U-Boot prints `Starting kernel ...`; QEMU is then terminated by the host gate.

This is a launch/protocol failure and is **not** evidence that Firefox or
Marionette changed its NewSession behavior.  The last valid userspace
diagnostic remains r12, which reached the greeting and recorded the
NewSession futex/ppoll wait chain.

## Runtime evidence

```text
U-Boot desktop marker: FIREFOX probe boot
Starting kernel ...
qemu-system-riscv64: terminating on signal 15
guest Firefox markers: absent
guest probe receipt: absent
```

The absence of `ASTERINAS_BOOT_PHASE`, desktop, and Firefox markers means the
failure is before the userspace boundary.  No retry is scheduled from this
receipt.

## Inputs and artifacts

| Artifact | SHA-256 |
|---|---|
| `desktop-m4-drm.serial.log` | `bff6be2e9d87f44ede6495f52d4ff90705f4a4c4c750edbe29ef79a06eb50782` |
| `result.json` | `22dc12ee883181c4fa88e9f4af34a242f1eb787421e9a24d0cad2a757f38087d` |
| `lifecycle.json` | `dac70ead23ec30bbd38c3bcc756969f9a538f163a6fb79d005e6afcb9691c115` |
| QEMU | `d4a95ece1ea69687257cb584182bce231d8f379fda0231ec62d4da07611e1952` |
| kernel | `457ff00a120cf1481295ec54b3966606d9e73fc168c7dfd72d5a4e7d282ead68` |
| U-Boot | `47516422aea981a45eb3da6de786d356dbd9059c96bd9fc11d8e3ba28aec7210` |
| DTB | `d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c2` |
| Stage1 initramfs | `ce7fb996e9a6ac48160d81c29347a68d88e99204adcc147051df76f645031c84` |
| probe rootfs | `df7e8dcaee262cf1966eb4f0fbf6ad745d8ef20ea4f5a53e1faaaa77a69326ca` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r14-container/`

## Classification

`marionette_newsession_boundary` is unchanged.  The next useful action is to
diagnose the early GL/virtio-gpu launch boundary or use the already valid r12
userspace receipt; do not repeat the 15-minute scene gate.
