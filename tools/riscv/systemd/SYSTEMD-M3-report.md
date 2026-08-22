# SYSTEMD M3 — Runtime ecosystem: D-Bus, udev data, login getty

Date: 2026-08-14
Status: **Runtime supporting cast cross-compiled.** D-Bus (`dbus-daemon` 1.16.2),
a fully-static `agetty`, and a full inventory of the udev data files are now
in the cross prefix. The single remaining runtime blocker is unchanged from M2 —
**the dynamic linker** — but it is now characterized precisely: the systemd
*core* binaries NEED the systemd shared libs, while the *supporting cast*
(agetty, dbus-daemon) static-link their own libraries and only NEED `libc.so.6`
(or, for agetty, nothing at all).

## Objective

M2 proved systemd v257.5 **compiles** cleanly (878/878) but is still dynamically
linked, and the pid1/journald blockers are architectural. M3 fills in the
*runtime ecosystem* around pid1 so that, when the dynamic-linker gap closes,
there is a complete set of binaries + data files to assemble a rootfs: the D-Bus
system bus, the udev device manager data, and a login getty. None of this touches
the kernel (session A owns the QEMU boot work).

## 1. D-Bus — `dbus-daemon` 1.16.2

**Decision: classic `dbus-daemon`, not dbus-broker.** dbus-broker is a Meson
project whose daemon links `sd-bus` and expects a working systemd (socket
activation, `systemd` dirs, `libsystemd` shared lib). That is exactly the M2
dynamic-linking blocker, so the *reference* daemon — a self-contained C program —
is the right call for a static ecosystem.

**Build gotcha:** dbus **≥ 1.16 is Meson-only** (autotools dropped; the tarball
ships `meson.build` + `CMakeLists.txt`, no `./configure`). The first
`build_dbus.sh` draft assumed autotools and died on `./configure: not found`.
Rewritten to the same `--cross-file` + `pkg-config-static` pattern as
`build_systemd_minimal.sh`.

Result (`build_dbus.sh`, idempotent):

```text
$ file usr/bin/dbus-daemon
ELF 64-bit LSB pie executable, UCB RISC-V, RVC, double-float ABI,
dynamically linked, interpreter /lib/ld-linux-riscv64-lp64d.so.1

$ readelf -d usr/bin/dbus-daemon | grep NEEDED
  NEEDED  libc.so.6        # <-- libdbus-1 + libexpat are STATIC-linked in
```

- `libdbus-1.a` (1.7 MB), `dbus-1.pc` — static, for the ecosystem to link against.
- Tools: `dbus-daemon`, `dbus-send`, `dbus-monitor`, `dbus-launch`,
  `dbus-run-session`, `dbus-uuidgen`, `dbus-cleanup-sockets`,
  `dbus-update-activation-environment`, `dbus-test-tool`.
- Config installed to `share/dbus-1/{system.conf,session.conf}` + empty
  `system.d/`, `session.d/`, `services/`, `system-services/` dirs. (`etc/dbus-1/*`
  are now just empty stubs that redirect to `share/dbus-1/*` — a 1.16 change.)
- Flags: `-Dsystemd=disabled -Dselinux/apparmor/libaudit=disabled
  -Dx11_autolaunch=disabled -Dmodular_tests=disabled -Ddbus_user=root
  -Duser_session=false`; `--default-library=static`.

**Fully-static dbus-daemon:** a second build dir (`build-riscv-static`) with
`c_link_args += '-static'` (M2's note: never reuse a build dir for changed
`c_link_args`) **links cleanly** — `bus/dbus-daemon` is a `statically linked`
ET_EXEC with no interpreter. Installed as `usr/bin/dbus-daemon.static` (1.2 MB
stripped). See §4 for the NSS caveat that keeps it from being the default.

## 2. udev — confirmed built, data files inventoried

**`systemd-udevd` is not a separate binary in v257.** It is a **symlink to
`udevadm`** — a multicall binary (`src/udev/meson.build`:
`meson.add_install_script(sh, '-c', ln_s.format(bindir/'udevadm', libexecdir/'systemd-udevd'))`).
The udevd daemon source (`udevd.c`) is compiled into `libudevd_core.a`, which is
linked into `udevadm`; `argv[0]` dispatches between the `udevadm` CLI and the
`udevd` daemon. So "udev is built" — the 878/878 tree emits `udevadm` + 8 helpers:

