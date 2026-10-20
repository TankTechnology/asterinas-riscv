# M4 ordered device handoff — r71

## Scope

This is the third and final short replay in the current debugging slice.  It
uses a derived ext2 image only; the base image and all real hardware remain
untouched.  The change under test defers both ordered `fb0` and evdev inode
permission operations, which had previously been able to block the desktop
handoff.

## Runtime result

The QEMU run used a 60-second boot deadline and ended fail-closed with
`lifecycle.status=failed`, `reason=protocol`.  It did not reach the complete
M4 classifier, so this is not an E3 pass.

The serial transcript does prove that the ordered device preflight reaches its
terminal marker:

```text
ASTERINAS_DESKTOP_DEVICE_ACCESS phase=fbdev-permissions-done
ASTERINAS_DESKTOP_DEVICE_ACCESS phase=input-permissions-deferred
```

The remaining boundary is systemd user-service start latency: no
`Started asterinas-desktop-m4.service` or session marker appeared before the
60-second deadline.  This is distinct from the earlier r66/r70 PAM/TTY and
device-open stalls.

## Derived input identity

| Artifact | SHA-256 |
| --- | --- |
| overlay-r71.json | `4a0a811b1aa9861155a2f4a632ee70de035e11aa493893d086ed00744d4d6cf1` |
| derived root image | `f20f68ae0b1ab4ce79fc5febb69af38e6086cbe13b15a7a7b0d63dce52f369f0` |
| derived manifest | `8ff2ba31f28f6a3dc97b3f274e6146b9d5ea9a262d212ccebadb7bd588efea86` |
| device-access source | `c893fb71acdabd3ce226e224f4973379d2869809d4bbe7b4590379de6c87cf26` |
| session source | `22032ecd817d6cb3a64cf527dd471d085c109882cc0c4d78ef6efbcd14108352` |

## Next bounded step

Do not repeat the same 60-second run.  The next code-only step is to inspect
the systemd user-service setup (user/group resolution and service dependencies)
and add a bounded diagnostic marker around `ExecStart` creation.  A longer
runtime budget is not a fix and is not justified by this receipt.

