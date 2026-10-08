/* SPDX-License-Identifier: MPL-2.0 */

.text

# Exercise the production trap return with a stale saved CPU-local base.
# The continuation repairs gp before executing any Rust, even on a broken kernel.
.balign 4
.global riscv_test_kernel_trap_return_gp
.type riscv_test_kernel_trap_return_gp, @function
riscv_test_kernel_trap_return_gp: # (saved_gp: usize) -> observed_gp
    # 34-word trap frame, original gp/sstatus/sepc, and alignment padding.
    addi sp, sp, -38 * 8
    .irp reg, 1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31
        sd x\reg, \reg * 8(sp)
    .endr
    sd sp, 2 * 8(sp)
    sd gp, 34 * 8(sp)
    csrr t0, sstatus
    sd t0, 35 * 8(sp)
    csrr t1, sepc
    sd t1, 36 * 8(sp)

    # Return to S-mode with IRQs still disabled (SIE = SPIE = 0).
    ori t0, t0, {SSTATUS_SPP}
    andi t0, t0, ~((1 << 1) | (1 << 5))
    sd t0, 32 * 8(sp)
    lla t0, .Lriscv_test_gp_returned
    sd t0, 33 * 8(sp)
    sd a0, 3 * 8(sp)
    j _trap_return

.Lriscv_test_gp_returned:
    mv a0, gp
    ld gp, 34 * 8(sp)
    ld t0, 35 * 8(sp)
    csrw sstatus, t0
    ld t0, 36 * 8(sp)
    csrw sepc, t0
    addi sp, sp, 38 * 8
    ret
.size riscv_test_kernel_trap_return_gp, .-riscv_test_kernel_trap_return_gp
