# Firefox RISC-V cross-builder preflight

Date: 2026-10-17 (Asia/Shanghai)

This is a bounded Task 6 host preflight. It did not compile Firefox and did
not start a guest or touch the physical board.

## Candidate inspected

```text
image: codex/asterinas-cross-builder:aa6f7533
digest: sha256:e0ffcf0612ed4da5c1a3a0e3a2fd601edb81c909008ee540eb62f5140f8ff5b4
host architecture: x86_64
cross compiler: riscv64-linux-gnu-gcc (present)
RISC-V sysroot: /usr/riscv64-linux-gnu (present)
```

The candidate is not sufficient for the Firefox gate:

```text
error[E0463]: can't find crate for `std`
note: the `riscv64gc-unknown-linux-gnu` target may not be installed
RUST_TARGET_RC=1
gtk-riscv=no
```

The image therefore lacks both the Rust RISC-V standard library and the
RISC-V GTK development libraries required by Firefox's target dependency
check. It is rejected as a Firefox builder. No dependency bypass or compiler
invocation was attempted.

## Bounded repair attempt

With the host proxy explicitly passed into a disposable copy of the image,
`rustup target add riscv64gc-unknown-linux-gnu` completed successfully. A
Debian Trixie RISC-V package index was also fetched and exposed candidates for
`libgtk-3-dev:riscv64`, `libdrm-dev:riscv64`, `libx11-dev:riscv64`, and
`llvm-dev:riscv64`.

Installing those packages directly into the Ubuntu multiarch image is not a
valid builder construction: the simulated transaction cannot resolve the
RISC-V libc/glibc development closure and reports held or non-installable
dependencies. A second index refresh also encountered a transient proxy 502.
The container was discarded; no host package state changed and no Firefox
compile was started.

## Task 6 boundary

The GL QEMU desktop gate is now complete (r24), but Firefox WebRender remains
open. The next valid input is the pinned native-riscv64 Firefox builder or an
audited Debian Trixie cross image containing the Rust target plus a coherent
RISC-V GTK/DRM/X11, LLVM/Clang, audio, and wasm sysroot. This preflight does
not change the QEMU DRM result.

A clean `debian:trixie` container was checked as the next repair route. Its
RISC-V package index is reachable, but the current amd64/riscv64 package
snapshot resolves conflicting versions (`libpcre2` and the libc development
closure). The correct builder must pin one matching Debian snapshot for both
architectures; ad-hoc mixed-archive installation is rejected.

The base image records snapshot `20261005T000000Z`. Through the container's
proxy, snapshot access returned HTTP 502. Bypassing the proxy reached the
snapshot and fetched its InRelease and amd64 index, but the RISC-V index did
not finish within the bounded 120-second run. The next reproducible builder
step must prefetch and checksum both architecture indexes (or use a local
Debian mirror) before package resolution; no unbounded retry was left running.

## Snapshot inputs now prefetched

The host successfully prefetched the pinned snapshot inputs into
`/mnt/mydisk/firefox-builder-snapshot-20261017/`:

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `InRelease` | 140421 | `0584fba32e13e0ab8285fb16c27adea1ec03a73669c18702821094fd6ca86675` |
| `Packages-amd64.xz` | 9678380 | `7778d3e3f303b7ddb8ce0fe7c8d57473a076c6bf2e8f241f75421d2396352498` |
| `Packages-riscv64.xz` | 9385284 | `1b0d89902b0bde1d8cacb392e3c9b73511abc12b291511c3a9dcd28ad0fa3582` |

The SHA-256 entries for both package indexes match the pinned `InRelease`.
The records also show matching versions for the initial closure, including
`libgtk-3-dev` 3.24.49-3, `libdrm-dev` 2.4.124-2, and `libc6-dev`
2.41-12+deb13u4 on both architectures. Package resolution can now use these
immutable local indexes instead of the live archive.

A clean Debian container was then run against the same pinned snapshot with a
600-second bound. It downloaded both indexes but APT produced no dependency
result during the bound and remained in its package-state phase, so the
process was terminated. The local indexes remain the authoritative inputs; no
package installation or Firefox compilation was started.

## Sysroot extraction and compiler preflight

All 370 unique `.deb` files in the 391-entry closure (including `all`
architecture packages) were extracted without maintainer scripts into:
`/mnt/mydisk/firefox-builder-snapshot-20261017/sysroot-riscv64/`.
The extracted sysroot occupies about 1.5 GiB and contains RISC-V glibc,
GTK 3, libdrm, X11, and LLVM libraries.

