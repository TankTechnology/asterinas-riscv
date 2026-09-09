# Kernel Parameters

This section documents kernel command-line parameters supported by Asterinas.

## Inherited from Linux

### `init`

Run the specified binary as `init`.

Example:
```text
init=/bin/busybox
```

Notes:
- The value is the path to the executable.
- If omitted, Asterinas will try to execute from the following paths in order:
  `/sbin/init`, `/etc/init`, `/bin/init`, `/bin/sh`.

### `console`

Select console devices for kernel messages.
This parameter may be specified multiple times.
Kernel messages are delivered to each listed console.

Valid values:
- `tty0`
- `ttyS0`
- `hvc0`

Examples:
```text
console=ttyS0
console=ttyS0 console=hvc0
```

### `virtio_mmio.device`

Register a VirtIO-MMIO device from the kernel command line.
This parameter may be specified multiple times.

Format:
```text
virtio_mmio.device=<size>@<base>:<irq>[:<id>]
```

Notes:
- `size` and `base` may be decimal or hexadecimal with a `0x` prefix.
- `size` may use `K`, `M`, `G`, or `T` suffixes.
- `irq` must be nonzero.
- The optional `id` field is accepted for Linux compatibility but ignored.

Examples:
```text
virtio_mmio.device=0x200@0x5950f000:10
virtio_mmio.device=1K@0x1001e000:74
```

## Asterinas-specific
### `earlycon`

Enable the early console to output logs during the early stages of system boot.
The name follows Linux's `earlycon` parameter.
Asterinas currently supports a simplified form.

Example:
```text
earlycon
```

Notes:
- If omitted, the early console stays disabled.
- Only the bare `earlycon` token is supported;
  complex Linux forms such as `earlycon=uart8250,io,0x3f8,115200` are not supported yet.

### `loglevel`

Control how verbose kernel log output is on the console.
Set either a numeric value (`0` to `8`) or a lowercase level name.

Each value acts as a cutoff: messages at that severity and all more urgent levels are printed.
For example, `loglevel=4` shows emergencies through errors, but not warnings or routine info.

This uses the same `0`–`8` scale as the Linux kernel `loglevel` parameter,
with string aliases for convenience.

| Value | Name(s)            | Messages shown              |
|------:|--------------------|-----------------------------|
| `0`   | `off`              | None                        |
| `1`   | `emerg`            | Emerg only                  |
| `2`   | `alert`            | Emerg through Alert         |
| `3`   | `crit`             | Emerg through Crit          |
| `4`   | `error`, `err`     | Emerg through Error         |
| `5`   | `warning`, `warn`  | Emerg through Warning       |
| `6`   | `notice`           | Emerg through Notice        |
| `7`   | `info`             | Emerg through Info          |
| `8`   | `debug`            | All levels (most verbose)   |

Example:
```text
loglevel=4
loglevel=error
```

Notes:
- Level names are case-sensitive; use lowercase names.
- If omitted, the default is `8` (`debug`). Invalid values are ignored.
- Use `warn` for normal operation, `info`/`debug` when troubleshooting,
  and `error` or lower for a quieter console.

### `asterinas.klog_capture`

Set the retained kernel-log threshold independently of console output. It accepts
the same numeric values `0..8` and lowercase aliases as `loglevel`. The default
is `warning`, so warnings and more severe messages remain available through
`syslog(2)` and `/dev/kmsg` even when `loglevel=off`.

The effective capture threshold is the more verbose of `loglevel` and
`asterinas.klog_capture`; capture therefore never hides a record selected for
the console. Repeated parameters use the normal last-value-wins rule. An invalid
value falls back to `warning` and emits one warning after the retained logger is
installed. Runtime syslog console controls do not change this capture threshold.

For a quiet console with informational syscall lifecycle records retained for
`dmesg`, use:

```text
loglevel=off asterinas.klog_capture=info asterinas.syscall_diag=1
```

### `asterinas.syscall_diag`

Enable bounded syscall diagnostics with `asterinas.syscall_diag=1`.
The default is disabled.
While disabled, syscall entry and completion perform only the enable check:
they allocate no diagnostic storage, read no timer, and format no messages.
When enabled, each POSIX thread lazily allocates constant-size storage
for its current syscall and 32 most recently completed syscalls.
Bookkeeping covers all threads; a userspace collector selects the process tree to inspect.

