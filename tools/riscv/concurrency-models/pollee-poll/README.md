# Pollee register / check / notify model

A loom model of `Pollee::poll_with` and `Pollee::notify`
(`kernel/src/process/signal/poll.rs`), the layer under `poll(2)`, `epoll`, and
every socket and tty wait.

The model covers two things:

- **Mask merge in `register_poller`.** `poll(2)` accepts duplicate entries for
  one descriptor, and the entries may carry different masks. OpenSSH polls a
  pty with `POLLIN` and again with an empty mask that only asks for
  `ERR`/`HUP`. `SyncSubject` keeps one filter per observer, so before
  `66d0290f7` the second registration replaced the first, `notify(IN)` matched
  nothing, and the client slept forever on a pty that had data. That is the
  hang that made OpenSSH sessions stop responding.
- **The ordering protocol.** `poll_with` registers, reads the cached state,
  claims it with a `swap`, then checks the object; `notify` publishes the
  readiness, invalidates the cached state with `Release`, and only then takes
  the subject lock. The model keeps that order so the checker can verify the
  documented guarantee: if there are interesting events, either the events are
  returned or the poller is notified.

Tests:

- `buggy_protocol_drops_a_duplicate_fd_wakeup` — loom must find the lost
  wakeup in the pre-fix version.
- `fixed_protocol_keeps_every_duplicate_fd_interest` — the invariant holds in
  every explored interleaving.
- `readiness_published_before_registration_is_seen_by_the_check` — a producer
  that ran first must not be lost either: the registration's lock acquisition
  orders the poller's state read after the invalidation.
- `always_poll_events_are_added_to_every_mask` — `ERR`/`HUP` are structural.

```sh
cargo test
LOOM_MAX_PREEMPTIONS=6 cargo test
```

The model is bounded model checking over an extracted protocol: it does not
cover the kernel's interrupt masking or the parts of `poll_with` that the model
does not restate (the `Task` pointer in the state is a constant here).
