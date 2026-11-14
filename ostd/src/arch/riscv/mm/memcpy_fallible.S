/* SPDX-License-Identifier: MPL-2.0 */

.text

# Copies `size` bytes from `src` to `dst`. This function works with exception
# handling and can recover from a page fault. The source range must not overlap
# with the destination range (In virtual address level. Their corresponding
# physical addresses can be overlapped).
#
# Returns number of bytes that failed to copy.
#
# The byte-at-a-time loop this used to cost about six instructions per byte,
# which is what a 4 KiB page copy turned into ~20 us on the board and capped
# buffered reads at ~120 MiB/s. Copy eight bytes per iteration instead; the
# head and the tail stay byte-wise because they cannot be aligned safely, and
# every load and store keeps its exception-table entry so a fault still
# returns the number of bytes left.
#
# Both pointers must be aligned to the same offset before the word loop runs.
# That looks weaker than aligning the destination alone, and it is: a user
# copy has a page-aligned kernel side and a user side at whatever offset
# `read()` was handed, so the loop only fires for aligned user buffers. It is
# also the only safe choice here. A misaligned access does not run at a slight
# penalty on this core (EIC7700): it traps and the kernel emulates it, measured
# at 7 MiB/s on the board against 116 MiB/s for the byte loop and 202 MiB/s for
# this loop. So the trade is: word-wise when jointly aligned, byte-wise
# otherwise, never a misaligned load or store.
.global __memcpy_fallible
.type __memcpy_fallible, @function
__memcpy_fallible: # (dst: *mut u8, src: *const u8, size: usize) -> usize
    li t1, {SSTATUS_SUM}
    csrs sstatus, t1

memcpy_align:
    beqz a2, memcpy_exit
    or t2, a0, a1
    andi t2, t2, 7
    beqz t2, memcpy_words
memcpy_lb:
    lb t0, (a1)
memcpy_sb:
    sb t0, (a0)
    addi a0, a0, 1
    addi a1, a1, 1
    addi a2, a2, -1
    j memcpy_align

memcpy_words:
    li t3, 8
    blt a2, t3, memcpy_tail
memcpy_word_loop:
memcpy_ld:
    ld t0, (a1)
memcpy_sd:
    sd t0, (a0)
    addi a0, a0, 8
    addi a1, a1, 8
    addi a2, a2, -8
    bge a2, t3, memcpy_word_loop

memcpy_tail:
    beqz a2, memcpy_exit
memcpy_tail_loop:
memcpy_tail_load:
    lb t0, (a1)
memcpy_tail_store:
    sb t0, (a0)
    addi a0, a0, 1
    addi a1, a1, 1
    addi a2, a2, -1
    bnez a2, memcpy_tail_loop

memcpy_exit:
    mv a0, a2
    csrc sstatus, t1
    ret
.size __memcpy_fallible, .-__memcpy_fallible

.pushsection .ex_table, "a", @progbits
    .balign 8
    .quad memcpy_lb
    .quad memcpy_exit
    .quad memcpy_sb
    .quad memcpy_exit
    .quad memcpy_tail_load
    .quad memcpy_exit
    .quad memcpy_tail_store
    .quad memcpy_exit
    .quad memcpy_ld
    .quad memcpy_exit
    .quad memcpy_sd
    .quad memcpy_exit
.popsection