Read `/proc/<pid>/task/<tid>/asterinas_syscall`,
or `/proc/<pid>/asterinas_syscall` for the main thread,
to obtain one immutable JSON snapshot per open file.
This is an Asterinas interface, not the Linux `/proc/<pid>/syscall` ABI.
Partial reads and seeks reuse the same snapshot; reopen to refresh it.
The file requires the existing read-level alien-access check using filesystem credentials.
Reads recheck access and return EOF after the target changes its VM through exec.
Records from the previous VM are hidden from new opens,
and discarded at the next syscall entry without resetting the thread's sequence.
IDs in the JSON are global kernel IDs, including after exec promotes a thread to main thread.

Version 1 contains `version`, `enabled`, `pid`, `tid`, `snapshot_jiffies`,
`current`, and `completed`. Version 2 adds `history`, an oldest-first array
of at most 32 completions; `completed` remains an alias for its newest entry.
An absent call is JSON `null`.
Each call contains `sequence`, `number`, six unsigned scalar registers in `args`,
and `entered_jiffies`.
A completion additionally contains `finished_jiffies`, `result`, and `outcome`.
The outcome is `return`, `error`, or `no_return`;
`result` is the signed return value or negative errno,
and is `null` when the syscall does not return normally or changes the user context.
While disabled, both call fields are `null` and version 2 history is empty.
Jiffies are kernel timer ticks, not wall-clock time or nanoseconds.
Arguments are not dereferenced: strings, environment variables,
and user payloads are never collected.

The same flag enables OSTD `info` lifecycle observations
for clone/fork, exec, wait outcomes, normal exit, and signal-driven exit.
The `normal_exit` and `signal_exit` records carry the final `termination_status`
encoded like `wait(2)` status; signal exits also include the signal number.
Each completed thread exit transition produces at most one exit record,
after completing any pending syscall diagnostic state.
Use `loglevel=info` (or `7`) to receive them;
`loglevel=off` suppresses these logs while proc snapshots remain available.
There is a boot-wide limit of 1024 lifecycle records,
followed by at most 64 `cap_exhausted` summaries
with cumulative suppression counts at powers of two.
Startup can exhaust this global budget before the selected workload runs;
an absent record does not establish that an event did not happen.
No serial message is emitted for ordinary syscall entry or completion.
Syscall lifecycle records report both the current `tid` and `entered_tid`
so successful exec's thread-ID change can be recognized.

### `asterinas.tcp_diagnostic_port`

Enable bounded scalar-only TCP tracing for connections whose local or remote
port matches the configured value. Values `1..65535` select a port; omitting
the parameter, using `0`, or using a value outside that range leaves the
facility disabled. The trace records no payload bytes or pointers.

At most 128 boundary records are emitted, followed by one suppression summary.
The records use `info` severity, so retain them with
`asterinas.klog_capture=info` when console output is disabled.

Example:

```text
loglevel=off asterinas.klog_capture=info asterinas.tcp_diagnostic_port=2828
```

### `asterinas.reboot_after`

On RISC-V, opt in to a software recovery deadline.
After the specified number of seconds,
the deadline requests an SBI cold reboot.

Example:
```text
asterinas.reboot_after=120
```

Notes:
- This parameter is supported only on RISC-V.
  It is disabled when omitted or set to `0`.
- The value must fit in an unsigned 32-bit integer.
- Once armed, a fatal kernel panic also requests the recovery restart.
- This is an emergency debugging mechanism, not a graceful shutdown.
  It does not sync filesystems, unmount storage,
  or run userspace shutdown handlers.

### `asterinas.mmc_write_partition2`

On RISC-V Megrez systems, opt in to writes through the SD card's second
partition node. The MMC driver still rejects writes to the whole card and to
other partitions, and it enables the write path only when partition 2 matches
the recorded start LBA and sector count.

Example:
```text
asterinas.mmc_write_partition2
```

Notes:
- This parameter is disabled by default and is intended only for the bounded
  Megrez Debian provisioning gates.
- It does not authorize writes when the live partition-2 geometry differs from
  the frozen Megrez disk contract.
- Partition-table validation and a recoverable backup are still required before
  a real-card write test.

### `i8042.exist`

Override ACPI's indication of whether a PS/2 (i8042) controller exists.

Valid values:
- `1`, `on`, `yes`, `true` or no value — treat the i8042 controller as present (force probing)
- `0`, `off`, `no`, `false` - treat the i8042 controller as absent (skip probing)

Examples:
```text
i8042.exist
i8042.exist=1
i8042.exist=0
```