| binary | role |
|---|---|
| `udevadm` (→ `systemd-udevd`) | CLI + udevd daemon (multicall) |
| `ata_id`, `cdrom_id`, `scsi_id`, `v4l_id`, `mtd_probe` | device probing helpers |
| `dmi_memory_id`, `fido_id` | DMI / FIDO2 hardware identifiers |
| `iocost` | io controller cost model helper |

All are **dynamically linked** (`udevadm` NEEDs `libsystemd-shared-257.so` +
`libc.so.6`), i.e. inside the M2 blocker, not in the supporting cast.

**udev builtins** (compiled into `libudevd_core.a`): `btrfs`, `hwdb`, `input_id`,
`keyboard`, `net_driver`, `net_id`, `net_setup_link`, `path_id`, `usb_id`, plus
the conditional ones — `blkid` (**on**, libblkid present), `uaccess` (**on**),
`kmod` (**off**, `-Dkmod=false`).

### Data file inventory

| data | source location | count / size | installed by 878 build? |
|---|---|---|---|
| `rules.d/*.rules` | `rules.d/` | 31 files | **no** (not installed) |
| `rules.d/*.rules.in` | `rules.d/` | 8 files (needs meson `configure_file` substitution) | **no** |
| `hwdb.d/*.hwdb` | `hwdb.d/` | 32 files, **19.2 MB** | **no** (`-Dhwdb=false`) |
| `udev.conf` (sample) | `src/udev/udev.conf` | 305 B | only if `install_sysconfdir_samples` |
| `iocost.conf` | `src/udev/iocost/iocost.conf` | — | same |

The 8 `.rules.in` (e.g. `50-udev-default.rules.in`, `70-uaccess.rules.in`,
`99-systemd.rules.in`) are Jinja2 templates that `meson install` substitutes
(they reference build-time variables like `{udevlibexecdir}`). Because M2's build
ran `ninja` only (never `meson install`), **none of the udev data is assembled in
the prefix** — the rules/hwdb live in the source tree, ready for a rootfs
assembly step.

**hwdb is the big one:** `-Dhwdb=false` disables both the install *and* the
`hwdb.bin` compile (19.2 MB of PCI/OUI/vendor lookup tables → `hwdb.bin`). The
`udev-builtin-hwdb.c` builtin is still compiled but has no database to query. To
enable hwdb the tree must be reconfigured `-Dhwdb=true`, and `hwdb.bin` is
generated by `udevadm hwdb update` — a *target* binary, so it cannot run on the
build host; it would have to run at first boot inside the guest.

## 3. Login component — `agetty` (util-linux 2.40.4)

agetty ships in the same util-linux tree that already provides
libmount/libblkid/libuuid, so no new source is needed. `build_agetty.sh`
reconfigures the existing tree with `--enable-agetty` (on top of the M2 library
flags) and builds **only the `agetty` target** — deliberately **not** re-running
`make install`, which would overwrite the prefix's libmount/libblkid/libsmartcols
archives with un-localized copies and undo the M2 `parse_size/parse_range` fix.

Two variants are produced:

```text
$ file usr/sbin/agetty
ELF 64-bit LSB executable, UCB RISC-V, RVC, double-float ABI,
statically linked, stripped, for GNU/Linux 4.15.0   # fully static (no interpreter)

$ file usr/sbin/agetty.dyn
ELF 64-bit LSB pie executable, … dynamically linked, NEEDED libc.so.6
```

- **`agetty` (static)** is the primary deliverable: a login getty that runs with
  **no dynamic linker**. The `-all-static` link emits the standard NSS warnings
  (`getgrnam` for the tty group, `getaddrinfo` for the `--list`/IP banner) — the
  same static-glibc NSS caveat the project has already mapped; agetty degrades
  gracefully when those lookups fail.
- **`agetty.dyn`** is the PIE libc-only variant, for when a dynamic linker lands.

agetty's own deps are trivial: `term-utils/agetty.c` + `lib/logindefs.c` linked
against util-linux's internal `libcommon` only (no PAM, no NSS requirement for
the core path, no glib stack).

## 4. Runtime dependency checklist

