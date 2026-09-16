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
`physical_external_services_quiesce.sh`, the fixed-action
`physical_graphics_control.sh`, and the experiment's
`physical_graphics_gate.py` as executable files under `usr/lib/asterinas`.
The interaction gate's direct project dependencies,
`browser_interaction_perf.py` and `browser_m5_marionette_gate.py`, are carried
beside it so the payload is import-complete. All of these files are part
of the deterministic Stage1 entry list, timestamp normalization, and archive
validation. Carrying the guest gate is necessary because the one-cycle host
contract cannot safely execute the older three-cycle-only copy in the reused
partition-2 image.

The interaction HTML is also carried by Stage1 and served from
`/run/asterinas-tools`. This binds the browser-visible fields to the guest gate
that validates them; an older page from partition 2 cannot silently produce a
different evidence schema.

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

The five system-readiness probes are packaged as
`physical-system-probe`. The preflight, browser-start, interaction-cycle, and
final-state operations are likewise packaged behind the fixed-action
`physical-graphics-control` helper. Every host-to-guest request is therefore
below 128 bytes while retaining the existing output markers, nonce binding,
Firefox PID/restart checks, and timeout bounds. The helper validates every
argument before interpolation and exposes no general shell-evaluation action.
Python bytecode caches are redirected to an ephemeral `/run` prefix so the
first cycle compiles from immutable source and later cycles reuse the
memory-backed cache without reading or writing persistent ext2 `.pyc` files.
This avoids depending on reliable input of several long shell programs over
the physical serial console.

No fallback to `/usr/lib/asterinas` is allowed in the physical gate. A stale
Stage1 must fail closed rather than silently executing helper code from an
unrelated partition-2 generation.

The fixed boot and helper contract continues to:

- create the volatile browser home;
- mask competing evidence, network, and getty units;
- stop and verify inactive competing workloads;
- start Xorg/desktop prerequisites without Firefox;
- emit its bounded terminal status marker.

The desktop device-access helper resolves the keyboard and pointer by exact
Linux `EVIOCGID`, `EVIOCGNAME`, and `EVIOCGPHYS` input identity, then publishes
stable `/run/asterinas-input/{keyboard,pointer}` symlinks before Xorg starts.
It never assigns roles from `eventN`: the two physical xHCI controllers are
initialized concurrently, so the same board has produced both
keyboard=`event1`/pointer=`event0` and the reverse ordering. Missing or
ambiguous identities fail closed. The Stage1 control helper applies the same
identity rule to the reused partition-2 Xorg configuration for experiments
that precede the next root-image rebuild.
The kernel does not currently expose a Linux `/sys/class/input` identity
contract, so the future rootfs resolver must use evdev ioctls.

The host will still run low-load kernel and root probes before explicitly
starting Firefox. The display-provider contract remains unchanged, and this
work adds no DRM implementation.

## Deployment and Data Flow

1. Build a deterministic Stage1 containing `init` and the nine helper files.
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
- The control helper rejects unknown actions, malformed nonces, and malformed
  numeric arguments without producing a success marker.
- The physical gate uses one bounded command and accepts only the complete
  terminal marker with inactive competing services.
- Missing executable, mount failure, nonzero setup status, incomplete systemd
  state, or missing marker fails the gate and triggers bounded diagnostics and
  recovery.
- The board experiment retains exact kernel, Stage1, DTB, serial, and result
  identities. QEMU evidence remains explicitly non-physical.

## Verification

1. Unit-test the exact Stage1 entry list and installed executable path.
2. Unit-test that every physical command uses only the
   `/run/asterinas-tools` path, remains below 128 bytes, and has no root-image
   fallback.
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
