---
date: 2026-08-20
mode: diff
base: ecdea5a39
head: db53406fd
branch: codex/riscv-ltp-integration
title: "RISC-V LTP gate and BASELINE-M1"
---

# Summary

The series establishes a substantial, reproducible RISC-V LTP gate: it
validates the reviewed test selection, isolates QEMU artifacts, publishes
normalized evidence, exposes live progress, and records real SMP=1/SMP=4
baselines. The strongest parts are the explicit 767/12 selection accounting,
the mutually exclusive result model, and the real full-run evidence.

The top correctness issues are that the normalizer does not yet bind verdicts
to the selected manifest, full runs still depend on mutable shared build
outputs, and the guest runner can misclassify mixed LTP results or leave
descendants outside its process group. The top infrastructure issues are PID
1's incomplete reaping and inconsistent BROK markers. Security review also
found that several path and provenance checks are pathname- or
caller-assertion-based rather than bound to retained handles or tree digests.
The baseline report needs complete per-run artifact provenance and two root
cause corrections (`setfsgid03` account-file permissions and the five
`INADDR_ANY` failures). Prioritize those evidence and lifecycle issues before
using the gate as a merge-blocking conformance result; the larger module split,
manifest-contract deduplication, commit-history cleanup, and semantic line
breaks can follow as maintainability work.

## Maintainability

### `commit 0b6707ea2 message`

> ```diff
> test(riscv): add isolated LTP gate orchestration
> ```

`imperative-subject` (minor): `test(riscv): add isolated LTP gate orchestration` starts with a scope tag rather than the imperative verb.

**Fix.** Rewrite the subject as `Add isolated RISC-V LTP gate orchestration`.

### `commit 0d0ebce56 message`

> ```diff
> test(riscv): validate LTP manifest packaging
> ```

`imperative-subject` (minor): `test(riscv): validate LTP manifest packaging` does not begin with an imperative verb.

**Fix.** Rewrite the subject as `Validate RISC-V LTP manifest packaging`.

### `commit 0fe475750 message`

> ```diff
> docs(riscv): use container vDSO directory
> ```

`imperative-subject` (minor): `docs(riscv): use container vDSO directory` does not begin with an imperative verb.

**Fix.** Rewrite the subject as `Use the container vDSO directory for RISC-V builds`.

### `commit 34bfed31e message`

> ```diff
> test(riscv): publish readable LTP evidence
> ```

`imperative-subject` (minor): `test(riscv): publish readable LTP evidence` starts with the Conventional Commit tag instead of the imperative verb.

**Fix.** Rewrite the subject as `Publish readable RISC-V LTP evidence`.

### `commit 37c5661ce message`

> ```diff
> test(riscv): isolate LTP test processes
> ```

`imperative-subject` (minor): `test(riscv): isolate LTP test processes` is not verb-first.

**Fix.** Rewrite the subject as `Isolate RISC-V LTP test processes`.

### `commit 4808b5aab message`

> ```diff
> test(riscv): expose live LTP progress
> ```

`imperative-subject` (minor): `test(riscv): expose live LTP progress` does not begin with an imperative verb.

**Fix.** Rewrite the subject as `Expose live RISC-V LTP progress`.

### `commit 67f81bd78 message`

> ```diff
> docs(riscv): pin the LTP musl toolchain
> ```

`imperative-subject` (minor): `docs(riscv): pin the LTP musl toolchain` starts with a Conventional Commit tag instead of the imperative action.

**Fix.** Rewrite the subject as `Pin the RISC-V LTP musl toolchain`.

### `commit 6b06ecb4f message`

> ```diff
> test(riscv): accept containerized worktree provenance
> ```

`imperative-subject` (minor): `test(riscv): accept containerized worktree provenance` is not verb-first.

**Fix.** Rewrite the subject as `Accept containerized RISC-V worktree provenance`.

### `commit 78b61449d message`

> ```diff
> test(riscv): register LTP QEMU profiles
> ```

`imperative-subject` (minor): `test(riscv): register LTP QEMU profiles` starts with `test(riscv):` instead of an imperative verb.

**Fix.** Rewrite the subject as `Register RISC-V LTP QEMU profiles`.

### `commit 89bb85c4e message`

> ```diff
> tools/riscv/ltp_gate.py                    |  8 +++++-
> tools/riscv/nixos/ltp/init_ltp.c           | 27 +++++++++++++++----
> tools/riscv/tests/test_ltp_gate.py         | 20 +++++++++++++-
> tools/riscv/tests/test_ltp_guest_runner.py | 43 ++++++++++++++++++++++++++++++
> ```

