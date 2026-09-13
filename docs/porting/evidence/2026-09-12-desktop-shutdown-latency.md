# Desktop shutdown and signal validation

## Outcome and scope

Two independently reproduced defects are repaired: an already-stopped process
did not resume when SIGCONT was ignored, caught or blocked; and the Stage1
interactive Bash ignored TERM. **Normal desktop shutdown is still slow.**
One diagnostic-console run recovered in 18.940 s, but the final normal-console
configuration took 106.963 s. The faster observation is not a qualified fix.

The board is recovered to RockOS, SSH works, and offline partition-2 checking
passes. The installed networking kernel and default menus are unchanged.
No shutdown timeout was reduced, filesystem synchronization skipped, physical
reset issued, or second reboot request sent during a quiet shutdown interval.

The scoped repairs are local commits `62d3720bc` (signal semantics/regression)
and `5714dd940` (generated console/test). No remote push was performed.

## Established signal and shell defects

`test/initramfs/src/regression/process/signal/stop_continue.c` tests four
SIGCONT dispositions (default, ignored, caught and blocked) using both kill
and tgkill. A pipe handshake establishes readiness, then WUNTRACED confirms
an actual SIGSTOP before CONT is sent. WCONTINUED is checked before the child
can exit. Handler execution and blocked/pending masks are checked separately.
The test is registered in the process regression runner.

