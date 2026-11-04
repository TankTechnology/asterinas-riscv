# Lock order model

A check of the kernel's multi-lock acquisition paths against a total order.
A set of locks with a total order cannot deadlock — every waiter holds only
lower-indexed locks while asking for a higher one — so encoding the order and
the paths that take two locks is a proof of deadlock freedom for those paths.

Lock classes, in acquisition order:

| Order | Lock | Paths |
| --- | --- | --- |
| 0..63 | futex buckets, ordered by bucket index | `lock_bucket_pairs` (`futex.rs`), for `FUTEX_REQUEUE`/`FUTEX_CMP_REQUEUE` |
| 64 | VMAR `RwLock` | `Vmar::fork_from` (write), page faults (read) |
| 65 | page-table cursor lock | under the VMAR lock |

`futex_wait_bitset` drops the bucket lock before calling `handle_page_fault`,
so a futex bucket is never held while taking the VMAR or page-table locks.

| Test | Checks |
| --- | --- |
| `requeue_in_both_directions_keeps_the_total_order` | `requeue(A,B)` racing `requeue(B,A)` |
| `every_bucket_pair_is_ordered` | all 64×64 bucket pairs, including the same-bucket case (locked once) |
| `fork_and_fault_take_the_vmar_before_the_page_table` | fork and fault both take VMAR then page table |
| `argument_order_without_sorting_breaks_the_total_order` | negative control: unsorted acquisition is rejected |
| `page_table_before_vmar_would_break_the_total_order` | negative control: the inverse order is rejected |

## Scope

This checks the locking *policy*, not interleavings: the total-order argument is
static. It does not enumerate schedulers, and it does not cover locks whose
order depends on data (e.g. inode locks, file-table locks, or the socket
registry), which would need their own order document.
