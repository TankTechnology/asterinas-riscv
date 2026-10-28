# SYSTEMD M1 — Feasibility probe: minimal cross-build of systemd for riscv64

Date: 2026-08-14
Status: **Cross-build SUCCEEDS** (878/878 ninja targets). Only two workarounds
needed: one missing lib (libxcrypt) + one linker flag for a 2-symbol static
collision. Output is **dynamically-linked PIE**, not the fully-static form this
project currently ships.

## Objective

Determine whether systemd (pid1 + journald + `systemctl`/`journalctl`) can be
cross-compiled for the riscv64 userspace used by this project
(`target/riscv-cross/usr`, toolchain `riscv64-linux-gnu-gcc` 15.1.0).

## 1. Gap analysis — systemd build deps vs. the existing prefix

systemd v257.5's *hard* (unconditional, `required:true`) meson dependencies and
their status in `target/riscv-cross/usr`:

| dependency | meson spec | provider | status |
|---|---|---|---|
| `threads`, `rt`, `m`, `dl` | `dependency('threads')` / `find_library` | glibc | present (libc) |
| `libcap` | `dependency('libcap')` | libcap 2.75 | present (built earlier) |
| `mount` | `dependency('mount', '>= 2.30')` | util-linux 2.40.4 | present (built earlier) |
| `libcrypt` / `libxcrypt` | `dependency('libcrypt','libxcrypt')` + `find_library('crypt')` fallback | libxcrypt 4.4.38 | **built** (this M1) — was the only gap |
| `blkid` | `dependency('blkid', required: option)` | util-linux 2.40.4 | present |

**Missing-items list (before this M1): exactly one** — `libcrypt`. glibc ≥ 2.41
moved `crypt()` out of libc into libxcrypt, and the cross sysroot ships neither
`libcrypt.a` nor `crypt.h` (verified via `riscv64-linux-gnu-gcc -print-sysroot`).
systemd links `crypt()` unconditionally, so meson setup failed at
`meson.build:1068: ERROR: C shared or static library 'crypt' not found`.

Everything else systemd needs (libcap, libmount, libblkid, zlib) was already in
the prefix from the desktop (GTK2/X11) cross-compile work. All optional
subsystems (seccomp/selinux/…, gnutls/openssl/gcrypt, zstd/xz/lz4/bzip2,
curl/idn/microhttpd/dbus/glib/pcre2, and the homed/networkd/resolved/timesyncd/
logind/coredump/nss-* family) are disabled via `-D…=false`.

## 2. What was built this milestone

| artifact | script | result |
|---|---|---|
| `libcap.a` + `libpsx.a` + `sys/capability.h` | `build_libcap.sh` | OK |
| `libuuid.a libblkid.a libmount.a libfdisk.a libsmartcols.a` | `build_util_linux_libs.sh` | OK |
| `libcrypt.a` + `crypt.h` + `libcrypt.pc` | `build_libxcrypt.sh` | **OK — unblocked configure** |

Two fixes went into `build_libxcrypt.sh` (see script comments):

1. **Source URL**: the github `releases/download` asset stalls on this network
   (SSL `unexpected eof`, >2 min timeout). Switched to the Debian pool
   `deb.debian.org/.../libxcrypt_4.4.38.orig.tar.xz`, which is the same upstream
   orig tarball and downloads instantly.
2. **Missing configure**: that orig tarball is a git archive (ships `configure.ac`
   but no generated `configure`). Added `autoreconf -fiv -Wall`; the required
   autoconf-archive macros (`AX_*`) are bundled under `build-aux/m4` and found via
   `AC_CONFIG_MACRO_DIR`.

## 3. Build result

`build_systemd_minimal.sh` runs a heavily trimmed `meson setup` + `ninja`.
With the stock script the build reaches the link stage and stops; with the one
linker flag described in §4 the **full build completes (878/878, 0 failures)**.

- **meson setup: SUCCEEDS.** All hard deps resolve through the static pkg-config
  wrapper (`pkg-config-static`), required because `mount.pc` declares
  `Requires.private: blkid` and meson does not resolve `Requires.private` even
  with `--default-library=static`.
- **Compile: all objects build** (`libbasic.a`, `libsystemd-core.a`,
  `libsystemd_static.a`, `libudev-basic.a`, gperf-generated load-fragment tables,
  …). No C compile errors.
- **Link: fails on one collision, then succeeds with a flag.** See §4.

Produced binaries (in `build-riscv-amd/`): `systemd`, `systemctl`, `journalctl`,
`systemd-journald`, `systemd-nspawn`, plus `libsystemd-shared-257.so` and
`libsystemd-core-257.so`.

## 4. The one linker blocker: `parse_size` / `parse_range`

