/* SPDX-License-Identifier: MPL-2.0 */

.section .rodata

marker:
    .ascii ">>> Hello from RISC-V userspace on Asterinas! <<<\n"
marker_end:
    .equ marker_len, marker_end - marker
serial_path:
    .asciz "/dev/ttyS0"

.text

.global _start
.type _start, @function
_start:
    li a0, -100
    la a1, serial_path
    li a2, 2
    li a3, 0
    li a7, 56
    ecall
    bgez a0, 1f
    li a0, 1

1:
    mv s0, a0
    la a1, marker
    li a2, marker_len

3:
    /* Retry errors and advance partial writes so success can never be a truncated marker. */
    mv a0, s0
    li a7, 64
    ecall
    blez a0, 3b
    sub a2, a2, a0
    add a1, a1, a0
    bnez a2, 3b

    /* Echo loop: read one byte from ttyS0 and write it back. */
2:
    mv a0, s0
    la a1, echo_buf
    li a2, 1
    li a7, 63
    ecall
    blez a0, 2b
    mv a0, s0
    la a1, echo_buf
    li a2, 1
    li a7, 64
    ecall
    j 2b
    .size _start, . - _start

    .section .bss
echo_buf:
    .skip 1

    .section .note.GNU-stack,"",@progbits