`atomic-commits` (minor): The PID-1 lifetime change also modifies `_git_commit()` to inject `safe.directory`, which is unrelated worktree-provenance handling and is not described by the commit subject.

**Fix.** Move the `_git_commit()` and its test into the following containerized-worktree provenance commit `6b06ecb4f`; keep this commit limited to `init_ltp.c` lifetime behavior and its tests.

### `commit 89bb85c4e message`

> ```diff
> test(riscv): keep LTP init alive after completion
> ```

`imperative-subject` (minor): `test(riscv): keep LTP init alive after completion` starts with a scope tag rather than an imperative verb.

**Fix.** Rewrite the subject as `Keep RISC-V LTP init alive after completion`.

### `commit 90cba6473 message`

> ```diff
> Makefile                  |  15 +++++
> tools/riscv/README.md     |  27 ++++++++
> tools/riscv/ltp/README.md | 162 ++++++++++++++++++++++++++++++++++++++++++++++
> ```

`atomic-commits` (minor): The commit is presented as documentation but also adds the executable `test_riscv_ltp_unit` and `test_riscv_ltp` interfaces to `Makefile`; the operational build change and the guide are independently reviewable logical changes.

**Fix.** Move the `Makefile` targets into a preceding commit such as `Add RISC-V LTP Make targets`, and keep this commit limited to the documentation that explains those targets.

### `commit 90cba6473 message`

> ```diff
> docs(riscv): document the LTP gate
> ```

`imperative-subject` (minor): `docs(riscv): document the LTP gate` starts with `docs(riscv):` rather than an imperative verb.

**Fix.** Rewrite the subject as `Document the RISC-V LTP gate`.

### `commit ad092e410 message`

> ```diff
> test(ltp): enable reviewed RISC-V syscall set
> ```

`imperative-subject` (minor): `test(ltp): enable reviewed RISC-V syscall set` is not verb-first.

**Fix.** Rewrite the subject as `Enable reviewed RISC-V LTP syscall set`.

### `commit bd2e4e7a3 message`

> ```diff
> test(riscv): normalize LTP gate results
> ```

`imperative-subject` (minor): `test(riscv): normalize LTP gate results` starts with the Conventional Commit tag `test(riscv):` instead of an imperative verb, so the subject is not verb-first.

**Fix.** Rewrite the subject as `Normalize RISC-V LTP gate results`.

### `commit c5bab60b6 message`

> ```diff
> 67f81bd78 docs(riscv): pin the LTP musl toolchain
> c5bab60b6 build(riscv): require LTP autotools
> e2e371c3c build(riscv): require LTP UAPI headers
> ```

`atomic-commits` (minor): `c5bab60b6` and the immediately following `e2e371c3c` repair missing prerequisites in the cross-image recipe introduced by `67f81bd78`; leaving these as appended fixups means the earlier commit contains instructions that cannot perform the build by themselves.

**Fix.** Fold the Autotools and `linux-libc-dev-riscv64-cross` requirements into `67f81bd78`, so the commit that switches build images also introduces a complete working recipe.

### `commit c5bab60b6 message`

> ```diff
> build(riscv): require LTP autotools
> ```

`imperative-subject` (minor): `build(riscv): require LTP autotools` is not verb-first.

**Fix.** Rewrite the subject as `Require Autotools for RISC-V LTP builds`.

### `commit db53406fd message`

> ```diff
> docs(riscv): record LTP baseline M1
> ```

`imperative-subject` (minor): `docs(riscv): record LTP baseline M1` does not begin with an imperative verb.

**Fix.** Rewrite the subject as `Record RISC-V LTP baseline M1`.

### `commit e2e371c3c message`

> ```diff
> build(riscv): require LTP UAPI headers
> ```

`imperative-subject` (minor): `build(riscv): require LTP UAPI headers` does not begin with an imperative verb.

**Fix.** Rewrite the subject as `Require UAPI headers for RISC-V LTP builds`.

### `commit e520dbda2 message`

> ```diff
> test(riscv): add LTP guest runner and builder
> ```

`imperative-subject` (minor): `test(riscv): add LTP guest runner and builder` does not follow the required verb-first subject form.

**Fix.** Rewrite the subject as `Add RISC-V LTP guest runner and builder`.

### `commit eed8bd94f message`

> ```diff
> test(riscv): isolate LTP boot evidence by run
> ```

`imperative-subject` (minor): `test(riscv): isolate LTP boot evidence by run` starts with a scope tag rather than the imperative action.

**Fix.** Rewrite the subject as `Isolate RISC-V LTP boot evidence by run`.