With that sysroot mounted read-only in the cross-builder image, a bounded
compiler preflight passed:

```text
PKG_CONFIG=... -lgtk-3 ... -ldrm -lX11
/tmp/gtk-riscv64: ELF 64-bit ... UCB RISC-V ... dynamically linked
/tmp/gate.rmeta: data
FIREFOX_SYSROOT_PREFLIGHT PASS
```

The Rust RISC-V standard library was installed during this disposable run.
No Firefox source compilation has started; the remaining work is to bind this
sysroot and its package metadata to the Firefox Debian build driver.

The Firefox ESR `.dsc` and Debian tarball were also downloaded and verified:
the `.dsc` SHA-256 is
`721ad7dc3924a0061a0f5893340f97659e2e7c35b65a95f004eb8174cab2f249`, and the
Debian tar SHA-256 is
`61f7977216d310103af70d66918ddf7d47bf60ac50a19016712f0d6c168e9245`.
The declared Build-Depends are now available for the next host/target split;
no 688 MiB upstream source archive was downloaded yet.

The pinned Firefox ESR source archive has now been downloaded, unpacked, and
both compatibility patches applied with `patch --fuzz=0`:

```text
FIREFOX_PATCHES PASS
```

The patched source tree is under
`/mnt/mydisk/firefox-builder-snapshot-20261017/firefox-source/`. No compiler
build has started yet; the next gate is Debian Build-Depends admission against
the extracted sysroot and host toolchain.

The bounded downloader attempted the same closure with eight workers. Snapshot
package requests returned repeated HTTP 502 responses, so the process was
stopped after retaining 124 verified `.deb` files (44 MiB) and removing all
partial files. The verified closure and indexes remain intact; the next
download should use a Debian mirror pool or serialized host-direct requests.

The mirror-pool retry with four workers completed 369 of 391 packages and
verified 244 MiB before two large LLVM/Z3 downloads stalled. The bounded job
was stopped and all `.part` files removed. The remaining package list is
limited and can be retried serially or through a secondary Debian mirror.

The final missing `libz3-4` package was fetched from `ftp.cn.debian.org` and
matched the pinned SHA-256. A reuse-aware downloader replay then passed:

```text
SNAPSHOT_DOWNLOAD PASS packages=391 output=.../packages-riscv64
```

The complete verified package set is now under
`/mnt/mydisk/firefox-builder-snapshot-20261017/packages-riscv64/`, with its
machine-readable manifest at `download-manifest.json`. This is the first
complete offline package closure; it has not yet been extracted into a
sysroot or installed into a builder image.

The resolver was then corrected to index Debian `Provides` aliases. The GTK
GIR development alias is provided by the concrete `gir1.2-atk-1.0` package,
so the apparent missing dependency is resolved without a substitution. A
fresh offline closure now passes:

```text
SNAPSHOT_RESOLVE PASS packages=391 arch=riscv64
```

The deterministic closure is
`/mnt/mydisk/firefox-builder-snapshot-20261017/riscv64-initial-closure.json`
(SHA-256
`7ad4dca5f5a9ad4bb0ff930bd3d94d90540825b6107f26b3b14f5a9427ac7860`), with
391 packages and 266,577,420 compressed package bytes. This is metadata only;
the packages have not yet been downloaded or installed.

## Offline dependency resolver result

The first resolver run stopped on the virtual dependency
`gir1.2-atk-1.0-dev:riscv64`. The resolver was then corrected to index Debian
`Provides`; the concrete `gir1.2-atk-1.0` package provides that alias. The
fresh 391-package closure above passes without inventing a substitution.

The candidate cross image still lacks several host tools (`debhelper`, `lld`,
`cbindgen`, `nodejs`, `nasm`, `xvfb`, and `xauth`). An Ubuntu package
simulation confirmed those packages are available, but its live archive update
returned a proxy 502; no mutable image was created. The target sysroot and
source remain unchanged.

An isolated amd64 host-tool closure was resolved and extracted (229 package
rows; `debhelper`, `lld`, `cbindgen`, `nodejs`, `nasm`, `xvfb`, `xauth`, and
`dpkg-buildpackage` are present under
`/mnt/mydisk/firefox-builder-snapshot-20261017/host-tools`). A final bounded
preflight reached the Rust standard-library download, where the mirror TLS
handshake failed before compilation. No Firefox build was started.

