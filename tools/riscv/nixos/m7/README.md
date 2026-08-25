<!-- SPDX-License-Identifier: MPL-2.0 -->

# NixOS M7 credential reproducer

Purpose: validate AF_UNIX SCM_RIGHTS and SO_PEERCRED required by nix-daemon.

Provenance: track/nixos commit 8a7396a1fae4dfce21b2d0e19794b83dd7771bd8.

Scope: source fixture only; build/boot integration belongs to the R3 child issue.

The checked-in fixture is adapted from that commit.
A parent listens in the Linux abstract AF_UNIX namespace,
a distinct child connects,
and SCM_RIGHTS is exchanged over the accepted connection.
The abstract address leaves no socket file;
bind collisions retry eight `(parent PID, attempt)` names
and preserve the selected address for the child.
The unique payload file is unlinked immediately.
A single five-second monotonic transaction deadline covers connection
acceptance, SCM_RIGHTS receive readiness, and child exit. Timeout and other
parent failures after fork attempt `SIGKILL` and WNOHANG reap under a separate
250 ms cleanup deadline. The child arms `PR_SET_PDEATHSIG(SIGKILL)` before and
after credential changes, so a parent lacking permission to signal the
65534:65534 child still exits promptly and the child dies with it. Cleanup
reports `EPERM`/`ESRCH` instead of entering an unbounded `waitpid`.

The focused test owns two explicit fault-injection arguments:
`--test-exit-before-connect` and `--test-stall-after-connect`. They verify the
pre-connect and post-connect timeout boundaries and are not acceptance modes.

Build and run the default host check from the repository root:

```sh
set -eu
repro_dir="$(mktemp -d)"
repro_bin="$repro_dir/asterinas-scm-repro"
trap 'rm -f -- "$repro_bin"; rmdir -- "$repro_dir"' EXIT
chmod 700 "$repro_dir"
cc -Wall -Wextra -Werror tools/riscv/nixos/m7/scm_repro.c \
  -o "$repro_bin"
"$repro_bin"
```

The default mode proves only that SO_PEERCRED reports the distinct connecting
child PID. Its marker is
`__M7_PEERCRED_PID_OK__ pid=... distinct_ids=0`; it deliberately makes no
UID/GID isolation claim because an ordinary host child inherits the parent's
IDs.

For the full credential boundary on the host,
run the complete build recipe above as UID 0,
then invoke strict mode from the same shell:

```sh
"$repro_bin" --require-distinct-ids
```

The host `cc` command above produces a host-architecture binary only.
Do not copy the host binary into a RISC-V guest.
For the future Asterinas/QEMU runtime acceptance,
copy `scm_repro.c` into a RISC-V guest that provides a native C toolchain,
then build it inside that guest:

```sh
cc -Wall -Wextra -Werror /root/scm_repro.c \
  -o /root/asterinas-scm-repro
```

Then run this command inside the root guest:

```sh
/root/asterinas-scm-repro --require-distinct-ids
```

The child calls `setgid(65534)` and then `setuid(65534)` before connecting.
Only a parent-observed child PID with UID/GID 65534 emits
`__M7_PEERCRED_OK__ pid=... uid=65534 gid=65534 distinct_ids=1`.
This command is an acceptance recipe, not an Asterinas/QEMU runtime PASS.