Without a workaround, the link of `src/shared/libsystemd-shared-257.so` fails:

```
ld: .../libmount.a(libcommon_la-strutils.o): in function `parse_size':
    multiple definition of `parse_size'; .../libbasic.a.p/parse-util.c.o: first defined here
ld: .../libmount.a(libcommon_la-strutils.o): in function `parse_range':
    multiple definition of `parse_range'; .../libbasic.a.p/parse-util.c.o: first defined here
collect2: error: ld returned 1 exit status
```

**Root cause.** systemd's `src/basic/parse-util.c` and util-linux's
`lib/strutils.c` both define global `parse_size` / `parse_range`. In a normal
distro this never collides because util-linux is a *shared* library whose
internal helpers are hidden by a version script (`libmount.sym`, `local: *`).
Here util-linux was built `--disable-shared --enable-static`, so `libcommon.la`
(owner of `strutils.o`) is archived *into* `libmount.a` with `parse_size`
exported global; systemd whole-archives its own `libbasic.a` into the `.so`, and
the two global definitions meet under `-Wl,--fatal-warnings`.

GNU ld reports *all* multiple-definition errors in a single invocation, and this
one lists exactly two symbols — so the collision is **shallow**, not a broad
namespace clash.

**Workaround that makes the whole build pass:** add `-Wl,--allow-multiple-definition`
to the cross-file `c_link_args`/`cpp_link_args`. The first definition (systemd's,
whole-archived first) wins. Verified: a fresh build dir with the flag completes
878/878 with 0 failures.

## 5. Feasibility conclusion

**Cross-compiling a minimal systemd for riscv64 is feasible** — the dependency
surface is one lib (libxcrypt) and the only build blocker is a shallow 2-symbol
static collision, cleared by a single linker flag. This is a much smaller gap
than the milestone assumed.

The remaining caveats, in order of importance:

1. **The output is dynamically linked, not static.** `systemd`/`systemctl` are
   PIE with `interpreter /lib/ld-linux-riscv64-lp64d.so.1` and `NEEDED
   libsystemd-core-257.so / libsystemd-shared-257.so / libc.so.6`. This project
   currently ships fully-static binaries. Producing a fully-static systemd needs
   further work (`-Dstatic-libsystemd=true` + static glibc linking) and — more
   fundamentally — either a working dynamic linker on Asterinas riscv64 (not yet
   verified) or a static-link effort with the same NSS/dlopen caveats already hit
   by static glibc programs in this project.
2. **`--allow-multiple-definition` is semantically risky.** systemd's and
   util-linux's `parse_size`/`parse_range` are not identical, so the "first
   definition wins" behaviour could diverge on edge-case size strings. A clean
   fix would rename systemd's copies (many call sites) or rebuild util-linux with
   per-symbol visibility (needs export annotations). For a *feasibility* probe
   the flag is fine; for a real init it would need the clean fix.
3. **Runtime viability is unproven.** Building is not running: systemd pid1 needs
   a large syscall surface (mount namespaces, cgroups, kdbus-free but still
   prctl/capset/setns/mknod/…), and glib-based programs have already hit
   rseq/signal gaps on this kernel (openbox). This was out of M1 scope.

**Recommendation.** M1 answered its question: yes, systemd *builds*. Do **not**
pursue systemd as pid1 on Asterinas yet — the static-linking and runtime-syscall
obstacles dominate the remaining cost, and the current `/init` spawner already
covers the desktop session's needs. Revisit only if dynamic linking lands, at
which point a normal distro-style shared build (the way buildroot/yocto do it)
is the low-effort path.

## 6. Diagnostic record

- Stock `build_systemd_minimal.sh`: meson setup OK → 502/878 → link FAILS on
  `parse_size`/`parse_range` (log `/tmp/systemd_build.log`).
- Fresh build dir `build-riscv-amd` + `-Wl,--allow-multiple-definition`: **878/878,
  0 failures** (log `/tmp/diag_ninja.log`). Confirms the collision is the only
  linker blocker and the flag clears it.
- Meson gotcha observed: `meson setup --reconfigure` does **not** re-apply changed
  cross-file `c_link_args` (the flag never reached `build.ninja`), so a clean test
  requires a fresh build dir.

## Reproduce

```sh
cd tools/riscv/systemd
./build_libxcrypt.sh            # libcrypt.a (only missing hard dep)
./build_util_linux_libs.sh      # libmount/libblkid/libuuid (idempotent)
./build_libcap.sh               # libcap/libpsx (idempotent)
# then build_systemd_minimal.sh, after adding to the cross file:
#   c_link_args  += '-Wl,--allow-multiple-definition'
./build_systemd_minimal.sh
```