### `commit f0c2a1153 message`

> ```diff
> test(riscv): isolate LTP QEMU artifacts
> ```

`imperative-subject` (minor): `test(riscv): isolate LTP QEMU artifacts` is not verb-first because its Conventional Commit tag precedes the imperative action.

**Fix.** Rewrite the subject as `Isolate RISC-V LTP QEMU artifacts`.

### `tools/riscv/ltp_gate.py` line 261

> ```diff
> def tree_sha256(root: Path) -> str:
>     ...
> def _pack_initramfs(rootfs: Path, output: Path) -> None:
>     ...
> def _show_status(repo: Path, run_id: str) -> int:
>     ...
> def _run_gate(repo: Path, args: argparse.Namespace) -> int:
> ```

`single-responsibility` (major): `tools/riscv/ltp_gate.py` now owns tree hashing and archive construction through `tree_sha256()` and `_pack_initramfs()`, status parsing and rendering through `_show_status()`, and the complete QEMU workflow through `_run_gate()`. These are separate concepts with separate reasons to change, leaving the command module large and forcing unrelated packaging, presentation, and execution changes into one file.

**Fix.** Move subset/rootfs packaging into a dedicated module such as `ltp_package.py`, move progress parsing and rendering into `ltp_status.py`, and leave `ltp_gate.py` as a thin CLI and workflow coordinator.

### `tools/riscv/nixos/ltp/build_ltp.sh` line 126

> ```diff
> python3 "${REPO_ROOT}/tools/riscv/ltp_manifest.py" select \
>     --unavailable-output "${UNAVAILABLE}" \
>     --expected-count 767
> ```

`dry` (minor): The reviewed runtime-manifest contract is embedded here as the unexplained literal `767`, while the same count is repeated in `tools/riscv/tests/test_ltp_guest_runner.py`, `tools/riscv/ltp/README.md`, and `tools/riscv/ltp/BASELINE-M1-report.md`. A manifest update therefore requires synchronized edits in several representations.

**Fix.** Define the expected counts once in a named, machine-readable contract, for example `tools/riscv/ltp/manifest_contract.json` with `enabled_test_count` and `runtime_test_count`, and make the builder and tests read it; have prose reference that contract rather than acting as another authoritative copy.

### `tools/riscv/nixos/ltp/build_ltp.sh` line 151

> ```diff
> for a in sh cat true echo test; do
>     ln -sf busybox "${ROOTFS}/bin/${a}"
> done
> ```

`descriptive-names` (nit): The loop variable `a` gives no indication that each value is a BusyBox applet name, so the body must be read to recover its meaning.

**Fix.** Rename `a` to `applet` and use `${applet}` in the destination path.

### `tools/riscv/nixos/ltp/init_ltp.c` line 4

> ```diff
> // /init for the LTP syscall gate. Runs as pid 1: attaches the serial console,
> // best-effort mounts the proc/sys/tmp pseudo-filesystems, then execs the static
> // /ltp_runner.
> ```

Stale comment (minor): The file overview still says PID `1` directly `exec`s `/ltp_runner`, but the implementation now forks the runner, waits for it, emits `__LTP_GATE_TERMINAL__`, and deliberately remains alive. The first explanation a maintainer sees describes the superseded lifecycle.

**Fix.** Update the overview to state that PID `1` forks and waits for `/ltp_runner`, reports abnormal completion, emits the terminal marker, and then remains alive.

### `tools/riscv/nixos/ltp/ltp_runner.c` line 10

> ```diff
> // lighter to pack into the initramfs. Results stream to the serial console and
> // a final __LTP_GATE_DONE__ marker followed by __LTP_GATE_PASS__ or
> // __LTP_GATE_FAIL__ lets the QEMU driver decide pass/fail.
> ```

Stale comment (minor): The overview says `__LTP_GATE_DONE__` followed by the PASS/FAIL marker lets the QEMU driver decide the result. The registered QEMU profile now terminates on `__LTP_GATE_TERMINAL__`; `ltp_result.py` subsequently interprets the DONE and PASS/FAIL evidence. The comment describes the old protocol division.

**Fix.** Describe the two-stage protocol accurately: the runner emits DONE and its normalized outcome, PID `1` emits `__LTP_GATE_TERMINAL__`, QEMU completes on the terminal marker, and `ltp_result.py` validates the runner markers.

### `tools/riscv/nixos/ltp/ltp_runner.c` line 99

