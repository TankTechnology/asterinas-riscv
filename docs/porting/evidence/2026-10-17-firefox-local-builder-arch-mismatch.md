# Firefox local builder architecture preflight

Date: 2026-10-17 (Asia/Shanghai)

## Result

An isolated local image was built from the existing GL/QEMU image to test
whether the missing Firefox build environment could be reproduced without
pulling the unavailable pinned image.  The image is deliberately recorded as
an **incomplete candidate**, not as the project's Firefox builder:

```text
base:  asterinas/qemu-gl-env:20261014
       sha256:de41e7c840a4861d654dd80996c0e9ce99d22eb08c37996860500c75b9e203b4
local: asterinas/debian-riscv-firefox-local:20261017
       sha256:37d8b120d48a5366e48dad72e6141c0f89783c6a41e85fac0efa7509b87970b7
architecture: amd64
```

The image contains the GTK/DRM/X11/audio/LLVM/wasm development packages,
Rust `1.96.0-nightly (55e86c996 2026-04-02)`, Cargo
`1.96.0-nightly (888f67534 2026-03-30)`, and cbindgen `0.27.0`.  Its local
metadata package records only the tool versions needed by the Debian source;
it does not provide target libraries.

The Rust binary advertises `riscv64gc-unknown-linux-gnu`, but the installed
Rust components contain only `x86_64-unknown-linux-gnu`; the RISC-V standard
library is absent.  The candidate also has no `riscv64` foreign package
architecture or cross sysroot.  This is an independent failure even before
linking GTK or DRM.

The real Firefox source descriptor was checked in the image:

```text
dpkg-checkbuilddeps debian/control             -> PASS (native amd64)
dpkg-checkbuilddeps -a riscv64 debian/control  -> FAIL
```

The target check fails on the unqualified GTK, DRM, X11, audio, LLVM/Clang,
wasm libc++, and related development packages.  The image has no foreign
`riscv64` architecture or target sysroot, so it cannot safely run the
requested `dpkg-buildpackage -B -a riscv64`.  No compiler or Firefox build was
started, and no `-d` dependency bypass was used.

The complete command output is retained at
`/mnt/mydisk/firefox-builder-local-20261017/preflight-20261017.log` with
SHA-256
`057a7ecdd94cc37b698edbb1912919fa2e3678536c516dae62cc4bc5cadee0db`.

## Boundary and next condition

This closes the host-side question that the available GL image is not a
drop-in Firefox RISC-V builder.  It does not close Task 6.  The next valid
input is either the pinned `asterinas/debian-riscv-builder` image (or an
offline export with its digest), or a separately audited native-riscv64
Debian/Trixie build context with a complete target sysroot.  Until one is
available, a Firefox WebRender runtime receipt cannot be produced honestly.

## Storage

At capture time `/mnt/shared` had 32,975,851,520 bytes available (about
30.7 GiB) and `/mnt/mydisk` had 2,748,188,372,992 bytes available.  The local
candidate image is retained on the Docker data root for this audit; no source
tree, tracked file, rootfs, or physical-board state was deleted.

## Driver fail-closed replay

After the preflight, the build driver was changed to repeat the target
dependency check and compile a tiny `riscv64gc-unknown-linux-gnu` Rust
metadata probe before invoking `dpkg-buildpackage`.  The focused driver tests
pass (`5` tests), and `bash -n` passes for the script.  A bounded replay with
the same local image stopped at the new gate, before a compiler or Firefox
build:

```text
FIREFOX_SOURCE_PACKAGE_GATE PASS package=firefox-esr version=140.16.0esr-1~deb13u1 archives=2
FIREFOX_SOURCE_PATCH_GATE PASS patches=2 source_files=4 fuzz=0
dpkg-checkbuilddeps: error: Unmet build dependencies: libx11-dev ... lld nodejs (>= 12.22.1)
FIREFOX_ESR_BUILD FAIL reason=target dependency or Rust target check failed
```

The complete bounded output is
`/mnt/mydisk/firefox-builder-local-20261017/driver-gate-rerun-r2/run.log`.
The wrapper was interrupted while its temporary source cleanup was waiting on
the `/mnt/mydisk` filesystem (`wait_on_buffer`); this does not change the
target-gate result and no package was produced.  The next valid run must use a
native RISC-V builder or an explicitly complete cross sysroot.
