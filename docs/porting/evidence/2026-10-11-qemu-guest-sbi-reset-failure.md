# QEMU guest SBI reset-failure boundary (2026-10-11)

This is an actual Asterinas guest run under QEMU, not the host serial-peer
fault fixture. It remains simulation evidence: no physical serial device,
board MMIO, hardware watchdog, or board reset was used.

## Setup

The run reused the prepared QEMU virt recovery payload and the guarded
`qemu_uboot_booti.py` runner inside the
`qemu-eic7700-builder-20261004` container. The profile was `generic-sv39`,
with the guest command line overridden to:

```text
console=ttyS0 loglevel=info init=/init asterinas.reboot_after=10
```

Because this profile has no recovery contract, the runner deliberately added
QEMU `-no-reboot`. The guest therefore exercises its real software recovery
timer and SBI reset call, while the hypervisor refuses to start a second
firmware epoch.

The retained runner result and serial transcript are:

- `target/qemu-guest-faults/sbi-reset-failure-20261011/result.json`
- `target/qemu-guest-faults/sbi-reset-failure-20261011/serial.log`
- `target/qemu-guest-faults/sbi-reset-failure-20261011/receipt.json`

## Observed boundary

The serial log contains exactly one `ASTERINAS_SOFTWARE_REBOOT_ARMED` marker
and one `ASTERINAS_REBOOT_TIMER_READY` marker. The guest reaches userspace,
then requests the restart. QEMU exits cleanly with `returncode=0`, the process
group is cleaned up, and no second OpenSBI epoch appears.

The receipt classifies this as:

```text
execution_scope=qemu-guest
classification=sbi-reset-failure
qualified=false
recovery_state=not-recovered
opensbi_epochs=1
uboot_epochs=1
watchdog_kind=software-recovery-timer
```

U-Boot prints its version twice in this run (the banner and the guarded
`version` command); the parser records that as `uboot_banner_count=2` but
correctly treats OpenSBI as the firmware-epoch boundary.

## Interpretation

This closes the guest-level SBI-reset-failure negative case and prevents the
runner from silently treating a clean QEMU exit as successful recovery. It
does not prove hardware watchdog recovery, RockOS fallback, or physical SBI
behavior. Timer-disabled and permanent-MMIO-block cases still require their
own guest/QEMU injection mechanisms; the separate host fault matrix remains
explicitly scoped as `execution_scope=host-serial-peer`.
