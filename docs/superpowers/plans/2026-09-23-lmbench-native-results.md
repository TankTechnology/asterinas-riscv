# Native LMBench results implementation plan

**Goal:** Run the pinned asterinas/lmbench suite through `cd lmbench/src && make results` on
Asterinas, with container-built binaries and explicit incomplete-result reporting.

**Architecture:** Keep the native configuration, results writer, benchmark
selection, loops and executables. Package the runtime closure and source tree;
add a GNUmakefile that skips guest compilation and invokes a bounded supervisor.
Record small compatibility patches separately. QEMU uses a disposable Debian disk.

**Tech stack:** Nix cross compilation, GNU make, POSIX shell, Python standard library.

- [x] Read the native scripts and reproduce the unmodified ALL workflow in QEMU.
- [x] Isolate network command-line drift, missing rpc user and PATH script execution.
- [x] Add tests for skipping compilation, rejecting missing/error result sections,
  and retaining native raw output on failure.
- [x] Package the upstream source, executables, dependencies and declared patches.
- [x] Add a bounded guest entry that runs native `config-run` and `results`,
  disables mailing and preserves raw results/configuration/stdout/stderr.
- [x] Exercise the packaged entry in QEMU; record remaining unsupported cases
  without converting omissions or a zero native exit status into success.
- [x] Document reproducible commands and evidence; review and test.
- [x] Prepare the verified change for commit and push.

Daily smoke remains the separate 18-case check. A native ALL run is an on-demand
compatibility qualification, not a sustained-load stability test or a TCG score.
