# Shebang script pathname and `argv[0]`

The pinned LMBench native script needed a `grep -E` adaptation because a
PATH-launched shell wrapper could not open its own script. A direct oracle
isolated the kernel behavior: `execve` an absolute `#!/bin/sh` script with
`argv[0] = "caller-supplied-argv0"` and `argv[1] = "payload"`. Linux printed
`SCRIPT0=/tmp/asterinas-shebang-argv-oracle.sh ARG1=payload` and the child
exited zero. On the existing Megrez Asterinas boot
`80a2b08d-e2aa-4f6b-b3ae-9394770f8384`, `/bin/sh` instead tried to open
`caller-supplied-argv0` and exited 2. The [board serial record](board-oracle.serial.log.gz)
shows that failure on the previously selected Image SHA-256
`3e3707b7e59f46303a914395a8dd4a85de55d0feb063fa26bb510130ce02b40e`.

The loader now replaces the caller's `argv[0]` with the script pathname after
the shebang interpreter and its optional argument. It retains the filename
passed to `execve`, and for a relative `execveat` path constructs Linux's
`/dev/fd/<fd>/...` pathname. The short
[`execve_shebang_argv` regression](../../../../test/initramfs/src/regression/process/execve/execve_shebang_argv.c)
also runs in the general process suite; `AUTO_TEST=shebang_argv` isolates it
for a cheap QEMU gate.

The C regression compiled with `-Wall -Wextra -Werror` and passed on Linux.
Before the kernel change, the focused RISC-V QEMU gate failed with the shell
trying to open the forged argument; see the [red log](red-qemu.log.gz).
After the change, the same gate passed; see the [green log](green-qemu.log.gz).
The resulting QEMU Image SHA-256 was
`78ec31caa7cad3db6cd5f896bd2f7776c45317d071793a7f564d9f14bb82cac9`.
The adjacent `AUTO_TEST=memfd_exec` gate also passed on this kernel; its
[log](memfd-qemu.log.gz) checks existing `execveat`/memfd behavior.
The [structured result](result.json) binds the observations and identities.

## Debian `egrep` wrapper control

The frozen Debian root image contains `/usr/bin/egrep` as a 41-byte shell
wrapper that executes `grep -E`. A separate focused QEMU probe ran the same
`PATH=/usr/bin:/bin egrep '^MAJOR' egrep-proof` command from `/run` on the
old and new Images, using the identical Debian root image. On the old Image,
`/bin/sh` reported `cannot open egrep` and the probe failed; on the new Image,
the command returned the expected `MAJOR=3` and the QEMU driver passed.
The [old serial log](debian-egrep-red.serial.log.gz),
[new serial log](debian-egrep-green.serial.log.gz), and
[old](debian-egrep-red-qemu.json) and [new](debian-egrep-green-qemu.json)
QEMU summaries record the exact kernel and root-image hashes.

No new kernel was deployed to Megrez for this change. After the oracle, two
separately reopened connections to the stable by-id serial device again
proved UID 0 on the same old boot ID, `systemd` on ext2, active desktop and
browser services, watchdog 0, and a bounded route lookup. See the
[first](board-handoff-1.serial.log.gz) and
[second](board-handoff-2.serial.log.gz) handoff records. The serial descriptor
was closed; the independent board boot menu and RockOS default were unchanged.

This verifies the absolute-path shebang case, the actual Debian `egrep`
wrapper, and the adjacent ELF `execveat` regression. Relative-path and nested
shebang behavior remain unverified. At this stage, the LMBench package still
has its `grep -E` adaptation; a native ALL run without that adaptation is the
next qualification step.
