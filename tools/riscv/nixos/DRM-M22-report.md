# DRM M22 resource-lifetime accounting and stress report

Date: 2026-08-28

## Result

M22 passes on the RISC-V Sv39, four-hart Asterinas guest with QEMU's
`virtio-gpu-gl-device` and virgl enabled. The gate completed 32 repeated
GEM/PRIME/context/fence lifetimes, and every reclaimable device-wide counter
returned exactly to its quiescent baseline after each round.

The final acceptance set also passes:

- RISC-V `cargo osdk build` with only seven pre-existing unrelated warnings;
- RISC-V `cargo osdk test`;
- M17 atomic KMS, 55 passes and no failures; and
- the complete NixOS Xorg/Xfce gate with DRI3 direct rendering, the virgl
  renderer, pixel read-back, a clean command stream, and `XFCE_DRM_PASS`.

The Xfce shader sample submitted 30 frames at 17.765 FPS in this run. This is a
semantic regression result, not a controlled performance comparison.

## What changed

DRM files now append device diagnostics to `/proc/<pid>/fdinfo/<fd>`. The
`drm-device-` prefix makes clear that the values are totals for the shared GPU,
not per-client accounting. The snapshot covers:

- the DUMB-pool used/capacity byte gauges;
- GEM objects, owners, and FLINK names;
- live and cleanup-only host resources;
- virgl contexts and resource attachments;
- retained fences and per-object fence associations;
- backend backing owners, active scanout, and cursor resources; and
- pending cleanup in the DRM, context, and backend layers.

The counts come from lifetime-owning maps or bounded aggregate counters.
Context destruction that cannot be confirmed is kept observable and retried
before attached host resources. Poisoning a context after an ambiguous
attach/detach operation now transfers failed destruction into the same retry
queue instead of leaving an unreported live host context.

The implementation was split into focused `resource_tracking` and `fdinfo`
modules. The first 64 MiB contiguous pool allocation now runs under a sleeping
mutex rather than a spinlock. Boot-pattern scanout is tracked alongside later
KMS presentation, and fdinfo no longer linearly sums GEM references or fence
associations while holding their spinlocks.

## Stress transaction

Each M22 round performs the following operations through the unmodified DRM
ioctl ABI:

1. open a worker render-node file;
2. create and map a 64×64×32-bit DUMB buffer;
3. create a backed virgl 3D resource;
4. export and re-import it through PRIME;
5. submit a fenced virgl NOP with the buffer in the BO list;
6. require `POLLIN` without `POLLERR`, `POLLHUP`, or `POLLNVAL`, then verify
   successful completion with `DRM_IOCTL_VIRTGPU_WAIT`;
7. close the imported handle and observe the live peak;
8. close the worker while retaining the PRIME fd and verify that GEM/resource
   ownership remains while its context and fences are gone; and
9. close the PRIME fd and require all reclaimable counters to match baseline.

The boot harness searches only output produced after each U-Boot command and
waits for a fresh prompt. It also tears down QEMU and temporary build state on
exception, so stale output or an interrupted run cannot produce a false pass.

## Evidence

The baseline and final snapshots were:

```text
baseline gem=0 refs=0 host=0 ctx=0 attach=0 fences=0 assoc=0
         backing=1 pending=0/0/0 pool=0/67108864 scanout=1 cursor=0
final    gem=0 refs=0 host=0 ctx=0 attach=0 fences=0 assoc=0
         backing=1 pending=0/0/0 pool=524288/67108864 scanout=1 cursor=0
```

All 32 rounds printed `baseline-restored`, followed by:

```text
M22_PASS all reclaimable resource counters returned to baseline after 32 rounds
M22_PASS dumb pool watermark grew only by the 524288 allocated bytes
M22_RESOURCE_STRESS_PASS
M22_RESOURCE_STRESS_GATE_PASS
```

The retained serial evidence is generated at
`target/drm-m22/evidence/serial.log`. The target directory is intentionally
untracked because it contains generated kernels, disks, and logs.

Run the gate from the repository root after building the RISC-V kernel:

```bash
tools/riscv/nixos/m22/build_m22.sh
tools/riscv/nixos/m22/boot_m22.py
```

## Review corrections

Two combined Asterinas persona reviews found thirteen issues before the final
runtime run: five major and eight minor. Their premises were verified against
the surrounding code; none were retracted. The major corrections covered
deferred poisoned-context cleanup, failed-fence false passes, stale U-Boot
output matching, contiguous allocation under a spinlock, and linear fdinfo
aggregation. The remaining findings drove cleanup safety, naming, module
separation, scanout accounting, and documentation changes.

Generated review artifacts are retained under `target/drm-m22-review/` and are
not part of the commit.

## Remaining limitation

M22 deliberately distinguishes live-resource leaks from DUMB-pool capacity.
The current pool is a 64 MiB non-reusing bump allocator because an established
userspace mapping can outlive its GEM handle; reclaiming the span at handle
close could alias a later allocation into that mapping. The 32 rounds therefore
consume exactly 512 KiB even though all live objects are released.

This is now visible rather than hidden, but it is not yet solved. Long-running
desktop workloads that repeatedly allocate new DUMB buffers can still exhaust
the pool. Safe reuse requires tying pool spans to VMA lifetime (or replacing
the shared bump pool with individually lifetime-managed backing), and is the
next resource-architecture task.

The result covers QEMU virtio-gpu. It does not claim native EIC7700 display or
GPU support on the Megrez board.
