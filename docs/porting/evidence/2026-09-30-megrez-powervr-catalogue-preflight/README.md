# Megrez PowerVR catalogue register preflight (2026-09-30)

This change adds an opt-in register transaction for the selected
BVNC 30.3.408.101 firmware MMU root. It does **not** start firmware, prove
a GPU page walk, submit a command, or render a pixel. The default boot path
does not write the catalogue registers.

The pinned RockOS source at commit
[`bf2ec5d53002c16bc1bc593b92516eb6c2866176`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/server/devices/volcanic/rgxstartstop.c)
selects the firmware mapping context, fences that write by reading it back,
then writes the page catalogue address. The matching register definitions
give context at `0xe140`, base at `0xe148`, a 4 KiB address shift, and a
28-bit encoded address field. The selected firmware configuration maps
FWPRIV and FWIF to context 0. Asterinas now checks the initial context and
base, writes and reads back the selected root, then selects context 0 before
clearing its base on close. If cleanup cannot be confirmed, it poisons the
control lease and retains the DMA allocations rather than freeing memory
that the device could still reference.

The transaction is guarded by `asterinas.powervr_mmu_preflight=1` and only
occurs after all four firmware DMA segments and their page tables have been
prepared. This flag is **not yet enabled on the development board**. In the
vendor `RGXStart` path, `RGXResetSequence` and firmware wrapper setup precede
`RGXInitBIF`, which installs this catalogue, and the firmware processor is
released afterward. Asterinas has not reproduced that full startup order.
Register readback alone is not evidence that the GPU can access these pages.

QEMU kernel tests each selected one test and reported zero failures:

| Test | Result |
| --- | --- |
| `selected_catalogue_base_is_encoded_read_back_and_cleared` | 1 passed, 0 failed |
| `selected_catalogue_rejects_bad_root_without_register_mutation` | 1 passed, 0 failed |
| `selected_catalogue_cleanup_selects_context_zero_before_clearing` | 1 passed, 0 failed |

The last test uses banked fake registers: clearing the base before selecting
context 0 leaves context 0's firmware address installed, and the regression
test detected that order. The RISC-V release kernel build exited successfully;
the resulting Image SHA-256 was
`604b07b4dd473b1f93fb6c9595eabe75e69c312acb408a6bfbbf83388cced162`.
These are compile and QEMU results only. A controlled board register test,
firmware execution, GPU completion fence, and hardware pixel readback remain
open. The board's default RockOS boot entry must remain available for that
test.
