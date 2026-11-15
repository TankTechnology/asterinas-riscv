# Firefox targeted diagnostics: first implementation and microtest

## Outcome and claim boundary

The first diagnostic slice observes outstanding syscalls instead of relying only on returned calls.
It also records bounded process lifecycle events and Marionette frame progress.
The RISC-V QEMU microtest passes with diagnostics both enabled and disabled.
This is not Firefox recovery, a complete wait/wakeup graph, or physical-board acceptance.
No new physical-board run or full Firefox run was performed.

The work is based on `cc66b9d0b2b90c046abd7ffa6db2fb0e86a9e2d0`
in the existing `codex/megrez-physical-graphics-current-main` worktree.
Pre-existing graphics changes are preserved; this batch is uncommitted.
The earlier causal hypotheses remain in
`docs/porting/evidence/2026-09-08-firefox-blocking-chain-analysis.md`.

## What the new observations distinguish

| Layer | Observation | What it does not establish |
| --- | --- | --- |
| Marionette | Request ID/name, send completion, partial frame header/body, failure stage | That Firefox processed a command merely because the client sent it |
| Thread syscall | Current entry and 32-completion history, sequence, six scalar registers, result/errno, kernel ticks | A scheduler wait reason, peer identity, or missing wakeup |
| Process lifecycle | Clone/exec/wait result, normal/signal exit, final wait-encoded status | Complete history after the boot-wide log budget is exhausted |
| Targeted proc collection | Selected root/descendants, all budgeted TIDs, status/comm, bounded fd/fdinfo | An atomic whole-tree snapshot or guaranteed PID identity across races |
| IPC microtest | EAGAIN → SCM_RIGHTS send → epoll readable → receive/read → wait/second-wait | Firefox's multiprocess execution path or a real-board driver pass |

The kernel flag is `asterinas.syscall_diag=1`, default off.
When enabled, constant-size per-thread bookkeeping is system-wide;
the Python collector selects the process tree to export.
The custom file is `/proc/<pid>/task/<tid>/asterinas_syscall`,
with `/proc/<pid>/asterinas_syscall` as the main-thread alias.
It is not an implementation of Linux's `/proc/<pid>/syscall` ABI.
Each open captures immutable JSON; reopen to refresh it.

The proc interface uses filesystem-credential alien-access checks on open and every read,
and hides records from an earlier VM generation after exec.
Arguments are scalar registers, never dereferenced user buffers.
They can still contain addresses, so captured artifacts should remain private.
The kernel implementation remains safe Rust.

Lifecycle output uses OSTD at `loglevel=info`,
with 1024 records per boot and finite power-of-two suppression summaries.
Startup can consume that budget before Firefox starts.
Proc snapshots remain available even if serial lifecycle output is suppressed.
This limitation must be checked before interpreting an absent lifecycle record.

## Informative RISC-V microtest

The retained experiment directory is `target/firefox-diagnostics-20260908/`.
Both runs use the same kernel and initramfs, QEMU `virt`, four harts and 2 GiB RAM.
Only `asterinas.syscall_diag=1` differs between the on/off boot commands.
No Debian root image, network device, Xorg, or Firefox is needed.
The host gives each run a 45-second timeout plus a three-second kill grace.
The C probe itself has a ten-second alarm and a three-second epoll timeout.

The probe first receives from an empty nonblocking Unix socket before creating a sender;
this ordering establishes EAGAIN without timing sleeps.
A child sends one byte and one pipe descriptor using `SCM_RIGHTS`.
The parent checks readiness, ancillary-message shape/truncation, descriptor contents,
exit status 23, and ECHILD on the second wait.

Observed in both runs:

- All eight expected `IPC_DIAG` phases report `ok=true`.
- The transferred descriptor reads `F` (70).
- `waitpid` observes exit status 23; the second wait returns errno 10.
- The guest reaches `SYSCALL_DIAG_MICRO_END` and QEMU exits with status 0.

