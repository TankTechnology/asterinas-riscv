# Firefox ESR RISC-V build preflight

Date: 2026-10-17

## Current source input

The full source tree is available at
`/tmp/firefox-esr-140-source-tree/firefox-140.16.0`. Its two core source
archives and the Debian descriptor were hash-checked, the descriptor
signature was checked with a disposable keyring, and the two compatibility
patches apply with zero fuzz.

## Build command and required environment

The intended package command is:

```text
dpkg-buildpackage -B -a riscv64 -us -uc
```

The repository's pinned builder base is
`asterinas/debian-riscv-builder@sha256:d53fb72d643698efb412985650161fc5deff60eba30c2457e173dca9ce9f1acd`.
The Firefox source descriptor requires GTK 3, DRM, X11, PulseAudio/ALSA,
Rust/Cargo, LLVM/Clang, libc++ wasm, LLD, cbindgen, Node.js, NASM and the
font/X11 test packages.

## Preflight result

On the current host, `dpkg-checkbuilddeps debian/control` reports these
dependencies as missing:

```text
debhelper-compat libx11-xcb-dev libxt-dev libgtk-3-dev libglib2.0-dev
libdrm-dev libstartup-notification0-dev libreadline-dev libnspr4-dev
libvpx-dev libdbus-1-dev libpulse-dev libasound2-dev llvm-dev libclang-dev
clang libc++-dev-wasm32 libclang-rt-dev-wasm32 lld cbindgen
ttf-bitstream-vera fonts-dejima-mincho
```

The pinned builder image is not present locally. The root filesystem has
about 6.4 GiB free after retaining the 4.1 GiB extracted source tree, which is
below a safe Firefox build budget. No build was started.

Existing local Asterinas cross-builder images do provide
`riscv64-linux-gnu-gcc`, `qemu-riscv64-static`, `clang`, Rust and
`dpkg-buildpackage`, but an in-container `dpkg-checkbuilddeps` reports the same
missing Firefox development packages. They are therefore not substitutes for
the pinned Debian Firefox builder.

The new build driver was run against the real source tree and descriptor:

```text
FIREFOX_SOURCE_PACKAGE_GATE PASS package=firefox-esr version=140.16.0esr-1~deb13u1 archives=2
FIREFOX_SOURCE_PATCH_GATE PASS patches=2 source_files=4 fuzz=0
FIREFOX_ESR_BUILD FAIL reason=insufficient free space: 6833160192 bytes, need 12884901888
```

The driver stopped before `docker image inspect` and before creating a build
copy, so this failure is an intentional storage preflight rather than a
compiler failure.

A metadata-only registry probe was performed afterward. The pinned image is
not present locally. `docker manifest inspect` and
`docker buildx imagetools inspect` both failed before downloading layers: the
Docker Hub endpoint timed out during TLS negotiation; configured mirrors
returned only 403/502/404 responses (the reachable DaoCloud endpoint returned
`DENIED`). No image was pulled and no Docker storage was changed.

## Boundary

This is a fail-closed preflight result, not a build failure and not a Firefox
runtime result. The next prerequisite is a host with the pinned builder image
and enough isolated storage; after that, run the source gate, apply both
patches, build the RISC-V package, and pass its `.deb` through the package
provenance gate before creating a QEMU rootfs.
