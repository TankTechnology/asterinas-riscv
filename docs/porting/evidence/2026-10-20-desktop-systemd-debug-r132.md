# desktop-systemd-debug r132

## Scope

Bounded generic-QEMU diagnostic with no display device, no network, no physical
hardware, and no Firefox/Xorg workload.

## Result

- output: `/mnt/mydisk/qemu-desktop-systemd-debug-r132`
- serial SHA-256: `a345547f53ac33cba7b06cbdd3728d9876965744b1583845a26624b78bc3473b`
- lifecycle SHA-256: `b95d6b97c9e38e9a36c7ae02d850de1af67fd4f48789135afadee22b7a3ddb5b`
- result SHA-256: `3ea49cd9411de7e88eeac880914f78a2cc7d0d0d8787b00fbd883a96130bf3b7`

Observed result: debug run reached sysctl failure but emitted no further transaction detail; next unit remained unresolved.

## Conclusion

This control run does not close sysinit; debug run reached sysctl failure but emitted no further transaction detail; next unit remained unresolved. It supports the r135 isolation and must not be treated as a Firefox or desktop result.
