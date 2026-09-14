# Stage1-Carried Physical Firefox Helper Design

## Goal

Allow the physical Firefox interaction gate to run against the already
installed Debian root filesystem without rewriting MMC partition 2 whenever
the small orchestration helper changes.

The accepted physical experiment must execute the helper bytes that are bound
to the same Stage1 artifact as the boot. A missing or mismatched helper must
fail before Xorg or Firefox is accepted as ready.

## Context

The current gate invokes
`/usr/lib/asterinas/physical-external-services-quiesce`. QEMU uses a newly
derived root image containing that file, but the Megrez board deliberately
reuses an older proven partition-2 image. The first physical replay therefore
failed with `No such file or directory`; the gate retained diagnostics and
recovered normally.

Stage1 already carries two experiment helpers under `/usr/lib/asterinas` and
bind-mounts that directory into the Debian guest at
`/run/asterinas-tools`. Extending this existing boundary is sufficient and
does not require a new deployment mechanism.

## Design

`build_stage1.sh` will install
`physical_external_services_quiesce.sh` and the experiment's
`physical_graphics_gate.py` as executable files under `usr/lib/asterinas`.
Both files will be part of the deterministic Stage1 entry list, timestamp
normalization, and archive validation. Carrying the guest gate is necessary
because the one-cycle host contract cannot safely execute the older
three-cycle-only copy in the reused partition-2 image.

The physical orchestration command will invoke
`/run/asterinas-tools/physical-external-services-quiesce`. This path is
provided only after Stage1 mounts its private tool directory into the mounted
Debian root. The full browser root image will continue to install the same
helper under `/usr/lib/asterinas` for QEMU and non-Stage1 environments, but the
physical gate will not depend on that mutable root-image copy.

The cycle and final-state commands will likewise invoke
`/run/asterinas-tools/physical-graphics-gate`. The Stage1-carried guest gate
accepts the host's bounded one- or three-cycle final-state contracts, so the
host and guest halves of the experiment always come from the same source
generation.

No fallback to `/usr/lib/asterinas` is allowed in the physical gate. A stale
Stage1 must fail closed rather than silently executing helper code from an
unrelated partition-2 generation.

The helper continues to:

- create the volatile browser home;
- mask competing evidence, network, and getty units;
- stop and verify inactive competing workloads;
- start Xorg/desktop prerequisites without Firefox;
- emit its bounded terminal status marker.

The host will still run low-load kernel and root probes before explicitly
starting Firefox. The display-provider contract remains unchanged, and this
work adds no DRM implementation.

## Deployment and Data Flow

1. Build a deterministic Stage1 containing `init` and the four helper files.
2. Transfer or select the versioned kernel, Stage1, and DTB only.
3. Stage1 mounts the existing ext2 root and bind-mounts its own helper directory
   at `/run/asterinas-tools`.
4. The isolated root console invokes the Stage1-bound quiesce and interaction
   helpers.
5. The host runs system probes, starts Firefox, and evaluates graphical and
   interaction evidence.
6. The fixed recovery timer returns the board to a fresh U-Boot epoch.

Partition 2 is neither reimaged nor used as the helper source in this flow.

## Error Handling

- The Stage1 build rejects an archive with a missing, extra, or misordered
  helper entry.
- The physical gate uses one bounded command and accepts only the complete
  terminal marker with inactive competing services.
- Missing executable, mount failure, nonzero setup status, incomplete systemd
  state, or missing marker fails the gate and triggers bounded diagnostics and
  recovery.
- The board experiment retains exact kernel, Stage1, DTB, serial, and result
  identities. QEMU evidence remains explicitly non-physical.

## Verification

1. Unit-test the exact Stage1 entry list and installed executable path.
2. Unit-test that the physical command uses only the `/run/asterinas-tools`
   path and has no root-image fallback.
3. Build Stage1 twice and require byte-identical output.
4. Inspect the archive and require executable helper bytes matching the source
   SHA-256.
5. Run the existing 115 physical-graphics tests and Firefox fast check.
6. Run the three-cycle QEMU interaction gate with the new Stage1.
7. Run one physical graphical-readiness and interaction experiment using the
   existing partition-2 root, then require automatic recovery to U-Boot.

## Non-Goals

- Rewriting or repairing the Debian root image.
- Changing the production browser service dependency graph.
- Implementing native EIC7700 DRM or changing the display-provider interface.
- Changing network-stack behavior.
- Making Stage1 a general-purpose package overlay.
