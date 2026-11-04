# Waiter / Waker model

A loom model of ostd's `Waiter`/`Waker` handshake (`ostd/src/sync/wait.rs`),
the mechanism under `WaitQueue` and therefore under every blocking
`read`/`write`, `poll`, `futex` wait, and mutex park.

The protocol:

```text
Waker::wake_up:  if has_woken.swap(true) { false } else { unpark(task); true }
Waiter::wait:    while !has_woken.swap(false) { park until has_woken or unpark }
```

`has_woken` is the token that makes a wake before (or during) a park visible to
the waiter, and the waiter's `swap(false)` makes two wakes collapse into one.
The model keeps both operations in the same order and replaces the kernel's
park with a `Condvar`/`Mutex` pair, so loom explores every interleaving of wake
and park.

A lost wake shows up as a deadlock: the waiter parks with `has_woken` false and
no unpark left to deliver.

Tests:

- `wake_before_wait_is_not_lost`
- `every_wake_from_two_wakers_unparks_the_waiter`
- `one_wake_is_delivered_at_most_once` — two concurrent wakes deliver exactly
  one, which is what keeps the run queue from receiving a duplicate wake.
- `a_closed_waiter_is_never_woken`

```sh
cargo test
LOOM_MAX_PREEMPTIONS=6 cargo test
```

The model's `Waker` targets the thread that will park (as `Waiter::new_pair`
does); binding it inside the waiter thread is what makes loom's `Condvar`
schedule the right task.
