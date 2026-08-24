---
date: 2026-08-24
mode: diff
base: 9236061e8
head: 3a0e64a75
branch: codex/megrez-usb-keyboard-main-convergence
title: "RISC-V PCI xHCI USB keyboard M1 review"
---

# Summary

This M1 change keeps the proof layers separate: PCI DT-resource validation and
exclusive MMIO ownership, xHCI/USB HID attachment, a guest evdev oracle, and a
host-side evidence gate. The custom Sv39/SMP=4 QEMU run reached the PCI, USB,
evdev, and exact key-event markers, published `passed: true`, and completed
process cleanup without a panic marker.

The five-persona review found one Important hardware/correctness issue: after
accepting QEMU's valid pre-operational extended capabilities, the MMIO validator
did not explicitly reject extended or fixed controller regions that overlap.
Commit `d66e8b287` added pairwise fixed-region and extended-capability overlap
checks plus focused kernel tests; the complete QEMU gate was then rerun and
passed. No Critical or Important findings remain.

The external combined review pass was bounded and terminated before it emitted
valid JSON. The final result therefore comes from the repository persona
checklists, direct diff inspection, focused unit/kernel checks, strict Clippy,
and the full custom runtime gate rather than an unverified partial external
report.
