# Lock order model

A check of the kernel's multi-lock acquisition paths against a total order.
A set of locks with a total order cannot deadlock — every waiter holds only
lower-indexed locks while asking for a higher one — so encoding the order and
the paths that take two locks is a proof of deadlock freedom for those paths.

Lock classes, in acquisition order:

| Order | Lock | Paths |
| --- | --- | --- |
| 0..63 | futex buckets, by bucket index | `lock_bucket_pairs` (`futex.rs`) |
| 64 | VMAR `RwLock` | `Vmar::fork_from` (write), page faults (read) |
| 65 | ext2 `Inode::inner` | file I/O, truncate, writeback |
| 66 | VMO `pages` (XArray) | `commit_on*`, `resize`, `collect_pages` — held briefly, released before the page lock |
| 67 | page lock (`LockedCachePage`) | page-cache I/O, writeback — one page at a time |
| 68 | `BlockPtrTree` | the page-cache backend's block lookup |
| 69 | `IndirectBlockManager` | under `BlockPtrTree` |
| 70 | `Ext2::super_block` | block allocation |
| 71 | `BlockGroup::metadata` | block/inode bitmaps |

`futex_wait_bitset` drops the bucket lock before calling `handle_page_fault`,
so a futex bucket is never held while taking the VMAR or page-table locks.
The full checklist, including the open items the audit found, is
[docs/porting/2026-11-04-lock-order-checklist.md](../../../../docs/porting/2026-11-04-lock-order-checklist.md).

| Test | Checks |
| --- | --- |
| `requeue_in_both_directions_keeps_the_total_order` | `requeue(A,B)` racing `requeue(B,A)` |
| `every_bucket_pair_is_ordered` | all 64×64 bucket pairs, including the same-bucket case (locked once) |
| `a_private_file_page_fault_follows_the_order` | VMAR → cursor → XArray → page lock → block tree |
| `buffered_io_and_truncate_follow_the_order` | inode → page lock → block tree → metadata, with the release between page lock and block manager |
| `writeback_waits_for_the_page_before_taking_the_page_table` | page lock, release, then XArray |
| `a_futex_bucket_is_never_held_across_a_fault` | bucket released before the fault path |
| `the_model_rejects_inversions` | seven inversion sequences, each rejected |
| `unsorted_bucket_acquisition_is_rejected` | negative control |

## Scope

This checks the locking *policy*, not interleavings: the total-order argument is
static. It does not enumerate schedulers, and it does not cover locks whose
order depends on data (e.g. inode locks, file-table locks, or the socket
registry), which would need their own order document.
