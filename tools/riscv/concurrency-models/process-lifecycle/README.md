# Process lifecycle model (fork / exit / wait)

A TLA+ model of the rules that make `fork`, `exit` and `wait` safe:

- `kernel/src/process/wait.rs` — `wait` and `reap_zombie_child`;
- `kernel/src/process/status.rs` — the zombie flag and the exit code;
- `kernel/src/process/posix_thread/exit.rs` — exit commits the status.

What is checked:

| Invariant | Meaning |
| --- | --- |
| `ReapRequiresExit` | a reaped process has an exit status: it exited before it was reaped |
| `OnlyParentReaps` | the process that reaped a child was the child's parent at that moment (after re-parenting, init may reap) |
| `NoOrphan` | every live process and every zombie has a living parent |
| `Disjoint` | a process is running, a zombie, or gone — never two |
| `InitLives` | init never exits; it is the reaper of last resort |

The `BUGGY` constant selects pre-fix behaviours, and each has a config that
must produce a counterexample:

| Config | BUGGY | Expected |
| --- | --- | --- |
| `ProcessLifecycleFixed.cfg` | `"none"` | no error |
| `ProcessLifecycleReapRunning.cfg` | `"reap_running"` | `ReapRequiresExit` violated |
| `ProcessLifecycleNoReparent.cfg` | `"no_reparent"` | `NoOrphan` violated |
| `ProcessLifecycleReapAnyone.cfg` | `"reap_anyone"` | `OnlyParentReaps` violated |

```sh
../fetch-tla.sh
python3 ..//run.py --only process-lifecycle
```

## Scope

Safety only: the model has no liveness properties (a parent that never waits
leaves its zombie forever, which is POSIX behaviour), no memory ordering for
the status publication (the status is a TLA+ variable, so publication is
atomic by construction), and no scheduler or signal delivery. It checks the
lifetime bookkeeping that `wait4`/`exit` implement, not the syscall layer.
