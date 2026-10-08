---
date: 2026-08-21
mode: diff
base: b54aad2f8
head: 3f1fc5806
branch: codex/remote-main-desktop
title: "RISC-V NixOS Route B R0 and runner foundation final review"
---

# Summary

No confirmed Critical or Major defects remain after three review-and-fix rounds.
The final branch passes 71 focused tests,
the fixed 108-record manifest regeneration,
shallow-checkout coverage,
static C/Python checks,
and the strict and fault-injected Docker credential paths.

The remaining findings are non-blocking cleanup:
four maintainability minors,
one correctness minor in mutually exclusive test-only option parsing,
and five semantic-line-break nits.
Before any upstream submission,
the existing R0, R1-B, and M7 commit boundaries can also be used to create
smaller review units without changing this local milestone branch.

## Maintainability

### `docs/superpowers/plans/2026-08-21-nixos-riscv-route-b-r0-runner.md` line 81

> ```diff
> +**Files:**
> +- Create: `tools/riscv/nixos_track_audit.py`
> +- Create: `tools/riscv/tests/test_nixos_track_audit.py`
> +
> +- [x] **Step 1: Write parser and classification tests first**
> ...
> +- [x] **Step 2: Run the focused test and confirm RED**
> ```

`least-surprise` (minor): In commit `0a0c504ab`, `Task 2` marks `Step 1` and `Step 2` complete even though `tools/riscv/nixos_track_audit.py` and its tests do not appear until commit `437f10a9e`. The same premature `- [x]` state is used throughout later tasks, so the plan's history does not reliably show when work was completed.

**Fix.** Commit future work as `- [ ]`, then change each item to `- [x]` in the commit that implements or verifies it. Rebase this series so the progress state at every commit matches the files present there.

### `tools/riscv/nixos_track_audit.py` line 35

> ```diff
> +_OVERRIDE_FIELDS = frozenset(
> +    {
> +        "disposition",
> +        "reason",
> +        "destination",
> +        "subsystem",
> +        "main_equivalent_or_pr",
> +        "verification",
> +    }
> +)
> +_METADATA_FIELDS = (
> +    "reason",
> +    "destination",
> +    "subsystem",
> +    "main_equivalent_or_pr",
> +    "verification",
> +)
> ```

`dry` (minor): The reviewed metadata fields `reason`, `destination`, `subsystem`, `main_equivalent_or_pr`, and `verification` are independently repeated in `AdmissionRecord`, `_OVERRIDE_FIELDS`, `_METADATA_FIELDS`, and `render_manifest()`. Adding or renaming one field therefore requires synchronized edits in four representations.

**Fix.** Define the metadata field names once in `_METADATA_FIELDS`, derive `_OVERRIDE_FIELDS` from `{"disposition", *_METADATA_FIELDS}`, and use that same tuple when copying metadata into the rendered record while keeping core fields explicit.

### `tools/riscv/tests/test_nixos_m7_assets.py` line 94

> ```diff
> +    def test_reproducer_runs_to_completion_with_verified_credentials(self) -> None:
> +        compiler = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
> ...
> +            if os.geteuid() == 0:
> +                parent_pid, returncode, stdout, stderr = run_reproducer(
> +                    "--require-distinct-ids"
> +                )
> ...
> +            for argument, timeout_marker in (
> +                ("--test-exit-before-connect", "__M7_ACCEPT_TIMEOUT_FAIL__"),
> +                ("--test-stall-after-connect", "__M7_RECV_TIMEOUT_FAIL__"),
> +            ):
> ```

`single-responsibility` (minor): `test_reproducer_runs_to_completion_with_verified_credentials()` combines host capability probing, compilation, timeout-managed process execution, default-mode assertions, privilege-dependent `--require-distinct-ids` assertions, and two fault-injection modes. These behaviors have separate reasons to change, and a failure does not identify which contract broke.

**Fix.** Extract `_compile_reproducer()` and `_run_reproducer()` helpers, then create focused tests for the default peer PID, distinct IDs, `--test-exit-before-connect`, and `--test-stall-after-connect` behaviors.

### `tools/riscv/tests/test_nixos_track_audit.py` line 246

> ```diff
> +    def test_checked_in_track_admission_inventory_is_complete(self) -> None:
> +        manifest = json.loads(TRACK_ADMISSION_MANIFEST.read_text(encoding="utf-8"))
> ...
> +        for commit in (
> +            "7f081686e89b2db02ccd8f9cd5c6348f3ab6a53b",
> +            "b62561964230974dc7e7e9606509c41c478eec1c",
> +            "f0ecc340a952d5a7a0c13eab5cc4d510472ac2f2",
> +        ):
> ...
> +        existing_prs = {
> +            "39dedb0aa4ca1a48814f55bac8c37881813f13cd": "#43",
> +            "89216fa1ef78365174391a96063f847fe0eec8d6": "#44",
> +            "538ed5168c9c2e29f24a782a91343096de50d387": "#45",
> +        }
> ```

`single-responsibility` (minor): `test_checked_in_track_admission_inventory_is_complete()` combines regeneration and schema checks with uniqueness policy, the exact automatic count `20`, loop routing to `#70`, `M7` routing to `#67`, mixed-commit exceptions, and all `7` open-PR mappings. A failure in any policy points only to an omnibus test whose name does not identify the broken contract.

**Fix.** Keep regeneration and schema completeness in this test. Move routing and exception policies into focused methods such as `test_loop_commits_route_to_issue_70()`, `test_mixed_commits_keep_selective_destinations()`, and `test_existing_pr_records_match_open_topics()`, sharing the loaded manifest through `setUpClass()` or a helper.

## Correctness