> ```diff
> int main(int argc, char **argv) {
>     ...
>     while (fgets(line, sizeof(line), f)) {
>         ...
>         pid_t pid = fork();
>         ...
>         while (!done) {
>             ...
>         }
>         ...
>     }
>     ...
> }
> ```

`single-responsibility` (major): `main()` combines environment parsing, manifest parsing, argument construction, log setup, supervisor and test creation, watchdog operation, verdict accounting, failure-log rendering, and final summary publication in one long function. Each concern can change independently, and the process-isolation additions make the central flow especially difficult to review.

**Fix.** Keep `main()` as the top-down manifest loop and extract focused helpers such as `parse_manifest_entry()`, `run_test_case()`, `wait_for_supervisor()`, `update_counts()`, and `print_failure_log()`.

### `tools/riscv/nixos/ltp/ltp_runner.c` line 103

> ```diff
> const char *t = getenv("LTP_PER_TEST_TIMEOUT");
> ...
> const char *mul = getenv("LTP_TIMEOUT_MUL");
> ...
> FILE *f = fopen(manifest, "r");
> ```

`descriptive-names` (minor): The runner repeatedly uses context-dependent abbreviations and single-letter names such as `t`, `mul`, `f`, `w`, `r`, `lf`, and `b`. In this large `main()` these names force readers to trace declarations to distinguish timeout text, files, wait results, verdicts, and buffers.

**Fix.** Use names such as `timeout_text`, `timeout_multiplier_text`, `manifest_file`, `wait_result`, `verdict`, `log_file`, and `log_buffer`.

### `tools/riscv/nixos/ltp/ltp_runner.c` line 178

> ```diff
> dprintf(2, "TCONF: cannot exec %s\n", binpath);
> _exit(32);
> ```

`no-magic-number` (minor): `_exit(32)` embeds LTP's externally defined `TCONF` exit status as an unexplained numeric literal, so its meaning is available only to readers who already know the LTP result ABI.

**Fix.** Define a named constant such as `LTP_TCONF_EXIT_STATUS` and cite LTP's `include/tst_res_flags.h` next to the definition.

### `tools/riscv/nixos/ltp/ltp_runner.c` line 181

> ```diff
> if (test < 0)
>     _exit(125);
> ...
> _exit(125);
> ...
> int status = 125 << 8;
> ```

`no-magic-number` (minor): The internal supervisor-failure status `125` is repeated in three `_exit()` paths and again in the synthesized wait status. Nothing names the invariant that these sites must remain identical.

**Fix.** Introduce a constant such as `RUNNER_INTERNAL_ERROR_STATUS` and derive both `_exit(RUNNER_INTERNAL_ERROR_STATUS)` and the initial wait status from it.

### `tools/riscv/nixos/ltp/ltp_runner.c` line 203

> ```diff
> // Watchdog poll.
> long long start = now_ms();
> ```

`explain-why` (nit): The comment `Watchdog poll.` merely restates the loop immediately below it and provides no rationale for polling or for the chosen interval.

**Fix.** Remove the comment, or replace it with the reason polling is required and why the `10000`-microsecond cadence is appropriate.

## Correctness

### `tools/riscv/ltp_gate.py` line 367

> ```diff
> @@
> +def _normalizer_command(repo: Path, paths: LtpRunPaths, commit: str, smp: int):
> +    return [
> +        ...
> +        "--serial",
> +        str(paths.result_dir / "serial.log"),
> +        "--boot-result",
> +        str(paths.result_dir / "boot-result.json"),
> ```

Dropped test (major): `_normalizer_command()` gives `ltp_result.py` the serial log and boot result but not `selected-syscalls`. Consequently, an internally consistent summary with fewer or different verdict names is accepted. If the guest manifest loses an entry, the runner can report a smaller `total` and the strict gate can still pass without executing every selected test.

**Fix.** Pass the run-owned `selected-syscalls` to `ltp_result.py` and require the verdict names, order, and `total` to match its tags exactly. Add regressions for a missing, extra, and reordered verdict.

### `tools/riscv/ltp_gate.py` line 527

> ```diff
> @@
> +    build_script = repo / "tools/riscv/nixos/ltp/build_ltp.sh"
> +    if not args.skip_build:
> +        subprocess.run([str(build_script)], cwd=repo, check=True)
> +
> +    selected_initramfs = paths.initramfs
> +    selected_manifest = repo / "target/ltp/rootfs/opt/ltp/runtest/syscalls"
> ```

`atomic-critical-sections` (major): Distinct run IDs do not isolate the build: concurrent invocations without `--skip-build` both destructively update the shared `target/ltp/stage`, `target/ltp/rootfs`, and `target/ltp/ltp-initramfs.cpio.gz`. A concurrent `--skip-build` run can also read those paths midway through rebuilding, producing a mixed rootfs or mismatched manifest and initramfs.

