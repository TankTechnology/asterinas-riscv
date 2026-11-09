# Boot warnings: the missing `=` lines, 2026-11-09

## Symptom

Every boot printed about thirty lines like:

```
WARN  : /etc/systemd/system/asterinas-desktop-m5-network.service:20: Missing '=', ignoring line.
...
WARN  : /etc/systemd/system/asterinas-desktop-m5-network.service:49: Missing '=', ignoring line.
```

The unit is a oneshot that probes the wired network. systemd parsed the file,
warned for every non-`key=value` line, and kept the rest of the unit working,
so the only visible effect was console noise.

## Root cause

`build_rootfs.sh` writes the unit through an **unquoted** heredoc, because the
body needs `$desktop_ordering` and a conditional `Environment=` line. A comment
in that body said:

```
# inactive instead of parked in `systemctl --failed`.  Evidence mismatches
```

Bash expands backticks in an unquoted heredoc exactly like `$(...)`, so the
**build host** ran `systemctl --failed` and substituted its output into the
file. The image unit therefore contained lines 20-167 of the host's failed-unit
table between two halves of the comment ("... parked in " and "`. Evidence
mismatches"). systemd warned on each row.

This also explains the absurd end of the block: `143 loaded units listed.` was
the host's footer, followed by the rest of our sentence.

## Fix

- The comment now uses `'systemctl --failed'`; a note above the heredoc warns
  that the body is shell-expanded.
- `test_debian_rootfs.py` scans every `tools/riscv/debian/rootfs/*.sh` heredoc
  and fails if an **unquoted** one contains a backtick. Quoted heredocs
  (`<<'EOF'`) are exempt.
- The board's copy was replaced with the correct file, `daemon-reload`ed, and
  the board was rebooted: the fresh boot has zero `Missing '='` warnings.

## What is left in the boot log

The fresh boot still shows a small set of warnings. They are real statements
about the kernel or the image, not parse noise:

| Warning | Disposition |
| --- | --- |
| `writing to /proc/sys/fs/nr_open is not supported` | The write path is deliberately unimplemented; the value is a constant. Dropping the sysctl file or making `nr_open` writable are both possible, neither is needed for boot. |
| `Failed to bump fs.nr_open` | systemd's own startup bump; same cause as above. |
| `Failed to find module 'unix'/'autofs4'`, `modprobe ... /sbin/modprobe` | Asterinas has built-in drivers; module loading is meaningless in this image. Cosmetic. |
| `TFD_TIMER_CANCEL_ON_SET is not implemented yet` | timerfd limitation. |
| `UNIX sockets in SCM_RIGHTS messages can leak kernel resource` | Compatibility-policy note; it used to fire on every socket-passing sendmsg (10 per boot). Now once per boot. |
| `use randomness based on the timestamp` | Early-boot RNG has no entropy source on this board. |
| Skipped units (TPM, hugepages, binfmt, debugfs, efi_pstore) | `ConditionPathExists`/`ConditionSecurity` doing their job. |
| `Switching to a task already running in the foreground` | Documented cross-CPU handoff spin in `ostd`; one line per event. |

## Board `/root` cleanup

The same pass removed superseded artifacts created while bringing up the
timesync kernel and the earlier epoll/pty experiments:

- kernels: `kernel-{alloc,epollfix,pollfix,ptyfix}.img`,
  `kernel-rel.f19053c4.img`, `kernel-rel.img.bak-20261104`,
  `kernel-rel.img.bak-20261105`, `kernel-rel.img.prev` (two of the backups
  were byte-identical to `kernel-rel.img.bak`; verified by sha256 first);
- debug binaries: `dup-poll`, `epoll-cycle-check`.

Kept: `kernel-rel.img` (the default), `kernel-rel.img.bak` (the pre-timesync
rollback), `bench`, the smoke scripts, and the login material. Free space went
from 106 MB to 181 MB.
