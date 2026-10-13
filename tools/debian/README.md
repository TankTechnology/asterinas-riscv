# Architecture-neutral Debian tooling

`tools/debian/` holds the parts of the Debian rootfs pipeline that do not depend
on a target architecture.

## Contents

| Module | Role |
| --- | --- |
| `rootfs/arch.py` | The architecture table: Debian name, output-directory slug, Nix platform, dynamic loader, user-mode emulator, cross compiler. |
| `rootfs/profiles.py` | The rootfs profiles: one frozen list of Debian packages and a filesystem identity per profile. |
| `rootfs/contract.py` | Writes and verifies the rootfs manifest and the package lock. |
| `rootfs/signed_sources.py` | Verifies Debian `InRelease` metadata against the archive keyring. |
| `rootfs/fsops.py` | Publishes and caches artifacts. |
| `rootfs/dev_overlay.py` | Materializes a development overlay on top of a published rootfs. |

`tools/riscv/debian/rootfs/` re-exports each of these as a module alias, so
`tools.riscv.debian.rootfs.contract` and `tools.debian.rootfs.contract` are the
same module object. Attribute access and `mock.patch` through either path
therefore reach the one real module. An alias does not survive `python -m`, so a
caller that *executes* a module must name `tools.debian.rootfs.<module>`.

The shell builder, its profile assets (desktop wallpapers, browser probes,
systemd units) and `build_stage1.sh` still live under `tools/riscv/debian/rootfs/`
and read those assets relative to their own directory. Moving them is a separate
change.

## Target architecture

The builder takes `--architecture`, defaulting to `riscv64`:

```sh
tools/riscv/debian/rootfs/build_rootfs.sh --architecture amd64 \
  --profile systemd-ext4-m6-userspace --output-dir target/debian-x86/rootfs
```

An architecture the host can execute runs natively: `run_chroot` becomes a plain
`chroot`, no user-mode emulator is installed into the staged root, and the
execution-boundary check is skipped. Anything else requires the emulator named
by `arch.py` and a working binfmt registration, exactly as before.

The default target's output paths are derived in the shell as well as in
`arch.py`. The builder's pre-tool checks must run before any external command is
trusted, including the interpreter that would read the table, so the shell keeps
the default and `arch.py` supplies everything else. A test asserts the two
agree.
