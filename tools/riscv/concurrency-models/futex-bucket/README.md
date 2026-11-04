# Futex bucket model

A loom model of the protocol in `kernel/src/process/posix_thread/futex.rs`:
`futex_wait_bitset` enqueues an item under the bucket lock and sleeps on its
waker, `futex_wake_bitset` drains matching items under the lock and wakes them,
and `futex_requeue`/`futex_cmp_requeue` move items to another bucket so a later
wake on the *target* bucket reaches them.

This module already produced one real failure in this port — LTP's
`futex_cmp_requeue01` on four CPUs panicked the run queue — so its wake/requeue
bookkeeping is worth an executable spec.

| Test | What it checks |
| --- | --- |
| `a_wake_reaches_a_waiter_that_already_enqueued` | the basic wake, including the race between parking and the wake |
| `a_requeued_waiter_is_woken_on_the_target_bucket` | a requeued waiter is woken by a wake on the target bucket |
| `a_wake_on_the_source_bucket_leaves_the_requeued_waiter_to_the_target` | a wake that finds the source bucket empty does not consume the waiter's token |
| `two_waiters_are_both_woken_by_two_wakes` | wake counts do not lose a waiter |

Wakes are ordered after the enqueue (a channel), which is the futex contract:
`FUTEX_WAKE` only affects waiters that are already queued. The park/unpark
token is the one `waitqueue-waker` verifies.

```sh
cargo test                      # ~3 minutes
LOOM_MAX_PREEMPTIONS=6 cargo test
```

## Scope

The model verifies the bucket bookkeeping and the wake token, not the futex
word check, the page-fault-in of the word, bitsets, timeouts, or signal
interruption. Those interact through `futex_wait_bitset`'s value compare and
its `remove_by_waker` cleanup, which the model does not restate.
