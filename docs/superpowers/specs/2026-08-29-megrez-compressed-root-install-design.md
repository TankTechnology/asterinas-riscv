# Megrez compressed Debian root installation

## Problem

The protected Megrez installer currently transfers the full 1 GiB ext2 image
over HTTP. The physical Asterinas receive path sustained about 1.1 MiB/s, so
the raw transfer cannot finish before the originally armed 600-second software
reboot.
The write gate and target-partition check are correct. The compressed transfer
fits the LAN better, but a second full 1 GiB device readback kept the protected
installation from completing even after the receive had finished.

## Decision

The host will create a deterministic gzip representation of the frozen ext2
image before it opens the serial device. The installer initramfs will fetch
`http://10.100.19.216:8080/debian-root.ext2.gz`, stream it through
`gzip -dc`, split the exact resulting bytes with `tee` between a SHA-256 FIFO
and a 1 MiB-buffered `dd` writer for `/dev/mmcblk0p2`, and wait for both before
declaring success. It will retain the existing `pipefail`, exact 4 GiB
partition check, write-arming kernel parameters, sync, and original ext2
SHA-256 check without performing a second full-device read.

The compressed file is transport data only. The immutable identity remains
the uncompressed root image recorded in the signed rootfs manifest and the
Megrez plan. A bad, truncated, or replaced compressed stream cannot publish
success because either the pipeline or background hasher fails, or the
SHA-256 of the exact bytes split by `tee` differs. `dd` uses full 1 MiB input
blocks so that the block device does not receive the small writes issued by
BusyBox `tee`. This validates the transport and write stream; it does not claim
an independent post-write media readback.

## Host-side publication

`megrez_debian_install.py` will stream-compress the held root image into a
same-directory temporary file using a gzip header with `mtime=0` and no source
filename. It will fsync the file, set mode 0644, atomically replace the final
transport file, and fsync the directory. Compression happens before building
the initramfs, starting the HTTP server, or opening the serial device. The
server will expose the compressed file's directory.

## Failure and recovery

Compression or publication failure leaves an existing compressed artifact
unchanged and prevents any serial side effect. A guest download/decompression
failure emits the existing `DEBIAN_INSTALL_FAIL` marker and cannot emit
`DEBIAN_INSTALL_PASS`. The Asterinas kernel's bounded software reboot remains
the recovery path; Linux is not used as the installer or runtime kernel.

## Verification

Focused tests will prove deterministic gzip bytes and round-trip identity,
atomic preservation on failure, the exact
`sha256sum < fifo` plus `wget | gzip -dc | tee(fifo) | dd(target)` pipeline,
the `.gz` private-LAN URL, compression-before-serial ordering, stream-hash
guard, and corrupted-gzip failure propagation. The real artifact will then be
compressed once and its size, gzip integrity, and decompressed SHA-256 will be
checked before a new plan-bound board attempt.
