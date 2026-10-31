# Downstream pick ledger (2026-10-31)

## Base

- Upstream: `upstream/main` = `cdd5bc10907f752dc19a399297ea1fd57af20ee4`
- Rebase branch: `rebase/upstream-20261029` = `feature/ext4-jbd2`
- Source product branch: `feature/qemu-network-runtime` = `af48751df`
- Scope: QEMU-supported Debian/RISC-V surface. Board/GPU/DRM work remains out
  of scope unless separately approved.
- Current stack: 48 commits over upstream (23 feat/fix, 4 test, 20 docs, 1
  chore).

This ledger records the provenance of the current stack and the dependency
closure for the still-missing desktop/browser automation. It is updated in the
same change as each new pick. See
`.agents/skills/upstream-rebase-cherry-pick/SKILL.md` for the procedure.

## Delivered on the rebase line

| Feature | Representative source commit(s) | Current commit(s) | Action | Evidence |
|---|---|---|---|---|
| virtio-net 10-byte header | `ce2cf1662` | `5cf47ed96` | semantic port | `direct`/network gates |
| unprivileged port-zero connect | old product tree | `56218c47c` | new on rebase line | ktest + dsh |
| SCM_RIGHTS UNIX-socket passing | `79ee67872`,`f901a9f0f` | `f3830f7cb`,`d92b6530a` | semantic port + test | Firefox content process |
| klog, `/dev/kmsg`, boot identity, `syslog(2)` | old klog work | `415c233e4`,`978d0413b` | port/new | journald, `/dev/kmsg` probes |
| devtmpfs, `/dev/shm`, devpts/PTY | `3604162f8` and related | `977757632`,`2f42172df` | port | PTY probes |
| framebuffer graphics class + DT simple-framebuffer | `0ce8ab960` and related | `bd966a253`,`6cea7ade3` | port | desktop framebuffer evidence |
| rtnetlink set, IPv4 routes, `/proc/net/{dev,route,tcp,udp}`, netns | `ef469eb48`,`779667a03` | `fb837cf21`,`f5febeb7a`,`e9375313c`,`b32616377`,`980e47646` | port | QEMU network/procfs evidence |
| Debian rootfs/Stage1/direct gate/Image sidecar | old builders and gates | `fc78d33a8`,`25909f303`,`0881283db` | port/adapt | rootfs contract, direct gate |
| ext4 extents + JBD2 + crash gates | `294c225ce` | `30cd05424`,`7b2bfc87d`,`7c960b105` | superseded/expanded | 64-bit and crash gates |
| ext2 durability invariants | old ext2 work | `31acb7c3c` | port/new | ktests + ext2/e2fsck probe |
| host suite + aggregate product gate | old host suite, not cherry-picked wholesale | `2abf63a3a` | new on rebase line | 81 host tests, product receipt |
| QMP screendump/input + PPM audit helpers | `30e71ce3f`,`5c364ff75`,`0627c9983`,`4e8d063bc`,`d858c0c85` | `961b7ceb7` | file pick + tests | `test_qemu_qmp.py`, `test_qemu_ppm.py` |
| Debian gate framework (protocol/runtime/rootfs orchestration/systemd M2) | `239789a59`,`294c225ce`,`a7184cc59`,`8afb69e6e`,`aa98d0857`,`ef35d98a0` | `6b62ca317` | file pick + focused tests | `test_debian_gate_framework.py` |
| Debian desktop application gates (M3/M4 graphical shell + framebuffer classification) | `5ca126b73`,`df62a0419`,`58432ef36` | `9d597171c` | file pick + focused tests | `test_desktop_gates.py` |

`superseded` means the old implementation exists, but the current line's
implementation is the one to keep. Do not replace current modules with the old
copies merely for provenance.

## Pending: desktop/browser automation

Source commits:

| Source | Feature | Status |
|---|---|---|
| `5ca126b73` | Debian desktop application gate (`desktop_m4_gate.py`) | delivered in `9d597171c` |
| `df62a0419` | usable Debian desktop (profile/desktop files) | delivered gate portion in `9d597171c`; profile files still need merge review |
| `a0d6f2938` | browser-web desktop shell + bounded evidence gate | blocked; 8 conflicts when picked alone |
| `0dfa6f615` | two-boot daily-use QEMU gate | blocked; gate framework now present, browser/network closure still missing |
| `browser_web_qemu_gate.py` history | browser QEMU orchestration | blocked on browser/network helpers |

Dependency closure measured with
`.agents/skills/upstream-rebase-cherry-pick/scripts/local_import_closure.py`:

| Target | Closure | Missing after leaf + framework picks |
|---|---:|---:|
| `browser_web_desktop_shell_qemu_gate` | 24 | 9 |
| `browser_web_qemu_gate` | 19 | 6 |
| `desktop_m4_gate` | 10 | 0 |
| `desktop_m3_gate` | 9 | 0 |
| `rootfs_gate` | 7 | 0 |
| `qemu_qmp`, `qemu_ppm` | 2 | 0 |

Trial results:

- `0dfa6f615` applies cleanly to the rebase line but imports
  `desktop_m5_qemu_gate`, `gate_protocol`, `gate_runtime`, `rootfs_gate`,
  `rootfs_gate_backend`, and `systemd_m2_gate`; it is not runnable alone.
- `a0d6f2938` conflicts on eight paths, including
  `build_rootfs.sh`, `desktop_m5_session.sh`, and old rootfs tests.

Pick order:

1. ~~`qemu_qmp.py`, `qemu_ppm.py`~~ — done in `961b7ceb7`, with focused tests.
2. ~~`gate_protocol.py`, `gate_runtime.py`, `rootfs_gate.py`,
   `rootfs_gate_backend.py`, `systemd_m2_gate.py`~~ — done in `6b62ca317`.
3. ~~`desktop_m3_gate.py` / `desktop_m4_gate.py`~~ — done in `9d597171c`.
4. Browser helpers: `browser_web_contract.py`, `firefox_startup_profile.py`,
   `browser_web_qemu_gate.py`, `desktop_network_gate.py`,
   `desktop_network_qemu_gate.py`, `qemu_uboot_devices.py`; adapt the Megrez
   network fixture/proxy to the current local fixture rather than importing
   board-specific behavior.
5. `browser_web_desktop_shell_qemu_gate.py`.
6. Makefile/product-gate integration and evidence.

## Decisions

- **Skip** board/GPU/DRM/Megrez commits from the old tree; not part of this
  QEMU workstream.
- **Skip** the old ext4 implementation as a pick source; the current
  `30cd05424` plus `31acb7c3c` stack is the maintained implementation.
- **Do not** merge old branch history; the old and new trees have different
  paths and contracts.
- **Do not** pick a gate before its closure; clean textual application is not
  a runnable feature.

## Next action

Pick the browser helper layer next (`browser_web_contract`,
`firefox_startup_profile`, `browser_web_qemu_gate`, `desktop_network_gate`,
`desktop_network_qemu_gate`, `qemu_uboot_devices`), adapting the Megrez
network fixture/proxy to the current local fixture. Then re-run the closure
for the desktop shell gate and continue bottom-up. Every new pick gets a
`Picked-from:` or `Ported-from:` trailer and a row here.
