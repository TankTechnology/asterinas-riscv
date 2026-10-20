# QEMU virgl runtime replay audit

Date: 2026-10-17

## Scope

I attempted one bounded re-run of the virgl parameter gate using the only
currently retained DRM desktop input bundle:

```text
target/qemu-uboot/drm-desktop-rootfs-build/{u-boot,boot.ext4,artifacts.json}
```

The local GL-capable QEMU container was used read-only, with networking
disabled and a container tmpfs for output. No physical board or serial port
was used.

## Result

The gate stopped before QEMU launch:

```text
{"passed": false,
 "reason": "gate error: ValueError: boot disk payload identities do not match the manifest"}
```

The manifest declares these payload identities:

```text
kernel_sha256=79223b06381b79e0a3258d936e05f0107f365ba3f7f0e52ee11be71f305c9578
dtb_sha256=22a33eaefb4c064dc04482d2b336144e2f4352615c0f9052ba55100689a835a3
initrd_sha256=032e41f3b9b3482f9f5b122992bcd368c9ff5710df0d61802a6572609b0c4823
```

The retained bundle identities are:

```text
u-boot       3e5ae37dcd7071a274780414bc37c74ac1df12b916d77532b290e4eb61a5fc80
boot.ext4    1b7fd88f41c100ab6f5dc1e88a6deafd8a1ef9415758b85032646a06f5f8b315
artifacts    d9da0d9f47148a0756a041a3715b8d4beb9dd8e4855324e4089f475b54edf313
```

The failure is an input-integrity failure, not a virgl or kernel failure. The
gate correctly refused to launch with a stale manifest/image pairing.

I then scanned all 130 retained `target/qemu-uboot/**/boot.ext4` files against
the same manifest using `verify_boot_disk_artifacts` in the read-only GL
container:

```text
CANDIDATES=130
MATCHES=0
```

No matching boot disk remains in the current workspace.

## Consequence

The existing Task 3/5 Markdown receipts remain historical QEMU evidence, but
they cannot be independently replayed from this retained directory. A fresh
replay requires a matching boot disk and artifact manifest to be archived
together. No input was repaired or overwritten in this audit.