Linux passes eight cases; the old Asterinas image passes only the two default
cases. Enqueuing SIGCONT now resumes the process through either signal route,
independently of eventual signal delivery. Ordinary review identified remote
sender lifetime races; process/parent weak references are upgraded conditionally.
The default delivery action is retained. Full STOP/CONT cancellation and
concurrent stop-state serialization are **not** implemented by this repair.
Linux's generation-time behavior is specified in
[signal.c](https://github.com/torvalds/linux/blob/v6.12/kernel/signal.c).

The Stage1 generator now installs `trap 'exit 0' TERM` before console readiness.
Its new host test compiles the actual C generator, starts interactive Bash,
verifies command execution, and sends TERM while stdin remains open. Before
the fix this timed out after three seconds; afterward Bash exits with zero.
SIGINT behavior and service restart policy are unchanged. A runtime trap alone
did not improve the old kernel's physical recovery time.

The regression owns and reaps failed child cases, including terminal statuses
seen during setup. Its default SIGALRM watchdog does not run atexit cleanup;
it is an overall fail-stop bound, not a guarantee of cleanup on every failure.

## Frozen artifacts and software verification

All paths below are relative to local `target/shutdown-latency/` unless stated
otherwise. Builds reused the persistent container and cached tools offline.
No container image or toolchain download was required.

| Artifact | SHA-256 |
| --- | --- |
| Baseline kernel, `../rseq-safety/kernel.Image` | `b7510cc3b2a41a203b53b7697db4b854a0cbbed8b72707537fc3b39c2885f675` |
| Repaired kernel, `kernel.Image` (5,881,832 bytes) | `5da7586448d45ed5c8b65e1b3a34ebd87a45de54d0df19dd51ab42fa50bc4457` |
| Old Stage1 | `d62ab8325e03ec9959a82336ad7ddab2d433d9e6dfc1006d6237fa9f6c80c1e0` |
| Repaired Stage1, `stage1/initramfs.cpio` | `9ef253dadad3f399e6152ad8681319725e99562081f237ef851a7ec11c3ba405` |
| Unchanged DTB | `465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba` |
| Final canary menu | `6064fddb71a5d3e3efc07b6e455b9da4c999071cba00d0f630c0c9c8f8bcc39f` |

- Latest signal regression: `signal-reviewed-before/` passes 2/8 and
  `signal-reviewed-after/` passes 8/8, with identical initramfs SHA-256
  `eaab4e052d35dac1af44c2008af3d63803911b01b0c726d281940c53d03954c7`.
  The guest markers are respectively `SIGNAL_RESULT=1` and `SIGNAL_RESULT=0`.
  Both QEMU processes exit zero; host exit status alone is insufficient.
  Native Linux also passes (`signal-reviewed-host.log`).
- `signal-smoke/` passes stop_continue, kill, signal_test2, job_control and
  the previous strict rseq regression. The smoke used the pre-final cleanup
  revision of stop_continue; that cleanup revision was separately rerun above.
- Basic and automatic Probe pass with both the old and repaired Stage1:
  `qemu-{basic,probe}/` and `final-qemu-{basic,probe}/`. This is not a full
  Debian desktop QEMU qualification.
- The 135 selected Stage1, debug-console, boot-menu and serial host tests pass
  (`host-tests-final.log`). The initial aggregate invocation lacked the tools
  PYTHONPATH and had an import error; it is not counted as a behavioral failure.
- Rust formatting, new C regression formatting, shell syntax and whitespace
  checks pass. Existing build warnings remain; full CI/lint is not claimed.
  The final repeated 135-test/format/syntax command also exits zero
  (`handoff-verification.log`).

Host test selection, inside the persistent container:

```sh
PYTHONPATH=.:tools/riscv python3 -m unittest \
  tools.riscv.tests.test_debian_rootfs.DebianStage1Tests \
  tools.riscv.tests.test_debian_debug_console \
  tools.riscv.tests.test_megrez_boot_menu \
  tools.riscv.tests.test_megrez_board_session -v
```

## Five physical desktop runs

Each run sent one `sync; systemctl --force reboot`. Times are from request
dispatch to the fresh U-Boot prompt, not to the first firmware byte or RockOS.
Diagnostic menus changed tty0 to ttyS0 and enabled systemd console/info logs.
All images were content-addressed canaries, not replacements of defaults.

| Run | Kernel / shell / console | U-Boot prompt |
| --- | --- | ---: |
| Control | Old / unchanged / normal | 106.815 s |
| Trap only | Old / runtime TERM trap / normal | 107.079 s |
| Diagnostic control | Old / unchanged / serial logs | 106.908 s |
| Repaired diagnostic | New / runtime TERM trap / serial logs | 18.940 s |
| Final configuration | New / generated TERM trap / normal | 106.963 s |

The diagnostic control logged TERM at 0.958 s, then listed openbox, bash,
runuser, Xorg and dbus-daemon as waiting at 10.961 s. SIGKILL escalation,
filesystem detach and "Rebooting" arrived at 90.982 s; first firmware arrived
at 93.982 s. Firefox was not in the waiting list. Thus this run's major delay
is userspace process termination, not a 90-second SBI or ext2 sync operation.
The sequence matches systemd 257's
[killall](https://github.com/systemd/systemd/blob/v257/src/shared/killall.c) and
[shutdown](https://github.com/systemd/systemd/blob/v257/src/shutdown/shutdown.c)
paths. It does not identify every target process's internal wait condition.

The repaired diagnostic run reached reboot at 3.007 s and first firmware at
6.007 s. Its Firefox window was observed at 59.912 s. With final normal
arguments, console readiness was 9.757 s and window observation 69.353 s.
Window polling is coarse and these are non-randomized observations, not a
statistically established Firefox startup improvement. In both runs, a local
HTML/JavaScript assertion, DOM click handler and 1920x1080 framebuffer PNG
capture passed (`fixed-content.log`, `final-content.log`). This turn did not
transfer/visually inspect those PNGs or test physical USB input or the Internet.

Final recovery verified the generated trap before reboot; Bash printed exit
at 1.235 s, yet the U-Boot prompt still took 106.963 s. RockOS login completed
at 133.845 s from reboot-command dispatch, excluding the initial query.
Normal-console shutdown logs are not
available on serial, so the remaining waiters are not established. Logging
timing and console routing remain differences from the single fast run.
See `*-recovery.log.json` and their chunk-timed `*.events.jsonl` files.

## Separate pending-signal gap and next experiment

The local diagnostic `pending_stop_cont.c` blocks both SIGTSTP and SIGCONT in
fresh child processes. It sends either order through all four process/thread
queue combinations, first verifies that the first signal is pending, then
checks that the second cancels the first while remaining pending itself.
Native Linux passes 8/8; the repaired kernel passes 0/8 in 0.542 s, with both
signals still pending (`pending-host.log`, `pending-after/`). Its initramfs
SHA-256 is `6a860670049eacad18d3c1d8234bdea12cd271534e54563fb991469e52e75ab2`.
This is an intentionally failing standalone diagnostic, not a passing regression.

The missing cancellation is established; its causal role in the final physical
shutdown is not. A follow-up must cover all process/thread pending queues and
the race where a stop signal was already dequeued when CONT arrived. Clearing
one queue or merely reordering dequeue priority is not a complete solution.
Use deterministic queue tests and bounded shutdown-like QEMU tests before
further physical repetitions. The 19-second observation is not a release gate.

## Final board state

`final-rockos-audited.log` records RockOS 6.6.87, `PARTITION2_UNMOUNTED=1`, and
`e2fsck -fn /dev/mmcblk1p2` exit zero: 19,505/131,072 files and
231,205/524,288 blocks, followed by `E2FSCK_EXIT=0`. The SSH command also
returns zero. Installed SHA-256 values remain:

- Kernel: `485b9079c204bf6b34055f5e1061f3011381557d8cc4b4bf2d1e4831922058c1`.
- Default Asterinas menu: `02280720efe7a1ad0ac084cdc20429406631e12d2e16f05638544bab0883fb26`.
- Vendor menu: `eb5f39a6e2db71ccc93ae005c488fd9f7ef353e75426e5a51e8923a9cbf2ebc5`.

No network integration or additional syscall implementation is claimed here.
The earlier [fault-window/rseq findings](2026-09-12-fault-window-and-rseq.md)
and missing-interface limits still apply.
