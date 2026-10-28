# RISC-V tooling maintainability baseline

This page records the local, reproducible baseline for the
`tools/riscv` host test suite. It is intentionally small; detailed
refactoring work is tracked separately.

## Baseline

- Source: `origin/main` at `3ceeccd04` before the maintainability branch.
- Host modules: **123**
- Host tests: **2,457**
- Expected environment skips: **13**
- Production Python files: **204**
- Production shell files: **148**
- Top-level shell entrypoints: **15**
- Kernel ktest baseline (`make ktest TARGET_ARCH=riscv64 -j32`):
  **777 passed / 0 failed / 32 crates / 257 s**
- Canonical command:

```bash
make test_riscv_host HOST_TEST_JOBS=32
```

The runner discovers every `tools/riscv/tests/**/test_*.py` module, runs it in
an isolated subprocess, records per-module logs under `target/host-tests/`,
and checks the result against `tools/riscv/tests/expected_host_tests.json`.
A missing baseline test ID, an unexpected skip reason, a duplicate test ID, a
timeout, or an import failure is an error.

List the exact collected IDs without running the tests:

```bash
make test_riscv_host_list
```

The canonical unittest command remains available:

```bash
PYTHONPATH="$PWD" python3 -W error::ResourceWarning \
  -m unittest discover -s tools/riscv/tests -t . -p 'test_*.py' -v
```

## Expected skips

The baseline allowlist in `tests/expected_host_tests.json` names these
environment-gated groups:

- missing pinned Firefox archive / cached Node;
- the local experimental Firefox runner;
- the RISC-V cross compiler outside the development container;
- host DRM uapi headers that predate `VIRTGPU_CONTEXT_INIT`.

Adding any other skip requires an explicit baseline update in the same change.

## Phase 0 fixes found while measuring

The baseline run exposed three pre-existing host-test issues, fixed before the
baseline was recorded:

- `test_drm_uapi_contract`: skip explicitly when the host headers lack the
  virtgpu context-init uapi instead of failing to compile.
- `test_drm_virgl_param_gate`: wait for the lifecycle `READY` marker rather
  than a fixed number of lines, which was racy under parallel load.
- `test_physical_graphics_qemu_gate`: accept the parameterized
  `bochs-display,xres=...` device spelling.

## Next boundaries

The maintainability work proceeds in contract consolidation, test/tool splits,
and dead-code/documentation cleanup. The rule for every change is:

1. add the characterization/contract test first;
2. keep behavior unchanged for refactors;
3. run `make test_riscv_host` and the focused target;
4. update `expected_host_tests.json` only for intentional test additions or
   removals, with the reason in the same change.

Kernel changes stay opportunistic and require `make ktest TARGET_ARCH=riscv64`
with no new failures against the recorded baseline.