**Fix.** Shared with the other run-snapshot comments: make `build_ltp.sh` and every reader use one lock, atomically copy the matched initramfs and manifest into the run directory, and checksum only run-owned prepared artifacts. A `--skip-build` run must hold the same lock while taking its snapshot.

### `tools/riscv/ltp_gate.py` line 531

> ```diff
> @@
> +    selected_initramfs = paths.initramfs
> +    selected_manifest = repo / "target/ltp/rootfs/opt/ltp/runtest/syscalls"
> +    ...
> +    manifest_evidence = paths.result_dir / "selected-syscalls"
> +    _publish_copy(selected_manifest, manifest_evidence)
> ```

Evidence mismatch (major): For a full run, `selected_initramfs` and `selected_manifest` come from independent mutable paths. If a rebuild updates `target/ltp/rootfs` and then fails before publishing the archive, `--skip-build` records the new manifest as `selected-syscalls` while booting the previous initramfs. The result therefore claims a test selection that was not executed.

**Fix.** Shared with the other run-snapshot comments: publish the full initramfs and its manifest as one versioned pair under the common lock, copy both into the run directory, and bind `result.json` to the manifest extracted or hashed from the exact archive being booted.

### `tools/riscv/ltp_gate.py` line 585

> ```diff
> @@
> +        checksum_inputs = (
> +            paths.kernel,
> +            selected_initramfs,
> +            manifest_evidence,
> +            paths.prepared_dir / "boot.ext4",
> +            paths.prepared_dir / "artifacts.json",
> ```

Mutable evidence (major): `SHA256SUMS` hashes the shared source kernel and, for full runs, the shared initramfs rather than the run-owned copies that were placed in the prepared boot disk. Rebuilding either file during a long run can make the checksums describe different bytes from `boot-result.json`; rebuilding later also invalidates an otherwise immutable historical run.

**Fix.** Shared with the other run-snapshot comments: checksum run-specific prepared copies such as `fs-root/asterinas.booti` and `fs-root/initramfs.cpio.gz`, and verify their hashes equal `boot-result.json`. Do not retain checksums of mutable shared build outputs.

### `tools/riscv/nixos/ltp/init_ltp.c` line 55

> ```diff
> @@
> +    if (runner < 0) {
> +        say("init: fork for " RUNNER_PATH " failed\n");
> +    } else {
> +        ...
> +        if (waited < 0) {
> +            dprintf(1, "[BROK] waitpid for LTP runner failed: %d\n", errno);
> ```

Incorrect error classification (major): The QEMU profile rejects only the exact prefix `[BROK] LTP runner`, but a `fork()` failure has no `BROK` marker and a `waitpid()` failure begins with `[BROK] waitpid for LTP runner`. In either case `init_ltp` still emits `__LTP_GATE_TERMINAL__`, so the boot audit can record `passed` as `true` even though the runner never started or was not successfully reaped.

**Fix.** Emit the registered prefix for every runner-infrastructure failure, for example `[BROK] LTP runner fork failed` and `[BROK] LTP runner waitpid failed`. Add profile-level tests proving each path makes `boot-result.json` report infrastructure failure.

### `tools/riscv/nixos/ltp/init_ltp.c` line 60

> ```diff
> @@
> +        do {
> +            waited = waitpid(runner, &status, 0);
> +        } while (waited < 0 && errno == EINTR);
> +        ...
> +    for (;;)
> +        (void)pause();
> ```

`test-cleanup` (major): `PID 1` waits only for the direct runner and then calls `pause()` forever. Any test descendant orphaned by a timeout or faulty cleanup is reparented here, but is never passed to `waitpid()`, so exited descendants accumulate as zombies across the `767`-test run and can exhaust process slots or affect later process tests.

**Fix.** Implement a real `PID 1` reaper: wait for any child with `waitpid(-1, ...)`, preserve the direct runner's status, and continue reaping both while the runner is active and during the terminal hold. Add a fixture that orphans a child and assert it is reaped.

### `tools/riscv/nixos/ltp/ltp_runner.c` line 74

> ```diff
> @@
> +    if (log_has_token(log_path, "TFAIL") || log_has_token(log_path, "TBROK"))
> +        return R_FAIL;
> +    if (log_has_token(log_path, "TCONF"))
> +        return R_CONF;
> +    if (WIFEXITED(status) && WEXITSTATUS(status) != 0)
> +        return R_FAIL;
> ```

