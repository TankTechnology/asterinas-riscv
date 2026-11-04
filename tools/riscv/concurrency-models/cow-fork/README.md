# Copy-on-write fork model

A loom model of the protocol `fork` relies on to share memory safely:

- `kernel/src/vm/vmar/vmar_impls/fork.rs` — `cow_copy_pt` marks the parent's
  mappings read-only and maps the same frames into the child.
- `kernel/src/vm/vmar/vm_mapping.rs` — `handle_single_page_fault` copies on a
  write fault, unless `frame.reference_count() == 1`, in which case it upgrades
  the PTE in place.

The invariant: **a writable mapping's frame is otherwise unreferenced** — no
other address space maps it and the VMO page cache does not hold it. Sharing a
writable frame would make one process's writes visible in another.

Ops modeled after a fork: write fault (upgrade or copy), unmap/exit (drop a
reference), and a read fault that re-maps the frame from the page cache. Each
address space has its own page-table lock; the reference count and cache flag
are loom atomics.

| Test | What it checks |
| --- | --- |
| `buggy_protocol_allows_a_shared_writable_frame` | Without the `reference_count() == 1` check, loom **finds** a shared writable frame. |
| `fixed_protocol_keeps_writable_frames_private` | Fault vs fault: no shared writable frame in any interleaving. |
| `fixed_protocol_survives_fault_vs_unmap` | Fault vs unmap: upgrade-in-place only after the other side released the frame. |
| `fixed_protocol_survives_fault_vs_unmap_vs_remap` | A page-cache remap cannot resurrect a frame the parent made writable. |

```sh
cargo test
LOOM_MAX_PREEMPTIONS=6 cargo test
```

## Scope

This is bounded model checking of the refcount/fault protocol, not a proof
about `fork`. It does not model the page-table contents, TLB shootdown
ordering, `handle_page_faults_around`, process creation, file tables, or signal
state. It answers one question: does the *decision rule* (copy unless the frame
is solely referenced) preserve the CoW invariant under every modeled
interleaving — yes, and the model shows what a version without the rule breaks.