| component | binaries | links against | usable with no dynamic linker? |
|---|---|---|---|
| systemd pid1 / journald | `systemd`, `systemd-journald` | `libsystemd-core-257.so` + `libsystemd-shared-257.so` + libc | **no** (M2 blocker) |
| systemd CLI / udev | `systemctl`, `journalctl`, `udevadm`, … | `libsystemd-shared-257.so` + libc | **no** |
| D-Bus | `dbus-daemon`, tools | libdbus-1.a + libexpat.a + **libc.so.6** | needs dynamic linker for libc |
| D-Bus (static) | `dbus-daemon.static` | libdbus-1.a + libexpat.a + libc.a | **yes**, but NSS dlopen (`libnss_files`) |
| getty | `agetty` | libcommon.a only | **yes** (fully static) |
| getty (PIE) | `agetty.dyn` | libc.so.6 | needs dynamic linker |

**The dynamic-linker dependency is systemic, not systemd-specific.** The Debian
cross sysroot (`/usr/riscv64-linux-gnu`) ships `libc.so.6` and
`ld-linux-riscv64-lp64d.so.1`, and every binary in this glibc-based ecosystem —
systemd's or the supporting cast's — resolves to that interpreter unless
explicitly `-static`. The static `libc.a` *does* exist (`usr/lib/libc.a`), so
full-static is reachable, but static glibc reintroduces the NSS/dlopen caveat
(`getpwnam`/`getgrnam`/`getaddrinfo` → runtime `dlopen` of `libnss_*.so`), which
is why only the NSS-free-ish tools (agetty) get the full-static treatment here.

**Fully-static `dbus-daemon`:** confirmed — `c_link_args='-static'` in a fresh
build dir links `bus/dbus-daemon` fully static (`statically linked`, no
interpreter), installed as `dbus-daemon.static`. The link emits the same static-
glibc NSS warnings (`getpwnam_r`/`getgrnam_r` for `dbus_user=root`,
`getgrouplist`, `getaddrinfo`), so at runtime a static dbus-daemon still needs
`libnss_files.so.2` (and `libnss_dns.so.2` if it resolves names) — the one thing
that keeps the dynamic-libc build as the default and static as an opt-in.

## 5. Cross-cutting assembly caveat

Everything installed via `--prefix=<absolute cross path>` **bakes the host
path into generated config and unit files**:

- systemd `systemd-udevd.service` → `ExecStart=/home/arch-anjie/…/usr/lib/systemd/systemd-udevd`
- dbus `share/dbus-1/system.conf` → `<listen>unix:path=/home/arch-anjie/…/usr/var/run/dbus/system_bus_socket</listen>`

A bootable rootfs therefore needs a **second configure pass** with
`--prefix=/usr` (+ `--localstatedir=/var`, `--sysconfdir=/etc`) and
`DESTDIR=<staging>`, exactly as a distro package build does. That is the "会师"
(rendezvous) step that turns M1/M2's compile-proof prefix into a mountable rootfs.

## 6. Remaining (in order)

1. **Dynamic linker on Asterinas riscv64** — still the one gate for *running*
   anything here except the static agetty. Systemic, per §4.
2. **udev data assembly** — a `meson install` (or distro-style `--prefix=/usr` +
   `DESTDIR`) pass to lay down `rules.d/` and (optionally) `hwdb.bin`. hwdb needs
   `-Dhwdb=true` + a first-boot `udevadm hwdb update` in the guest.
3. **dbus static + NSS** — a static `dbus-daemon` needs `libnss_files.so.2` in the
   rootfs; fold in if a no-dynamic-linker rootfs is the target.
4. **Runtime syscall surface** (unchanged from M2) — pid1's mount/cgroup/prctl/
   capset/setns needs remain unproven on the kernel; session A's QEMU boot is the
   probe for that.

## Reproduce

```sh
cd tools/riscv/systemd
./build_dbus.sh      # dbus-daemon 1.16.2 + libdbus-1.a + config (idempotent)
./build_agetty.sh    # agetty (fully static) + agetty.dyn (idempotent)
# udev data is already in the M2 build; assembly is a meson install, see §5/§6.2
```

Fully-static `dbus-daemon` (optional; produces `dbus-daemon.static`): configure a
**fresh** build dir with `-static` in `c_link_args` (see the `cross-dbus-riscv64.txt.static`
cross file generated in `$SRC/`), then `ninja -C build-riscv-static` and
`riscv64-linux-gnu-strip -o usr/bin/dbus-daemon.static build-riscv-static/bus/dbus-daemon`.

Re-run preconditions (all idempotent): `build_libcap.sh`, `build_util_linux_libs.sh`,
`build_libxcrypt.sh`, `build_systemd_minimal.sh` from M1/M2.