Incorrect verdict (major): `classify()` returns `R_CONF` as soon as the log contains `TCONF`, before examining a nonzero exit status or `TWARN`. An LTP test that emits both `TCONF` and `TWARN` exits with `32 | 4`, but this branch downgrades the warning to `CONF`; pure `TWARN` is instead classified as `FAIL` through the later nonzero-status check. Cleanup failures can therefore disappear depending on whether the same test also skipped a case.

**Fix.** Shared with the other verdict-classification comments: parse recognized LTP result records with failure/warning precedence, accept CONF only for a configuration-only outcome, require TPASS for PASS, and reserve a distinct BROK path for runner/exec failures. Cover mixed `TCONF|TWARN`, empty output, and exec failure.

### `tools/riscv/nixos/ltp/ltp_runner.c` line 78

> ```diff
> @@
> +    if (WIFEXITED(status) && WEXITSTATUS(status) != 0)
> +        return R_FAIL;
> +    return R_PASS;
> ```

False pass (major): `classify()` reaches `R_PASS` whenever the process exits with `0` and produces no failure or configuration token; it never requires `TPASS`. A wrong executable such as `/bin/true`, or a test that exits before reporting any result, is therefore counted as `PASS` and can make the strict gate succeed without an executed assertion.

**Fix.** Shared with the other verdict-classification comments: require at least one real TPASS record after all failure and configuration precedence is applied; treat zero status with no recognized result as runner breakage and test it with empty output.

### `tools/riscv/nixos/ltp/ltp_runner.c` line 95

> ```diff
> @@
> +static void kill_test_group(pid_t supervisor) {
> +    if (supervisor <= 0)
> +        return;
> +    (void)kill(-supervisor, SIGKILL);
> +    (void)kill(supervisor, SIGKILL);
> +}
> ```

`test-cleanup` (major): `kill_test_group()` targets only the process group whose ID is `supervisor` plus that one process. A descendant that calls `setpgid(0, 0)` or `setsid()` escapes this group; the pinned LTP harness itself creates inner test process groups. On timeout, the supervisor and outer test can die while the actual workload survives, so the runner starts later tests while the timed-out test continues mutating guest state.

**Fix.** Shared with the other `test-cleanup` comment: contain the complete descendant tree with a PID namespace/cgroup or equivalent tracking, and terminate/reap that containment after every outcome. If descendants cannot be contained, stop the whole gate after a timeout. Test a child that creates a new process group.

### `tools/riscv/nixos/ltp/ltp_runner.c` line 177

> ```diff
> @@
> +                execv(binpath, args);
> +                dprintf(2, "TCONF: cannot exec %s\n", binpath);
> +                _exit(32);
> ```

Incorrect error classification (major): When `execv()` fails because a selected binary is corrupt, non-executable, or has a missing dynamic loader, the child reports `TCONF` and exits with `32`. `classify()` consequently returns `R_CONF`; because `CONF` does not increment `fail`, even a suite in which none of the selected binaries starts can emit `__LTP_GATE_PASS__`.

**Fix.** Shared with the other verdict-classification comments: report failure to execute a selected binary through the distinct runner-BROK path, never CONF, and add a regression using a binary with a missing interpreter.

### `tools/riscv/nixos/ltp/ltp_runner.c` line 221

> ```diff
> @@
> +        if (WIFSIGNALED(status))
> +            kill_test_group(pid);
> +        if (logfd >= 0)
> +            close(logfd);
> +
> +        int r = classify(log_path, status, timed_out);
> ```

`test-cleanup` (major): Residual-process cleanup runs only when the supervisor was signaled. A test can exit normally, including with a nonzero `TWARN` or `TFAIL` status, while leaving background children alive; those children retain the log descriptor and continue running concurrently with subsequent tests, making later verdicts order-dependent.

**Fix.** Shared with the other `test-cleanup` finding: after every test outcome, terminate and reap all remaining processes in the test's containment before closing or reading its log, including when the supervisor satisfies `WIFEXITED(status)`.

## Security

### `tools/riscv/ltp_gate.py` line 196

> ```diff
> +    before = tree_sha256(rootfs)
> ...
> +        shutil.copytree(rootfs, staged_rootfs, symlinks=True)
> ...
> +        if tree_sha256(rootfs) != before:
> +            raise RuntimeError("full LTP rootfs changed during subset packaging")
> ```

Race during tree snapshot (major): `package_subset()` hashes `rootfs` before and after `shutil.copytree()`, but never verifies that `staged_rootfs` matches the pre-copy hash. A concurrent writer can substitute malicious file contents while `copytree()` reads them and restore the source before the final `tree_sha256(rootfs)`; both checks then pass while the run-specific initramfs contains attacker-controlled code.

