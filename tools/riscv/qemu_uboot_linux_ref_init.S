/* SPDX-License-Identifier: MPL-2.0 */
/*
 * Minimal init for the sifive_u Linux reference run.
 *
 * Linux passes rdinit=/init with console=ttySIF0 already active, so this
 * writes the completion marker directly to fd 1 (the console) and spins.
 * It must not depend on devtmpfs or a specific /dev node.
 */

.section .rodata

marker:
    .ascii "ASTERINAS_LINUX_REFERENCE_READY\n"
marker_end:
    .equ marker_len, marker_end - marker

.text

.global _start
.type _start, @function
_start:
    li a0, 1
    la a1, marker
    li a2, marker_len

3:
    /* Retry errors and advance partial writes so success can never be a truncated marker. */
    li a7, 64
    ecall
    blez a0, 3b
    sub a2, a2, a0
    add a1, a1, a0
    bnez a2, 3b

2:
    j 2b
    .size _start, . - _start

    .section .note.GNU-stack,"",@progbits