The Rust standard-library tarball is now saved at
`/mnt/mydisk/firefox-builder-snapshot-20261017/rust-std-riscv64gc-nightly-20260403.tar.xz`
with SHA-256
`3862c3089a3269c6b0fdf0bed2b2e80e39ac07579a9df69458e4b3cf8aea9fa5`.
Extracting it into the disposable toolchain, together with the host-tools tree
and RISC-V sysroot, produced `FIREFOX_TOOLCHAIN_PREFLIGHT PASS`: Rust target
metadata compiled and GTK/libdrm/X11 pkg-config resolution succeeded. Firefox
package compilation remains unstarted.

## 2026-10-17 pinned Build-Depends identity gate

The pinned snapshot resolver was rerun against the complete Firefox ESR
`debian/control` Build-Depends set. It produced:

* RISC-V target closure: 405 package rows, with every row present and the
  recorded archive size matching under
  `/mnt/mydisk/firefox-builder-snapshot-20261017/packages-riscv64/`.
  Closure SHA-256: `8f6738a65795b1645f3431e912cc05a958f6e03732b901fdbd9bbd6a43a31d60`.
* amd64 build-host closure: 553 package rows, 532 unique `.deb` files after
  `Architecture: all` de-duplication, all downloaded and extracted under
  `/mnt/mydisk/firefox-builder-snapshot-20261017/host-tools/`.
  Closure SHA-256: `fb5589f3ee2936ad6aefd7c11446af4b6c9e2f79bf45be496a9ff40c03e6155e`.

`dpkg-checkbuilddeps --admindir=<temporary snapshot status> -a amd64
<firefox-source>/debian/control` returned zero. The temporary status was
constructed solely from `dpkg-deb -f` metadata plus a record for
`build-essential`, which is installed in the host container and is a dpkg
built-in dependency. No `-d` dependency bypass was used, no package was
installed into the host, and no Firefox compilation was started. This closes
the package-identity gate only; it does not claim a Firefox binary or a
WebRender runtime result.

The focused DRM host regression command also passed 22 tests:

```
python3 -m unittest -v tools.riscv.tests.test_drm_render_node_gate \
  tools.riscv.tests.test_drm_xorg_modesetting_gate \
  tools.riscv.tests.test_desktop_drm_libdrm_probe
```

Those are host contract tests and remain separate from the existing QEMU
runtime receipts. The physical board was not accessed.

## 2026-10-17 cross-build progress and input repair

The first extracted Firefox source tree was found to be invalid: 51,550 files
were zero bytes, including `build/mach_initialize.py`. The original upstream
tarball contains a 19,861-byte copy, so a separate source tree was extracted
from the verified orig tarball and the Debian tarball was overlaid there. The
two local virgl/DRM patches applied with zero fuzz. The repaired source is
`/mnt/mydisk/firefox-builder-snapshot-20261017/firefox-source-fixed-20261017/firefox-140.16.0/`;
`mach_initialize.py` SHA-256 is
`50d3ae01b97c26ebf652588150baa6915c8fcc917caaf553a4af638e64708960`.

An actual `debian/rules build-arch` cross build then passed the full configure
phase for `riscv64-linux-gnu`: C/C++, GTK3/Wayland, X11, DRM, Rust target
metadata, bindgen/LLVM, Node 18, and WASI headers/libraries all resolved.
The host wrapper distinguishes RISC-V, x86_64 host-tool, and wasm32-wasi
compilation; the target sysroot was extended with a 16-package
`libstdc++-dev` closure and a `wasi-libc` package. The exact closures are
`riscv64-libstdcxx-closure.json` (SHA-256
`aa0d8a8b2a951f3bbabbbd8db23e71ef5b44e5d66a226c028a2c8d8e9b970240`) and
`wasi-libc-closure.json` (SHA-256
`5a76388f3673bcfb2a924ccb22ff8ef759077d5aa2f2af05cc2bb085eeb9a6a8`).

The first compile pass reached RISC-V Rust and C++ compilation without a
compiler error. It was stopped after the bounded observation window, leaving
an incremental `build-browser` directory of 11,228,813,803 bytes. A second
run used `debian/rules build-arch` directly with `parallel=4`, avoided the
clean step, and advanced further through network/Places/process-tool sources;
it was also stopped at the next bounded point. Logs are
`/mnt/mydisk/firefox-builder-snapshot-20261017/firefox-build-cross-fixed.yu4iKZ.log`
and
`/mnt/mydisk/firefox-builder-snapshot-20261017/firefox-build-resume.yRuZsq.log`.
No Firefox binary or package has been produced yet, and no WebRender runtime
claim is made. The physical board was not accessed.
