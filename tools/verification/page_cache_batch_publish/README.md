# Page-cache batch publication model

This bounded TLA+ model checks the lock/publication/submission order used when
several cold page-cache pages are prepared for one backend read batch.

Run from the repository root:

```bash
bash tools/verification/page_cache_batch_publish/run.sh
```

The runner reuses the repository's cached TLC 1.7.4 JAR after verifying its
SHA-256 and never downloads a replacement. Artifacts are written below
`target/page-cache-batch-publish-model/`.

## Code correspondence

- `PrepareLockedBatch` represents `BackedVmo::commit_range` holding the XArray
  lock, locking each newly allocated `CachePage`, and only then storing it.
- `SubmitBatch` represents calling `PageCacheBackend::read_pages_async` before
  waiting on any existing uninitialized page.
- `CompletePage` represents a BIO callback marking the page up to date and
  dropping its `LockedCachePage`.
- `PublishBeforeLock` is a negative control for the tempting implementation
  that publishes all pages and then locks them in caller-dependent order before
  submitting any I/O.

`NoUnsubmittedWaitCycle` rejects the state where two collectors are both
waiting while no owned page has reached the device. TLC must find this state in
the negative control and exhaustively prove it absent in the corrected two-page,
two-collector abstraction.

## Limits

This is exhaustive model checking of the stated finite abstraction, not a proof
of the Rust compiler output, XArray implementation, device driver, or weak-memory
behavior. Runtime KTests separately check page initialization, one-read
ownership, backend errors, and batch submission.
