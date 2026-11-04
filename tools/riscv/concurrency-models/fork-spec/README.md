# Fork specification model

A TLA+ model of what `fork` promises, expressed over the same structures the
kernel uses: physical frames, per-process address mappings, frame reference
counts, and process states.

| Invariant | Meaning |
| --- | --- |
| `CoWIsolation` | a frame mapped in two processes is never writable in either — one process's write can never be observed by another |
| `RefsAccurate` | a frame's reference count equals the number of mappings of it |
| `ReapRequiresExit` | a reaped process exited before it was reaped |
| `OnlyParentReaps` | a child is reaped by its parent (init after re-parenting) |
| `NoOrphan`, `Disjoint`, `InitLives` | lifecycle bookkeeping |

Actions: `Fork` (private read-only copies of the mappings, reference counts),
`WriteFault` (upgrade in place only when the frame is solely referenced,
otherwise copy), `Write`, `Exit` (drop the address space, publish the status,
re-parent children), `Reap`, `Idle`.

| Config | Behaviour | Result |
| --- | --- | --- |
| `ForkSpecFixed.cfg` | as implemented | no error, 2,855,358 distinct states in ~80 s |
| `ForkSpecNoRefCheck.cfg` | write fault always upgrades in place | `CoWIsolation` violated |

```sh
../fetch-tla.sh
python3 ../run.py --only fork-spec
```

## This is a proof of the design, not of the code

The model is written by hand from the kernel's structures; nothing checks that
`kernel/src/vm/vmar` and `kernel/src/process` implement it. Closing that gap
(a refinement proof of the Rust code) is a different project — the scope,
tooling status and blockers are in
[docs/porting/2026-11-04-fork-proof-scope.md](../../../../docs/porting/2026-11-04-fork-proof-scope.md).
