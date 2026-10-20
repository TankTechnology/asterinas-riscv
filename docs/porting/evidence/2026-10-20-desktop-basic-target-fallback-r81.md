# M4 basic-target activation fallback (r81)

- Gate: `desktop-basic-target-activation`
- Scope: make the M4 desktop service reachable before the graphical target
  while the minimal Asterinas systemd/PAM/logind transaction is incomplete.
- Status: `code-pass`; no Firefox scene, physical board, serial write, or
  hardware operation was performed.

## Diagnosis

The bounded r80 run reached `systemd 257.13` and the complete Stage1 handoff,
then ended with `reason=protocol` without any `ASTERINAS_DESKTOP_*` marker:

`/mnt/mydisk/qemu-desktop-m4-r80-cow-stage1-120b/desktop-m4.serial.log`

The generated image had `asterinas-desktop-m4.service` linked only from
`graphical.target.wants`.  Its `After=`/`Wants=` relationship includes
`systemd-logind.service`; therefore a graphical-target transaction that is
waiting on PAM/logind can prevent the service from reaching `ExecStart` even
though the basic system is already running.

## Change

`tools/riscv/debian/rootfs/build_rootfs.sh` now also installs the M4 service
under `basic.target.wants`.  The graphical link is retained for normal Debian
semantics; systemd deduplicates the unit if both targets are later activated.
This changes activation reachability only and does not change the framebuffer,
DRM, Firefox, or input contracts.

## Verification

| Check | Result |
| --- | --- |
| TDD red test before source change | PASS (failed at missing basic-target symlink) |
| M4 builder contract test after change | PASS |
| `make test_riscv_debian_rootfs_unit` | PASS, 329 tests |
| Python compile, shell syntax, `git diff --check` | PASS |

## Next bounded gate

Materialize one new M4 qcow2 overlay and run a short service-activation gate.
Accept only a fresh ordered `ASTERINAS_DESKTOP_M4_SESSION phase=start` marker;
otherwise retain the failure receipt and inspect the next systemd boundary.
E3 remains unsatisfied until Firefox/WebRender evidence is collected.
