# Firefox pinned builder availability preflight

Date: 2026-10-17 (Asia/Shanghai)

## Result

The Firefox Task 6 build driver was run against the audited ESR 140 source
tree and descriptor with its output on `/mnt/mydisk`:

```text
FIREFOX_SOURCE_PACKAGE_GATE PASS package=firefox-esr version=140.16.0esr-1~deb13u1 archives=2
FIREFOX_SOURCE_PATCH_GATE PASS patches=2 source_files=4 fuzz=0
FIREFOX_ESR_BUILD FAIL reason=builder image is not available locally: asterinas/debian-riscv-builder@sha256:d53fb72d643698efb412985650161fc5deff60eba30c2457e173dca9ce9f1acd
```

The output filesystem has sufficient capacity for the recorded 12 GiB build
floor. The failure occurs at the immutable builder-image check, before a
build directory or compiler process is started. No Firefox package was
produced.

The locally available candidate images were inspected but cannot substitute
for the pinned image: their Debian dependency checks lack Firefox's GTK,
DRM/X11, LLVM/Clang, WebAssembly libc++, and `cbindgen` build dependencies.
Targeted searches also found no local RISC-V `firefox-esr` package, executable,
or `libxul.so` that could supply a runtime receipt.

## Boundary

This is a fail-closed build-environment receipt, not a compiler, package,
QEMU, WebRender, or physical-board result. The next Task 6 prerequisite is an
available immutable builder image (or an equivalently complete, auditable
environment) before starting the multi-gigabyte build.

