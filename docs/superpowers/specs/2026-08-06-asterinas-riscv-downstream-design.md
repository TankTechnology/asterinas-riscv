# Asterinas RISC-V Independent Downstream Design

## Objective

Establish `TankTechnology/asterinas-riscv` as a public, independently maintained
RISC-V downstream of Asterinas. The repository will provide a clean, tested
integration branch for general RISC-V support, QEMU firmware boot validation,
and development-board enablement.

The downstream will continue to consume changes from `asterinas/asterinas`, but
will not proactively submit pull requests upstream. If upstream maintainers
choose to reuse downstream changes or request help with a specific change, the
downstream maintainer remains willing to provide technical context, validation
evidence, or implementation assistance under an agreed scope.

## Repository Identities

Two existing repositories will be retained:

- Rename the existing private, non-fork repository from
  `TankTechnology/asterinas-riscv` to
  `TankTechnology/asterinas-riscv-private`. It preserves early experiments,
  diagnostic history, and private development branches.
- Rename the public fork from `TankTechnology/asterinas` to
  `TankTechnology/asterinas-riscv`. It becomes the official public downstream.

No repository, branch, or commit will be deleted during the migration.

The local remotes will use these roles:

```text
origin    TankTechnology/asterinas-riscv
private   TankTechnology/asterinas-riscv-private
upstream  asterinas/asterinas (fetch only)
```

## Branch Model

The public repository's default `main` branch is the tested downstream
integration line. It starts from the latest `upstream/main` and contains only
curated RISC-V changes that pass their required validation.

New work is developed and tested on focused topic branches. A topic branch may
be merged into `main` only after its intended behavior and relevant regression
matrix pass. Once the new public `main` is published, its history is not
rewritten and it is never force-pushed.

Upstream changes flow in one direction:

```text
asterinas/asterinas:main -> upstream/main -> asterinas-riscv:main
```

Periodic synchronization uses merge commits rather than rebasing the published
integration history. The downstream does not proactively open pull requests in
`asterinas/asterinas`.

## Clean Reconstruction

The current local `main` contains valuable bring-up history, but also includes
superseded diagnostics, temporary workarounds, and experimental commits. It
must not be published directly as the new default branch.

The new integration line will be reconstructed from the latest upstream
baseline. Validated changes will be replayed in dependency order, with conflicts
resolved against the current architecture. Old debugging commits remain
available in the private repository and local backup refs.

The merged upstream version of PR #3673 is taken from `upstream/main`. The local
review-fix commit that failed to follow the maintainer's proposed patch is not
reused.

## Initial Integration Stages

The downstream features are integrated in the following order:

1. OSDK generated-base-crate cache correctness and standard RISC-V Linux Image
   generation.
2. Svade support for kernel mappings, user mappings, and persistent CI
   validation.
3. Generic RISC-V firmware/QEMU boot validation and SiFive platform support.
4. Milk-V Megrez support, including DesignWare APB UART, firmware framebuffer,
   and board boot integration.
5. Maintainer-facing documentation: quick start, support matrix, validation
   evidence, upstream relationship, and synchronization policy.

Each stage is independently reviewable in the downstream repository. A failed
or incomplete stage remains on its topic branch and does not block publication
of earlier validated stages.

## Validation Policy

Validation is proportional to the affected layer:

- Rust formatting, compilation, and lint checks for every integrated stage.
- RISC-V OSTD and kernel tests for memory-management or architecture changes.
- Sv39 and Sv48 boot coverage where page-table behavior is affected.
- Svadu and forced-Svade coverage for A/D-bit handling.
- QEMU firmware boot and userspace smoke validation for supported virtual
  machines.
- Generated-code inspection when correctness depends on exact RISC-V
  instruction operands.

Real-board results are reported with explicit provenance. Results reproduced
during the current integration are marked as currently verified. Historical
Megrez results that cannot be rerun without board access are marked as
previously verified and linked to their evidence; they are not presented as a
current test result.

If a replayed change conflicts with current upstream code or fails validation,
the change stays outside `main`. The failure and its remaining dependency are
documented on the topic branch instead of being hidden by a partial workaround.

## Public Repository Documentation

The public landing page will state:

> `asterinas-riscv` is an independently maintained RISC-V downstream of
> Asterinas.
>
> We do not plan to proactively submit pull requests to the upstream Asterinas
> repository. Upstream maintainers are welcome to reuse changes from this
> repository under the project's license. If they request assistance with a
> specific change, we remain willing to provide technical context, validation
> evidence, or implementation support under an agreed scope.

The README will lead with current capabilities and reproducible commands rather
than the history of the upstream disagreement. It will distinguish supported,
previously verified, experimental, and planned configurations.

## Non-goals

- Rewriting or deleting the historical private repository.
- Publishing every experimental branch in the public default history.
- Maintaining a force-pushed patch stack on top of upstream.
- Claiming current hardware validation when only historical evidence exists.
- Proactively submitting downstream work to the upstream Asterinas repository.
