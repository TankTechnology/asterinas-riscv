# Firefox Marionette independent probe r30

r30 used the rematerialized probe rootfs with socket syscall argument
visibility.  It reached greeting and sent one NewSession request, then timed
out waiting for the response header.

The final parent snapshot is now precise:

```text
number=212  (recvmsg)
arg0=9      (socket fd)
arg1=274876972928 (msghdr pointer)
arg2=0
arg3=0
```

The Socket Process remains in `ppoll(73)` and the parent remains alive.  This
locates the stall at the Firefox parent-side Marionette socket receive after
the greeting; the evidence no longer points to an unobserved futex wake.
No scene, screenshot, frame-rate, or recovery phase was run.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| serial log | `ad96e985999a0bfccd9db81635a6f8caabd343c36128b07d413cbe55e926365f` |
| result | `ed0a72656c67b8be8f64694d594e6d0c7beef62d4960b54d08745a1b0834f911` |
| lifecycle | `c62fd25332142ac44bb7a2d68e1944abb139656cba50639da298e60876446462` |
| root image | `c7625d0c104205f37898773372465ac7a5e754a26930c3f490e11e8d2306fd28` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r30-container/`

