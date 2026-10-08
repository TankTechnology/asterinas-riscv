# Native startup observation and bounded wait

This follow-up adds the startup observer and wait policy, with no production
caller or physical firmware release yet. It does not prove GPU execution or
complete Task 3.

The pinned RockOS `rgxpower.c::RGXPostPowerState` waits for SYSINIT's
`bFirmwareStarted == IMG_TRUE` after `RGXDoStart`. Native compatibility checks in
`rgxinit.c` use the firmware image header; the waits for OSINIT's compatibility
update flag apply to guests. The observer therefore uses the native started flag
without depending on guest compatibility or connection-state updates.

Before accepting that flag, it rejects any firmware fault count, HWR count, or
the pinned Volcanic HWR bits for reset, general lockup, DM stalling, firmware
fault and requested restart. A simultaneous HARDWARE_OK or DM_RUNNING_OK bit
does not override those indications. Started values other than 0/1 are rejected.
These observations are asynchronous: a successful observation is still not a
guarantee of command readiness, continued health, or pixel correctness.

The selected experimental wait uses a one-second deadline and at most 1,000
observations. It checks the deadline before reading and before accepting a
successful observation, propagates observation/wait errors, and rejects deadline
overflow. Even a stalled clock leaves the loop after 1,000 reads. The future
hardware adapter must provide a bounded wait interval and retain the exclusive
GPU owner and all DMA allocations throughout the operation. This shorter
experimental budget is our policy, not the vendor's system-wide timeout.

The [negative results](wait-red-results.txt) record two real RISC-V QEMU failures:
the initial unimplemented waiter rejected an expected successful sequence, and
the initial flag-only observer incorrectly accepted a started firmware with
faults. After implementation, both exact tests executed one test and passed;
see [positive results](wait-green-results.txt). Coverage includes delayed startup,
deadline expiry, a stalled clock, observation/wait errors, overflow, all five
failure bits, and native startup with zero guest compatibility/connection flags.
Neither negative QEMU process was killed or counted as passing.

## Initialization audit before the physical release

| Item | Current finding | Remaining action |
| --- | --- | --- |
| Native runtime priority/isolation/time-slice defaults | Zero matches `RGXSetupFwSysData`; do not copy non-native priority defaults | None for the selected native defaults |
| Active-PM latency | The selected [eswin_cpu/sysinfo.h](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/system/eswin_cpu/sysinfo.h) defines 0; the zeroed SYSINIT/runtime fields match | None for this field |
| GPU utilization initial words | Vendor fills timestamps and IDLE state | The later [owner-binding checkpoint](../2026-09-30-megrez-powervr-boot-binding/README.md) measures and initializes these fields |
| SYSINIT HWPerf BVNC metadata | Vendor fills BVNC, feature flags and counter blocks; current stage leaves zeroes | Audit the selected feature/block values and their use |
| SYS/OS config and HWR debug options | Some fields remain zero without a completed selected apphint comparison | Record the intended minimal configuration and validate required fields |
| META boot configuration | The earlier physical reset preflight already used Python-prepared code; the default staging manifest describes raw payloads | The later [owner-binding checkpoint](../2026-09-30-megrez-powervr-boot-binding/README.md) validates the prepared bytes against actual owned GPU addresses |
| Hardware wait adapter / startup request | Not installed | Connect only after the initialization checks, then validate bounded failure cleanup and RockOS recovery |

Read-only serial checks before and after reopening still returned UID 0, Linux,
and RockOS boot ID `2e6d0321-293a-4c67-810f-f21a3b8392fc` on the stable FTDI device
recorded in the parent checkpoint. The host log
`target/powervr-start-wait-control.serial.log` has SHA-256
`d78e0b7f1b6df5fe6165a88ee9ffb4be2149baed94027656224a1458395877b5`.
No reboot, GPU release, monitor check or Firefox performance run occurred here.