With diagnostics enabled, the additional sleep target has
`current.number=115` (`clock_nanosleep`), sequence 40,
entered at tick 2851 and sampled at tick 3124.
Its last completed call remains at sequence 39.
The self-read succeeds as well.
Serial logs contain the IPC child's `normal_exit` with status 5888 (`23 << 8`),
the selected sleep's signal exit with signal/status 9,
and wait's `result=-10 outcome=error`.
With the flag omitted, both proc snapshots report `enabled=false`
and null call fields, and no `syscall_diag lifecycle=` records appear.

`validate_micro.py` asserts these properties and writes `micro-result.json`;
it does not infer acceptance from printed success markers alone.
The shell sampling delay selects an outstanding sleep for this smoke test;
it is not a timing-independent race regression.

| Retained input | SHA-256 |
| --- | --- |
| `kernel.Image` | `66b61ff120be1c033c20777b6cca86fe86bf8fe775a9df6d90e7ec276730628c` |
| `micro.cpio` | `66035dd266c0d5cbeeb89f4ee105b3a1d8fa6e5010a9d97b6da8261d98d0cb05` |
| `initramfs/ipc-event-probe` | `901ae053c1b4538d0420ca743411a86cf7b227f3fc99b543687ac5ba9569ff71` |
| `micro-on.serial.log` | `f6b5abbfc15ec0c97168dd7ec72ad55294be79605a6b1e6cad2c3957516b83d9` |
| `micro-off.serial.log` | `44e5ee6111f7e9c8fe6cdaeed5740e5263ed0e7a8ea6567809e07a738e6e7f9a` |

The native Linux reference uses the cached native C compiler.
The distro cross-GCC command lacks development headers in this container,
and the cached Nix toolchain lacks static libc.
The successful RISC-V build therefore uses the already cached Nix cross-GCC
and its dynamic glibc, copied into this small initramfs alongside cached BusyBox.
No compiler, libc, cargo-osdk, or package was downloaded or installed.

## Use the collector without depending on browser responsiveness

On the guest, with the selected root PID already known:

```sh
umask 077
timeout -k 2 6 /usr/lib/asterinas/firefox-diagnostic-snapshot \
    --root-pid 355 --max-seconds 3 --max-processes 16 \
    --max-threads 128 --max-fds 64 --max-file-bytes 8192 \
    --max-total-bytes 262144 > /run/firefox-snapshot.json
```

Replace 355 with the PID for that run; do not reuse a historical PID.
Exit 0 means complete collection, while exit 1 retains explicit completeness limitations.
The command neither contacts Marionette nor invokes systemctl or ps.
It distinguishes unsupported interfaces, disabled diagnostics, permission errors,
disappeared processes, invalid records, and exhausted resource budgets.
If the final identity check cannot run because of a budget,
partial evidence is retained but marked unverified.
If identity actually changes, that process's mixed payload is discarded.
Ancestor identities are rechecked before and after descendant collection;
a changed or unverifiable ancestor invalidates its selected subtree.
This conservatively omits already collected descendants if the final ancestor read
cannot fit in the remaining budget, and explicitly reports incomplete ancestry coverage.
An outer host deadline is still necessary if the guest kernel cannot interrupt a proc read.
The output is not a whole-tree atomic observation.

For the existing Marionette client, opt in with
`ASTERINAS_MARIONETTE_DIAGNOSTICS=1` in that client's environment.
The `A_WEB_MARIONETTE_TRANSPORT` lines contain no script, URL query, nonce,
screenshot, or response-body payload.
At most four records are emitted per command, not per received fragment.
The existing public protocol errors and absolute deadline remain unchanged.
Do not enable the separate payload-bearing `ASTERINAS_MARIONETTE_DEBUG_ERRORS`
when only these transport diagnostics are wanted.

## Verification and remaining work

All commands use the persistent launcher with this worktree and `--offline`:

