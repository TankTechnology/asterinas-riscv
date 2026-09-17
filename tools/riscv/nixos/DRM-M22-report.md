# DRM M22 resource-lifetime accounting and stress report

Date: 2026-08-28

## Result

M22 passes on the RISC-V Sv39, four-hart Asterinas guest with QEMU's
`virtio-gpu-gl-device` and virgl enabled. The gate completed 32 repeated
GEM/PRIME/context/fence lifetimes, and every reclaimable device-wide counter
returned exactly to its quiescent baseline after each round.
The 2026-08-28 resource-validation run reported 50 passes and zero failures.

Before those rounds, M22 also verifies that both card/render-node mappings and
PRIME dma-buf mappings retain their GEM span after the corresponding handle or
fd closes. A live mapping prevents reuse and preserves its contents; after
`munmap`, first-fit allocation reuses the original offset. Finally, 4,200
create/map/close cycles allocate more than 64 MiB cumulatively without
exhausting the 64 MiB pool.

The final acceptance set also passes:

- RISC-V Sv39 `cargo osdk build` with only seven pre-existing unrelated warnings;
- focused RISC-V ktests for virgl resource geometry, texture and buffer
  transfer bounds, and plane-only atomic flip events;
- M17 atomic KMS, 55 passes and no failures; and
- the complete NixOS Xorg/Xfce gate with DRI3 direct rendering, the virgl
  renderer, pixel read-back, a clean command stream, and `XFCE_DRM_PASS`.

The post-change Xfce run passed every functional gate.
Its shader sample was 0.824 FPS while an unrelated RISC-V QEMU consumed about
333% host CPU, so this number is intentionally not used as a performance
comparison.
The earlier quiescent M22 acceptance run measured 17.765 FPS.

## What changed

DRM files now append device diagnostics to `/proc/<pid>/fdinfo/<fd>`. The
`drm-device-` prefix makes clear that the values are totals for the shared GPU,
not per-client accounting. The snapshot covers:

- the DUMB-pool live-used, high-water, and capacity byte gauges;
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

The DUMB pool is now a page-granular, coalescing first-fit allocator. Each span
is reference-counted by its GEM object, every surviving VMA, and every
virtio-gpu host backing owner. Pool pages are returned only after all three
lifetime domains release them and are cleared before reuse. An ambiguous
`ATTACH_BACKING` transport result retains its owner until a confirmed
`RESOURCE_UNREF`, preferring bounded quarantine over aliasing host-visible
memory.

Virgl resources now retain their complete validated Gallium creation metadata
alongside the GEM backing size. `RESOURCE_CREATE` rejects unknown targets,
zero or incoherent dimensions, unsupported flags, invalid sample/mip counts,
and arithmetic overflow before reaching the host. Transfer ioctls validate the
requested mip level, target-specific box geometry, backing offset, and byte
extent when the layout is known. Linear 32-bit formats receive exact last-byte
checks at every mip level instead of only a coarse aggregate-size check; other
format layouts remain bounded by virglrenderer's format-aware IOV validation.

The atomic KMS event path now treats an updated primary plane as affecting the
CRTC to which its committed state is routed. This permits the standard first
full modeset followed by `FB_ID`-only page flips. A real Mesa/GBM test completed
four frames with distinct read-back checksums and flip sequences 0 through 3.

The generic VMA layer carries an optional lifetime token through fork, split,
and remap. Adjacent VMO mappings merge only when those tokens are identical,
so mappings of separate buffers in the shared VMO cannot accidentally collapse
their lifetimes. Boot-pattern scanout remains tracked alongside later KMS
presentation, and fdinfo does not linearly sum GEM references or fence
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
         backing=1 pending=0/0/0 pool=0/67108864 high-water=0
final    gem=0 refs=0 host=0 ctx=0 attach=0 fences=0 assoc=0
         backing=1 pending=0/0/0 pool=0/67108864 high-water=32768
```

The lifetime and capacity phases printed:

```text
M22_MAPPING_LIFETIME DRM protected-and-reused offset=0
M22_MAPPING_LIFETIME PRIME protected-and-reused offset=0
M22_POOL_REUSE cycles=4200 offset=0
```

The resource-contract phase printed successful full level-zero transfers and
rejected every malformed request without changing the baseline counters:

```text
M22_PASS RESOURCE_CREATE rejects an unknown Gallium target
M22_PASS RESOURCE_CREATE rejects zero resource dimensions
M22_PASS TRANSFER_TO_HOST accepts the full level-zero texture
M22_PASS TRANSFER_FROM_HOST accepts the full level-zero texture
M22_PASS TRANSFER_FROM_HOST rejects a box outside resource geometry
M22_PASS TRANSFER_FROM_HOST rejects a write beyond GEM backing
M22_PASS TRANSFER_TO_HOST rejects a missing mip level
M22_PASS rejected resource requests leave all counters at baseline
```

All 32 rounds then printed `baseline-restored` and reused offset zero, followed
by:

```text
M22_PASS all reclaimable resource counters returned to baseline after 32 rounds
M22_PASS dumb pool live usage returned to baseline
M22_PASS dumb pool high-water mark remains within capacity
M22_RESOURCE_STRESS_PASS
M22_RESOURCE_STRESS_GATE_PASS
```

The retained serial evidence is generated at
`target/drm-m22/evidence/serial.log`. The target directory is intentionally
untracked because it contains generated kernels, disks, and logs.

Run the gate from the repository root after building the RISC-V Sv39 kernel.
OSDK ktests overwrite the generic kernel artifact, so rebuild the normal kernel
before packaging the gate:

```bash
make TARGET_ARCH=riscv64 FEATURES=riscv_sv39_mode kernel
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

The virgl-resource and partial-atomic tranche received a further combined
maintainability, correctness, security, and documentation review. Verification
confirmed all 11 findings (six major and five minor). The fixes made cleanup
idempotent after a lost response, constrained virgl wire values, extended
known-layout mip bounds, and prevented false-positive M19/M22 gates. The final
review is retained under `target/drm-resource-review/` and is also untracked.

## Remaining limitation

The monotonic-exhaustion limitation is resolved. The pool is still a fixed
64 MiB contiguous allocation, so a workload whose *simultaneously live* DUMB
buffers exceed that capacity receives `ENOMEM`. This is an explicit capacity
bound rather than cumulative leakage. The current allocator also uses
page-granular first-fit rather than relocation or compaction; severe live-range
fragmentation can therefore reject a large contiguous request even if total
free bytes would be sufficient.

The result covers QEMU virtio-gpu. It does not claim native EIC7700 display or
GPU support on the Megrez board.
