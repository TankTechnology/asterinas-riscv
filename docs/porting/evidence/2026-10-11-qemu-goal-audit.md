# QEMU goal audit (2026-10-11)

This was a read-only join of existing host/QEMU receipts. It did not start
QEMU, open a serial device, access board MMIO, reboot hardware, or change the
working tree's source artifacts.

The command was:

```bash
python3 tools/riscv/qemu_goal_audit.py \
  --output target/qemu-goal-audit-20261011/audit.json
```

The combined host suite passed **41/41** tests. The audit itself returned a
nonzero status because the evidence is intentionally incomplete:

| Check | Status | Evidence boundary |
| --- | --- | --- |
| Asterinas → firmware software recovery | passed | QEMU reaches a fresh U-Boot prompt |
| RockOS fallback | missing | QEMU virt bundle has no RockOS payload |
| Automatic boot-id receipts | insufficient | current U-Boot recovery receipt has no guest boot-id |
| fb0 userspace transaction | passed | normalized copy of the historical `desktop-m4` result |
| delayed guest timer fault | passed | QEMU-only timer-disabled receipt |
| SBI reset failure classification | passed | QEMU-only negative receipt |
| GMAC/DWC3 QTest | passed | current 19/19 TAP run |
| guest-scoped permanent MMIO block | insufficient | available matrix is host-serial-peer only |

The generated report is
`target/qemu-goal-audit-20261011/audit.json`. Its top-level fields are
`execution_scope=host-evidence-audit`, `hardware_state=not-accessed`, and
`passed=false`. The false result is a guard against overstating the remaining
RockOS and guest-MMIO work; it is not a QEMU test failure.