**Fix.** Shared with the run-snapshot comments: hash `staged_rootfs` immediately after `copytree()` and require it to equal `before` before replacing its manifest. Prefer descriptor-pinned, no-follow copies while holding the common build/snapshot lock.

### `tools/riscv/ltp_gate.py` line 291

> ```diff
> +    if runs and runs[-1].group(2) not in completed_names:
> +        current = runs[-1].group(2)
> ...
> +        f"state={state} completed={len(verdicts)}/{expected} current={current}"
> ```

`validate-at-boundaries` (minor): `progress.log` contains raw guest-controlled serial data, `_PROGRESS_RUN_RE` accepts any non-whitespace characters as a name, and `_show_status()` writes that name directly to the host terminal. A malicious guest can emit a final line such as `[RUN] 999 <OSC-52-sequence>`, causing `ltp_gate.py status` to replay terminal control characters.

**Fix.** Require every parsed name to match a tag from `selected-syscalls` and render it with an escaping function such as `ascii()` before printing. Any documented live-log viewer should likewise escape control bytes rather than replaying raw `progress.log` contents.

### `tools/riscv/ltp_gate.py` line 436

> ```diff
> +def _source_commit(repo: Path, explicit: str | None) -> str:
> +    if explicit is None:
> +        return _git_commit(repo)
> +    if re.fullmatch(r"[0-9a-f]{40}", explicit) is None:
> +        raise ValueError("source commit must be a full lowercase Git object ID")
> +    return explicit
> ```

`validate-at-boundaries` (major): `_source_commit()` treats any caller-provided string matching `[0-9a-f]{40}` as validated provenance and bypasses `_git_commit()`. Supplying a trusted commit ID through `--source-commit` or `ASTERINAS_SOURCE_COMMIT` while running a different or dirty tree makes `result.json` attribute unreviewed artifacts to the trusted commit.

**Fix.** When Git metadata is available, require the explicit value to equal `git rev-parse HEAD` and reject a dirty worktree. For containerized worktrees, consume a read-only host-produced provenance manifest that binds the commit to a verified source-tree digest; do not record a bare caller assertion as a verified `git_commit`.

### `tools/riscv/ltp_gate.py` line 561

> ```diff
> +    _validate_run_paths(repo, paths)
> ...
> +        subprocess.run(
> +            [str(repo / "tools/riscv/prepare_qemu_uboot_booti.sh"), "prepare"],
> +            cwd=repo,
> +            env=environment,
> +            check=True,
> +        )
> ```

Path replacement race (major): `_validate_run_paths()` performs a one-time `Path.resolve()` containment check, but `_run_gate()` later passes the same pathnames to `prepare_qemu_uboot_booti.sh`. An attacker can replace `target/ltp/qemu` or another ancestor with a symlink after validation; `canonical_output_dir()` then derives both its allowed root and destination through that symlink, after which preparation executes `rm -rf` and writes in an attacker-selected directory outside `target/ltp`.

**Fix.** Shared with the other pinned-evidence comment: retain `O_DIRECTORY | O_NOFOLLOW` descriptors for `target/ltp` and the run output for the whole workflow, and make preparation, normalization, checksumming, and publication operate relative to those handles.

### `tools/riscv/ltp_gate.py` line 577

> ```diff
> +        qemu = subprocess.run(
> +            _qemu_command(...),
> +            check=False,
> +        )
> +        normalized = subprocess.run(
> +            _normalizer_command(repo, paths, commit, args.smp),
> +            check=False,
> +        )
> ...
> +        document = json.loads(result_path.read_text())
> ```

Evidence replacement race (major): `qemu_uboot_booti.py` releases its descriptor-pinned evidence when its subprocess exits. `_run_gate()` then makes `ltp_result.py` reopen `serial.log` and `boot-result.json` by pathname and later reopens `result.json` to choose the gate exit code. Replacing these files after QEMU exits can turn a failing strict run into exit status `0`; because `SHA256SUMS` is generated afterward, it records hashes of the forged files.

**Fix.** Shared with the other pinned-evidence comment: normalize while QEMU still retains its evidence handles (or return retained bytes/descriptors over trusted IPC), then compute status and checksums from those handles or the in-memory document rather than reopened pathnames.

### `tools/riscv/nixos/ltp/build_ltp.sh` line 57

