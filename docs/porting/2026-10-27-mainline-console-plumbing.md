# 2026-10-27 mainline console plumbing and the missing gmac input

- Claim: the daily-use console plumbing (sudo, devpts, `/etc/hosts`,
  SIGPWR poweroff) is ported to the RISC-V rootfs builder, and the
  desktop-m5/browser builds no longer fail on the missing
  `gmac_single_packet_evidence.py`.
- Status: **landed on main; the daily image boot was verified on
  2026-10-28** (see `board-image-current.md`). The original stage-level
  note is retained as the historical record.
- Branch: `codex/debian-perf-main`.
- Commits: `366cd0859`, `4e468f925`.
- Landing: `main` was fast-forwarded from `d111b0d6b` to `7464352d2`
  on 2026-10-27. The `main-firefox-desktop` worktree that held `main`
  was restored to `HEAD` first: all 25 modified tracked files were
  byte-identical to blobs already in history (a stale ~2026-09-25
  tree that would have reverted the October bio, framebuffer and
  watchdog fixes), and the 40 deletions were files added after that
  date. The backup of that state and of the seven untracked 2026-09-25
  desktop-shell artifacts is in
  `/tmp/main-firefox-desktop-20261027/`.

## What the frozen image is missing

Read directly from the frozen r122c image
`/mnt/mydisk/asterinas-firefox-rootfs-20261020-capab-ab-r122c/debian-root.ext2`
with `debugfs` (read-only):

| Path | Content |
| --- | --- |
| `/etc/hostname` | `ubuntu-MS-7D76` (the build host name) |
| `/etc/hosts` | only `localhost` and IPv6 aliases; no `127.0.1.1` entry |
| `/etc/fstab` | `# UNCONFIGURED FSTAB FOR BASE SYSTEM` |
| `/usr/bin/sudo` | absent |

So the console account could not run package management, and `sudo`
would have failed on the missing pty and the unresolved host name even
had the binary been present. These are the same defects the x86 shared
builder fixed in `ab7cc30ac` and `2031bda34`.

## Ported changes

`tools/riscv/debian/rootfs/build_rootfs.sh`, inside `configure_desktop()`
(after the console account is created):

- `/etc/sudoers.d/asterinas`: `Defaults !use_pty` and
  `asterinas ALL=(ALL:ALL) NOPASSWD: ALL`, mode 0440;
- `/etc/fstab`: `devpts /dev/pts devpts gid=5,mode=620,ptmxmode=000 0 0`;
- `/etc/hosts`: append `127.0.1.1 <hostname>` when absent;
- `asterinas-sigpwr-poweroff.service` wanted by `sigpwr.target`, running
  `/bin/systemctl --no-block poweroff`.

`tools/riscv/debian/rootfs/profiles.py` adds `sudo` to the requested
packages of `desktop-m3`, `desktop-m4`, `desktop-m4-firefox` and
`desktop-m5-network`. The browser and M9 profiles derive their package
lists from `desktop-m5-network`, so they inherit it.

The SIGPWR unit follows the reference rationale: a drop-in that adds
`Wants=poweroff.target` does not start a shutdown, because special
targets are started by the manager objective; a unit that calls
`systemctl poweroff` does. The reference commit verified this with the
amd64 image and QEMU `system_powerdown`. It has not been re-verified on
RISC-V, and QEMU `virt` has no ACPI, so this is inert in the current
QEMU testbed.

## Verification

The builder test in `test_debian_m5_network` now writes an
`/etc/hostname` and a debootstrap-style `/etc/hosts` into its fixture
stage and asserts, after running the real
`configure_and_normalize_rootfs`:

- the exact sudoers content and mode 0440;
- the devpts fstab line;
- the appended `127.0.1.1 asterinas-fixture` hosts line;
- the SIGPWR unit content and its `sigpwr.target.wants` symlink.

Run:

```text
python3 -m unittest tools.riscv.tests.test_debian_m5_network.DebianDesktopM5NetworkTests.test_builder_installs_m4_desktop_and_m5_network_evidence -v
ok
```

The profile identities are frozen tuples, so
`test_debian_rootfs` was updated as well: the `desktop-m3` and
`desktop-m4` exact package lists gain `sudo`, and the schema v3/v4
lock fixtures gain `("sudo", "riscv64", "1.9.16p2-3")` (the trixie base
version).

Broad run over the affected modules:

```text
python3 -m unittest tools.riscv.tests.test_debian_rootfs \
  tools.riscv.tests.test_debian_browser_web \
  tools.riscv.tests.test_debian_signed_sources \
  tools.riscv.tests.test_debian_m9_software \
  tools.riscv.tests.test_debian_browser_m5_runtime_gate \
  tools.riscv.tests.test_debian_m6_browser \
  tools.riscv.tests.test_debian_m8_browser_quality
Ran 367 tests in 33.7s
FAILED (failures=1)
```

The single failure is the pre-existing
`test_classifier_requires_ordered_application_evidence`
("missing guest framebuffer evidence"), already recorded in
`2026-10-27-mainline-sysctl-merge.md`.

## The missing gmac input

`build_rootfs.sh` installs `gmac_single_packet_evidence.py` and
`browser_web_runtime_digest` hashes it, but the file never reached
`main` from the partial import `5b9d96cb6`. Every
`desktop-m5-network`, `browser-m5`, `desktop-m9-software` and
`browser-web` build failed at install time, and three tests failed on
the missing input.

`4e468f925` restores the 136-line script verbatim from `5b9d96cb6`,
and adds the five digest inputs that the runtime-digest test omitted
from its fixture copy list. The script only sends one nonce-bound UDP
datagram for the single-packet unit; it contains no GPU or DRM code.

## Pre-existing failures

- `test_classifier_requires_ordered_application_evidence`: known
  classifier drift; reproduced with the profile change stashed.
- `test_desktop_core_evidence_does_not_probe_netsurf`: the evidence
  script is killed by its own `timeout` (rc 137). Confirmed on the tree
  with these changes stashed.

## Not done in this round

- No fresh rootfs image was built, so the new files were not observed
  in a booted guest. The frozen r122c image still lacks sudo and the
  other entries; a console login/`apt-get update`/ACPI-poweroff gate
  needs a newly built image.
- `build_rootfs.sh`'s `configure_profile` case list still does not
  accept `desktop-m4-firefox`; the profile exists only for manifest
  validation. A daily-use image rebuild needs that profile wired into
  the builder first.
