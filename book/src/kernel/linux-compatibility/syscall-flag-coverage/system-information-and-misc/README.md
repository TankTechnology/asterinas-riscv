# System Information & Misc.

<!--
Put system calls such as
uname, getrlimit, reboot, setrlimit, sysinfo, times, gettimeofday, clock_gettime,
clock_settime, getrusage, getdents, getdents64, personality, syslog,
arch_prctl, set_tid_address, and getrandom
under this category.
-->

### `arch_prctl`

Supported functionality in SCML:

```c
{{#include arch_prctl.scml}}
```

Unsupported codes:
* `ARCH_GET_CPUID` and `ARCH_SET_CPUID`

For more information,
see [the man page](https://man7.org/linux/man-pages/man2/arch_prctl.2.html).

### `getrusage`

Supported functionality in SCML:

```c
{{#include getrusage.scml}}
```

Unsupported `who` flags:
* `RUSAGE_CHILDREN`

For more information,
see [the man page](https://man7.org/linux/man-pages/man2/getrusage.2.html).

### `getrandom`

Supported functionality in SCML:

```c
{{#include getrandom.scml}}
```

Silently-ignored flags:
* `GRND_NONBLOCK` because the underlying operation never blocks

For more information,
see [the man page](https://man7.org/linux/man-pages/man2/getrandom.2.html).

### `reboot`

Supported functionality in SCML:

```c
{{#include reboot.scml}}
```

Unsupported `op` flags:
* `LINUX_REBOOT_CMD_CAD_OFF`
* `LINUX_REBOOT_CMD_CAD_ON`
* `LINUX_REBOOT_CMD_KEXEC`
* `LINUX_REBOOT_CMD_RESTART2`
* `LINUX_REBOOT_CMD_SW_SUSPEND`

For more information,
see [the man page](https://man7.org/linux/man-pages/man2/reboot.2.html).

### `sysinfo`

Supported functionality in SCML:

```c
{{#include sysinfo.scml}}
```

Supported returned fields:
* `uptime`, rounded up to seconds when it has a fractional part
* `loads`, containing the 1-, 5-, and 15-minute load averages as fixed-point values scaled by `1 << SI_LOAD_SHIFT`
* `totalram`, `freeram`, `procs`, and `mem_unit`

The `sharedram`, `bufferram`, `totalhigh`, and `freehigh` fields are reported as zero.
The `totalswap` and `freeswap` fields are reported as zero because swap is not supported.

For more information,
see [the man page](https://man7.org/linux/man-pages/man2/sysinfo.2.html).

### `syslog`

Supported functionality in SCML:

```c
{{#include syslog.scml}}
```

Actions 0 through 10 share a retained record store with `/dev/kmsg`.
The destructive `READ` cursor, non-destructive clear marker,
and per-open device cursors are independent.
Console controls change printing without disabling capture.

The store retains 256 whole records with at most 1024 message bytes each;
oversized kernel messages end in `[truncated]`.
`SIZE_BUFFER` reports the message capacity, excluding record metadata and output prefixes.
Timestamps use microsecond units but currently have millisecond resolution.
Messages emitted before the Asterinas logger is installed are not retained.
At least warning-level messages are captured after installation,
even when the boot console is disabled;
more verbose capture follows the boot log level.

`/proc/sys/kernel/dmesg_restrict` defaults to `1`.
Privileged access accepts `CAP_SYSLOG` in the initial user namespace,
with the historical `CAP_SYS_ADMIN` fallback;
this is not the newer Linux policy that requires `CAP_SYSLOG` alone.
When restriction is disabled, only `READ_ALL` and `SIZE_BUFFER` become
unprivileged syslog actions, and readable device opens are unrestricted by this policy.
Device filesystem permissions still apply.

`/dev/kmsg` provides whole escaped records, independent readers,
blocking reads, polling, and overwrite detection with `EPIPE`.
Readers are notified through a periodic timer, not directly from the logging path.
User writes cannot claim the kernel facility and are limited to 1024 input bytes.
`/proc/kmsg`, configurable log buffer sizing, and printk rate-limit controls
are not provided by this implementation.

For the interface contract,
see [the syslog manual](https://man7.org/linux/man-pages/man2/syslog.2.html)
and [the Linux device ABI](https://www.kernel.org/doc/Documentation/ABI/testing/dev-kmsg).

## POSIX clocks

### `clock_gettime`

Supported functionality in SCML:

```c
{{#include clock_gettime.scml}}
```

Unsupported predefined clock IDs:
* `CLOCK_REALTIME_ALARM`
* `CLOCK_BOOTTIME_ALARM`
* `CLOCK_TAI`

For more information,
see [the man page](https://man7.org/linux/man-pages/man2/clock_gettime.2.html).

### `clock_nanosleep`

Supported functionality in SCML:

```c
{{#include clock_nanosleep.scml}}
```

Unsupported clock IDs:
* `CLOCK_TAI`

For more information,
see [the man page](https://man7.org/linux/man-pages/man2/clock_nanosleep.2.html).
