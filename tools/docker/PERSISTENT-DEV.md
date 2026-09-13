# Persistent development containers

Use the launcher from the repository root:

```bash
tools/docker/run_dev_container.sh -- make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
tools/docker/run_dev_container.sh --status
tools/docker/run_dev_container.sh
```

The last command opens a shell.
Exit the shell when finished so another build can acquire the shared build lock.
Commands return their exit status to the host.
The launcher requires Linux, Python 3.9 or later, and access to Docker.

The launcher creates one named container per canonical worktree path and image ID.
Later calls execute inside that container, starting it again if necessary.
It never pulls, rebuilds, removes, or prunes images automatically.
An image change selects a new container and cache namespace, preserving the old environment.

| State | Storage and reuse |
| --- | --- |
| Rustup toolchains and downloads | Named volume shared by worktrees using the same image |
| Cargo registry and Git downloads | Shared named volumes |
| Cargo binaries and installation metadata | Separate named volume for each worktree |
| Nix store, database, and profiles | One shared `/nix` volume per image |
| Nix fetch cache | Shared named volume |
| Kernel and test build outputs | Existing worktree directories, including `target/` |
| Packages installed manually in a container | Retained in that container until explicitly removed |

Docker initializes empty volumes from the image once.
The Cargo home is initialized per worktree, so its initial image contents may
also include a copy of the image's dependency cache beneath the shared mounts.
This avoids sharing installed OSDK binaries between different branches.
The shared volumes survive container removal.
See [Docker's volume lifecycle](https://docs.docker.com/engine/storage/volumes/).

Commands across these worktrees are serialized with a host lifecycle lock and
a lock inside the Rustup volume.
The latter remains held by a running command if the Docker client disconnects.
This prevents concurrent Rustup installation and Nix cache mutation through the launcher.
Use the launcher for builds; direct `docker exec` bypasses these locks.
Independent QEMU runs do not need to hold the build lock.

## Local configuration

The default image comes from `.devcontainer/devcontainer.json`.
Use `--image IMAGE` for one invocation, or create
`${XDG_CONFIG_HOME:-$HOME/.config}/asterinas/docker.json`:

```json
{
  "image": "asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached",
  "seed_cargo_volume": "asterinas-cargo-home"
}
```

This example uses an existing local derivative of the project image that already
contains `nightly-2026-07-21`.
The optional seed volume copies existing registry and Git downloads once,
without importing its installed OSDK binary.
The source volume is mounted read-only, and the completed seed container is retained.
Omit this option on machines without that volume.
The configuration file is local to the host and is not committed.

For another worktree, use the same launcher with `--workspace`:

```bash
tools/docker/run_dev_container.sh --workspace /absolute/path/to/worktree -- make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
```

The image must exist locally.
When the repository requires a newer toolchain, the first build may install it;
subsequent builds retain it in the Rustup volume.
`make install_osdk` uses `cargo install --locked` so it respects `osdk/Cargo.lock`
instead of resolving newer dependencies on each installation.
See [Cargo's installation options](https://doc.rust-lang.org/cargo/commands/cargo-install.html).

## Offline builds and stopping

After warming dependencies, verify cache reuse with:

```bash
tools/docker/run_dev_container.sh --offline -- make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
tools/docker/run_dev_container.sh --stop
tools/docker/run_dev_container.sh --offline -- make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
```

`--offline` runs the command in a fresh network namespace without external
interfaces, sets Cargo offline mode, and disables Nix substitutes and fetch retries.
Rustup and build scripts cannot access the host network either.
Use this mode for builds after warming dependencies; it is not intended for
tests that require host networking or network fixtures.
Stopping an idle container preserves its writable layer and all volumes.
The stop operation waits for launcher-managed commands to finish and refuses
to stop a container that still has untracked commands running.
It has no automatic cleanup operation.

For VS Code, use **Dev Containers: Attach to Running Container** with the
container name printed by `--status` to use the same environment.
The existing **Reopen in Container** configuration creates a separate environment.

## Validation

```bash
python3 -m unittest discover -s tools/docker -p 'test_*.py'
bash -n tools/docker/run_dev_container.sh
```

For a real repeat-build check, record the container ID, toolchain version,
`cargo-osdk` modification time, and kernel SHA-256 before and after the second build.
The ID and OSDK timestamp should stay unchanged; unchanged source and inputs
should produce the same kernel hash.