### `tools/riscv/nixos/m7/scm_repro.c` line 149

> ```diff
> } else if (strcmp(argv[index], "--test-exit-before-connect") == 0) {
>   options.test_fault = TEST_FAULT_EXIT_BEFORE_CONNECT;
> } else if (strcmp(argv[index], "--test-stall-after-connect") == 0) {
>   options.test_fault = TEST_FAULT_STALL_AFTER_CONNECT;
> }
> ```

Incorrect option handling (minor): `parse_options()` presents `--test-exit-before-connect` and `--test-stall-after-connect` as mutually exclusive but accepts both. For example, passing them in that order silently overwrites `options.test_fault` and exercises only `TEST_FAULT_STALL_AFTER_CONNECT`, so a fault-injection run can test the wrong path without reporting invalid input.

**Fix.** Before assigning either fault, reject the option when `options.test_fault != TEST_FAULT_NONE` and exit with status `2`. Add a regression that passes both flags and expects the usage error.

## Documentation

### `docs/superpowers/plans/2026-08-21-nixos-riscv-route-b-r0-runner.md` line 201

> ```diff
> The loop commits `7f081686e8`, `b625619642`, and `f0ecc340a9` must all target the
> loop child issue. The old Nix-track LTP harness must not replace the Stage 6
> gate already in `main`.
> ```

`semantic-line-breaks` (nit): `docs/superpowers/plans/2026-08-21-nixos-riscv-route-b-r0-runner.md` starts `The old Nix-track...` on the same physical line as the preceding sentence and splits `the loop child issue` across lines. Similar column wrapping recurs later in the plan instead of breaking at sentence or clause boundaries.

**Fix.** Reflow the affected prose so every sentence begins on a new line and any internal breaks occur after complete clauses.

### `docs/superpowers/specs/2026-08-21-nixos-riscv-route-b-decomposition-design.md` line 32

> ```diff
> `track/nixos` is 109 commits ahead of and 159 commits behind `main` from their
> merge base. A patch-id comparison of its 108 non-merge commits reports 20
> patch-equivalent commits already in `main` and 88 nominally unique commits. The
> nominally unique set also
> contains changes already merged through rewritten PRs
> ```

`semantic-line-breaks` (nit): `docs/superpowers/specs/2026-08-21-nixos-riscv-route-b-decomposition-design.md` joins the end of one sentence to `A patch-id comparison...` and later splits `nominally unique set also contains` mid-phrase. This is column wrapping rather than one coherent idea per line.

**Fix.** Start each sentence on its own line and place optional internal breaks before or after complete clauses.

### `tools/nixos/riscv_preflight.py` line 85

> ```diff
>     Each path is opened once. The nonblocking, read-only open follows symlinks.
>     Validation uses ``fstat`` on the descriptor, which is always closed in a
>     ``finally`` block without reading its contents. Checks across artifacts are
>     not atomic, and this function does not authenticate or freeze artifacts
> ```

`semantic-line-breaks` (nit): The `check_artifacts()` docstring places two complete sentences on line `85`, starts `Checks across artifacts...` after another sentence on line `87`, and otherwise wraps inside phrases. The doc comment does not follow semantic line breaks.

**Fix.** Put each sentence on a separate source line and keep longer clauses, such as the explanation of the `finally` block, intact.

### `tools/riscv/nixos/TRACK-ADMISSION-M1-report.md` line 25

> ```diff
> - The 109 ahead commits include merge
>   `fabd4693bd4464b8236cf83f6244af74135a048b`. `git cherry` intentionally
>   omits that merge, so its 108 lines agree with the non-merge inventory.
> ```

`semantic-line-breaks` (nit): The bullet in `tools/riscv/nixos/TRACK-ADMISSION-M1-report.md` starts the `git cherry` sentence on the same line as the preceding commit-ID sentence. Other report paragraphs similarly combine sentence boundaries with width-based wrapping.

**Fix.** Start the `git cherry` sentence on a new indented line and reflow the remaining prose at sentence or clause boundaries.

### `tools/riscv/nixos/m7/README.md` line 20

> ```diff
> A single five-second monotonic transaction deadline covers connection
> acceptance, SCM_RIGHTS receive readiness, and child exit. Timeout and other
> parent failures after fork attempt `SIGKILL` and WNOHANG reap under a separate
> 250 ms cleanup deadline. The child arms `PR_SET_PDEATHSIG(SIGKILL)` before and
> after credential changes, so a parent lacking permission to signal the
> 65534:65534 child still exits promptly and the child dies with it. Cleanup
> ```

`semantic-line-breaks` (nit): `tools/riscv/nixos/m7/README.md` repeatedly begins a new sentence on the same physical line as the previous one, including `Timeout and other...`, `The child...`, and `Cleanup...`. Later paragraphs repeat the pattern with `They verify...` and `Its marker is`.

**Fix.** Reflow these paragraphs so each sentence starts on its own line and longer sentences break only at clause boundaries.

## Retracted by verification

- `focused-prs` (major): retracted because the reviewed target is the explicitly
  authorized local Route B R0 milestone branch, not one upstream PR.
  R0, R1-B, and M7 already have independent issues, commits, and verification;
  PR splitting remains an integration choice before submission.
- `dry` on the preflight QEMU constants (minor): retracted because this module
  intentionally freezes a self-contained R1-B contract.
  Cross-check tests against `GENERIC_SV39` and `QEMU_VIRT_SMP4` make profile
  drift an explicit review event instead of silently changing the runner.
- `consistency` on disposition/classification (minor): retracted because
  `disposition` is the reviewed override action while `classification` is the
  exported manifest result; preserving both layers is intentional for audit
  provenance.