> ```diff
> +if [ ! -d "${LTP_SRC}" ]; then
> +    echo "missing LTP source at ${LTP_SRC}" >&2
> +    ...
> +fi
> +
> +if [[ "${SKIP_COMPILE}" -eq 0 ]]; then
> +    cd "${LTP_SRC}"
> +    make autotools >/dev/null 2>&1 || ...
> ```

`validate-at-boundaries` (major): The external `LTP_SRC` tree is validated only with `-d` before `make autotools` and later `make install` execute its build logic. A modified checkout or a retargeted `20260529` tag therefore obtains code execution inside the networked build container with the repository mounted writable, even though the documentation describes this source as pinned.

**Fix.** Define the expected LTP commit, currently `3a64d78f58bdceba93ed321e91215fb969a047ed`, and verify both `git -C "$LTP_SRC" rev-parse HEAD` and a clean `git status --porcelain --untracked-files=all` before invoking any `make` target, including with `--skip-compile`.

## Documentation

### `tools/riscv/ltp/BASELINE-M1-report.md` line 5

> ```diff
> +This milestone records the first reproducible RISC-V LTP syscall baseline on
> +the generic Sv39 QEMU platform. It is a characterization baseline, not a claim
> +that every selected syscall conforms.
> ```

`semantic-line-breaks` (nit): The report uses column wrapping instead of semantic boundaries: the first sentence is split after `on`, while line `6` finishes that sentence and starts another. Similar sentence mixing recurs throughout the report.

**Fix.** Reflow the prose so each line contains one sentence or coherent clause, for example:
```markdown
This milestone records the first reproducible RISC-V LTP syscall baseline
on the generic Sv39 QEMU platform.
It is a characterization baseline,
not a claim that every selected syscall conforms.
```

### `tools/riscv/ltp/BASELINE-M1-report.md` line 9

> ```diff
> +- Asterinas source: `37c5661cea2a2f193d1b1ccfe2bced8d62f91864`
> +- LTP source: tag `20260529`, commit
> +  `3a64d78f58bdceba93ed321e91215fb969a047ed`
> +- Kernel SHA-256:
> +  `661ca5e7de2275c5c48d560bc4932df56a5f5faffc4e5a62f745a18901baf3b0`
> +- LTP initramfs SHA-256:
> +  `36c975e2deb7982b240a187aadbc650f704f17d5e39468ab99dba186b7c78249`
> ```

Incomplete provenance (major): The tracked baseline report omits the source branch and the per-run DTB and boot-disk hashes, although `tools/riscv/ltp/README.md` explicitly requires the source branch, commit, artifact hashes, and result-directory names. Because `target/ltp/results/` is ignored, deleting generated evidence leaves no tracked record that completely binds each result row to its source and prepared boot artifacts.

**Fix.** Add a per-run provenance table containing the result-directory name, source branch or explicit detached state, full source and gate commits, LTP commit, and all identities from `result.json`—including `kernel_sha256`, `dtb_sha256`, `initrd_sha256`, and `boot_disk_sha256`.

### `tools/riscv/ltp/BASELINE-M1-report.md` line 60

> ```diff
> +- `setfsgid03`: repeatedly calls `getgrgid()` from GID 1 while the minimal
> +  `/etc/group` has no low-numbered non-root group.
> +
> +daemon:x:1:
> +bin:x:2:
> +sys:x:3:
> +adm:x:4:
> ```

Incorrect diagnosis (major): The report attributes the `setfsgid03` timeout to `/etc/group` having no low-numbered non-root group, but the packaged `tools/riscv/nixos/ltp/etc-group` already defines `daemon:x:1:`, `bin:x:2:`, `sys:x:3:`, and `adm:x:4:`. The stated cause—and the recommendation to add such a group—therefore contradicts the actual guest fixture.

**Fix.** Reanalyze the retained `setfsgid03` output to identify the actual stall, remove the missing-group diagnosis and recommendation, and document the exact lookup or kernel behavior only if the evidence supports it.

### `tools/riscv/ltp/README.md` line 3

> ```diff
> +This gate runs the Linux Test Project syscall suite on the current Asterinas
> +RISC-V kernel through U-Boot and QEMU.
> ...
> +mount. The gate validates this value before starting QEMU.
> ```

`semantic-line-breaks` (nit): The new operator guide repeatedly wraps prose by column rather than meaning: the opening sentence is split after `Asterinas`, while line `108` ends one sentence and starts another on the same source line.

**Fix.** Reflow the guide so every sentence—and longer independent clause—starts on its own line, for example:
```markdown
This gate runs the Linux Test Project syscall suite
on the current Asterinas RISC-V kernel
through U-Boot and QEMU.
```
Apply the same treatment throughout the file.