```sh
/mnt/shared/xaj/Program/asterinas/tools/docker/run_dev_container.sh \
  --workspace /home/ubuntu/.config/superpowers/worktrees/asterinas/megrez-physical-graphics-current-main \
  --offline -- make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
```

The final focused Python suite passes 180 tests in 8.014 seconds,
including a harness that separately runs eight actual Rust helper tests.
The native C program compiles with `-O2 -Wall -Wextra -Werror` and executes successfully.
A native Linux proc capture explicitly reports the custom interface as unsupported,
not as successful empty evidence.
Marionette tests use real sockets for partial frames, EOF, invalid framing/JSON/identity,
and fragmented reads without changing the absolute deadline.
The closed diagnostic-sink regression preserves the original transport timeout.
Collector fixtures exercise scope, missing interfaces, permissions,
symlink rejection, invalid JSON, byte/time/scan limits, and partial evidence retention.
Source-text integration assertions are not represented as live permission tests.

Independent specification and Asterinas persona-based quality reviews found
an ancestor-PID-reuse selection bug and a closed diagnostic-sink exception bug.
Both were reproduced before repair and independently re-reviewed afterward.
Malformed integer/register/completion records are now rejected as invalid schema.
The review record is retained under `target/firefox-diagnostics-20260908/review/`.
Non-blocking encapsulation/refactoring suggestions and the pre-existing Marionette
Boolean-identity acceptance remain separate follow-ups;
this diagnostic batch does not change protocol-validation semantics or claim merge readiness.

The offline overlay supports an explicit per-file `create: true` for the new collector,
while absent destinations remain errors by default.
Twenty-four overlay tests cover existing-directory-only creation, symlink rejection,
deterministic images, atomic failure after a staged addition,
and failed ownership readback for both UID 1000 and GID 1000.
They also cover misleading type text in symlink targets, exact schema-version typing,
incorrect timestamp writes, exit-zero command errors, metadata-checksum rejection,
TMPDIR paths with spaces, and unreused inode deletion-time determinism.
The final scoped independent re-review found no remaining blocking issue in those fixes.
The cached witness base remains
`76c57bfbd74ed1a45cc387408e2334efed5d1e2b184c77bce941ff31e603318a`.

The final packaged rootfs is `target/firefox-diagnostics-20260908/browser-rootfs-final/`,
not the earlier pre-review `browser-rootfs/` artifact.
It contains 18 audited runtime files and was materialized in 5.804 seconds.
Its image SHA-256 is
`8598c4e07f542e714d623270737de41526f60ff7c5713ab1395821abab665b14`.
`final-overlay-result.json`, the rootfs's derivation manifest,
`focused-tests.log`, and `source-SHA256SUMS` retain build/test and source identities.
The packaged collector and client have not yet been exercised together in a Firefox guest run.

Live cross-credential/FD-transfer protection, exec races, and multi-thread ID promotion
remain outside the runtime smoke test.
No claim is made about observer overhead under Firefox load or lossless lifecycle coverage.
The next informative browser experiment should correlate transport request ID,
bounded before/during/after thread snapshots, and lifecycle records from one kernel/rootfs pair.
Only after identifying a persistent outstanding syscall should a second-stage probe
instrument that primitive's wait registration and wakeup boundary.
This avoids treating an isolated futex/epoll snapshot or ECHILD warning as a root cause.

The next experiment should keep the page, Firefox profile, kernel configuration,
rootfs packages, and Marionette deadline fixed.
Collect at most three bounded tree snapshots: before ExecuteScript,
while its response remains outstanding, and after timeout or completion.
Retain host monotonic timestamps around each collection as well as guest ticks;
these clocks must not be silently equated.
Compare the selected PID's start time across captures and preserve explicit partial coverage.
If a content process exits, match its PID and wait status to the lifecycle record.
If it survives in a persistent `recvmsg`, epoll, or futex call,
use fd metadata and code paths to select exactly one deeper wait/wakeup probe.
An enabled diagnostic run should then be compared with the same inputs and the flag off
before attributing a timing difference to a kernel defect.
