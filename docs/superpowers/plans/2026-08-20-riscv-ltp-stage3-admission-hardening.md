# RISC-V LTP Stage 3 Admission Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the first RISC-V LTP gate admitted to `main` incapable of false success, descendant leakage, mutable evidence, missing-helper packaging, or an accidental SMP=1 default.

**Architecture:** Reuse the already-reviewed process isolation and run-owned evidence commits by moving them ahead of the Stage 3 pull-request boundary. Add only two new changes: a fail-closed BusyBox build preflight and an LTP-local SMP=4 default. Keep baseline narratives, named architecture suites, kernel point fixes, and loop devices in later stages.

**Tech Stack:** Python 3 `unittest`, C11 guest runner, Bash packaging, GNU Make, QEMU `virt`, Docker.

---

## File Map

- `tools/riscv/nixos/ltp/ltp_runner.c` — classify guest outcomes and isolate each test process group.
- `tools/riscv/nixos/ltp/init_ltp.c` — keep PID 1 alive and reap guest children.
- `tools/riscv/ltp_gate.py` — own preparation inputs and evidence by run ID; default its CLI to SMP=4.
- `tools/riscv/nixos/ltp/build_ltp.sh` — fail before packaging when the required static BusyBox is absent.
- `tools/riscv/nixos/build_busybox.sh` — reproducibly build the required static RISC-V BusyBox artifact.
- `tools/riscv/ltp/README.md` — document the BusyBox prerequisite and SMP=4 normal path.
- `Makefile` — expose an LTP-specific `RISCV_LTP_SMP ?= 4` without changing the repository-wide `SMP ?= 1` default.
- `tools/riscv/tests/test_ltp_guest_runner.py` — executable guest-runner lifecycle regressions.
- `tools/riscv/tests/test_ltp_gate.py` — run-owned evidence, CLI-default, Makefile, documentation, and build-script contracts.

### Task 1: Move Verified Gate Correctness Ahead of the PR Boundary

**Files:**
- Modify: `tools/riscv/nixos/ltp/ltp_runner.c`
- Modify: `tools/riscv/nixos/ltp/init_ltp.c`
- Modify: `tools/riscv/ltp_gate.py`
- Modify: `tools/riscv/nixos/ltp/build_ltp.sh`
- Create: `tools/riscv/nixos/build_busybox.sh`
- Test: `tools/riscv/tests/test_ltp_guest_runner.py`
- Test: `tools/riscv/tests/test_ltp_gate.py`
- Test: `tools/riscv/tests/test_ltp_result.py`

- [ ] **Step 1: Move the approved design commit onto the complete Stage 4 hardening tip**

Run from `codex/asterinas-main-ltp-stage3-gate`:

```bash
git rebase --onto 5fbc6545454cd1bf101b092f053e5bcbb16b8327 \
  8c4d03f9aae40d40455279140c95aadb001798a5
```

Expected: the design commit is replayed after the eleven already-verified
Stage 4 hardening commits with no conflict.

- [ ] **Step 2: Port the two dependency-complete evidence/lifecycle fixes**

Run:

```bash
git cherry-pick f27d29d472a4845ad33eb620dd1ed4dd2d894a1e
git cherry-pick 9e204cc06e4bcfaf664bf512eaac897c04678869
```

Expected: `f27d29d47` binds checksums to prepared run-owned payloads, and
`9e204cc06` turns failed exec/TWARN/empty output into FAIL while preserving a
real exit-32 TCONF.

- [ ] **Step 3: Run the focused guest and gate tests**

Run inside `asterinas/asterinas:0.18.0-20260702`:

```bash
make test_riscv_ltp_unit
```

Expected: all tests pass, including failed exec, parent-killing test,
run-owned prepared directory, and prepared-artifact identity checks.

- [ ] **Step 4: Verify the moved commit range is limited to LTP tooling, its design documents, and existing QEMU contracts**

Run:

```bash
git diff --name-only origin/main...HEAD
git diff --check origin/main...HEAD
```

Expected: no kernel, DRM, NixOS desktop, audio, or loop-device implementation
files appear; whitespace validation exits zero.

### Task 2: Require BusyBox Before Packaging

**Files:**
- Modify: `tools/riscv/tests/test_ltp_guest_runner.py`
- Modify: `tools/riscv/tests/test_ltp_gate.py`
- Modify: `tools/riscv/nixos/ltp/build_ltp.sh`
- Modify: `tools/riscv/ltp/README.md`

- [ ] **Step 1: Write failing build and documentation contract tests**

Add to `LtpBuildScriptContractTests`:

```python
def test_builder_requires_busybox_before_replacing_rootfs(self) -> None:
    source = BUILD_SCRIPT.read_text()

    preflight = 'if [[ ! -x "${BUSYBOX}" ]]; then'
    destructive_stage = 'rm -rf "${ROOTFS}"'
    self.assertIn(preflight, source)
    self.assertLess(source.index(preflight), source.index(destructive_stage))
    self.assertIn("missing required BusyBox", source)
    self.assertNotIn("WARN: no busybox", source)
```

Add to `LtpGateDocumentationTests`:

```python
OPERATOR_GUIDE = REPO / "tools/riscv/ltp/README.md"
BUSYBOX_BUILDER = REPO / "tools/riscv/nixos/build_busybox.sh"


def test_operator_guide_builds_the_required_busybox(self) -> None:
    source = OPERATOR_GUIDE.read_text()

    self.assertTrue(BUSYBOX_BUILDER.is_file())
    builder = BUSYBOX_BUILDER.read_text()
    self.assertIn('CROSS_PREFIX="riscv64-linux-gnu-"', builder)
    self.assertIn("ASH", builder)
    self.assertIn("CAT", builder)
    self.assertIn("TRUE", builder)
    self.assertIn("tools/riscv/nixos/build_busybox.sh", source)
    self.assertIn("target/nixos/busybox", source)
```

- [ ] **Step 2: Run the two focused tests and verify RED**

Run:

```bash
PYTHONPATH=tools/riscv python3 -m unittest \
  tools.riscv.tests.test_ltp_guest_runner.LtpBuildScriptContractTests.test_builder_requires_busybox_before_replacing_rootfs \
  tools.riscv.tests.test_ltp_gate.LtpGateDocumentationTests.test_operator_guide_builds_the_required_busybox -v
```

Expected: both tests fail because the script still warns after deleting the
old rootfs and the BusyBox builder is absent from this branch.

- [ ] **Step 3: Port the reproducible BusyBox builder**

Create `tools/riscv/nixos/build_busybox.sh` from the reviewed standalone file
at `d1d142cae:tools/riscv/nixos/build_busybox.sh`. It must pin BusyBox 1.36.1,
start from `allnoconfig`, use `riscv64-linux-gnu-` static linking, enable at
least ASH/CAT/TRUE/ECHO/TEST plus the existing NixOS smoke applets, strip the
result, and publish to `target/nixos/busybox`.

- [ ] **Step 4: Add the fail-closed preflight before rootfs publication**

Immediately after the existing compiler/LTP-source checks in
`build_ltp.sh`, add:

```bash
BUSYBOX="${REPO_ROOT}/target/nixos/busybox"
if [[ ! -x "${BUSYBOX}" ]]; then
    echo "missing required BusyBox at ${BUSYBOX}" >&2
    echo "build it with: tools/riscv/nixos/build_busybox.sh" >&2
    exit 2
fi
```

Replace the later optional BusyBox `if` block with unconditional staging:

```bash
mkdir -p "${ROOTFS}/bin"
cp -f "${BUSYBOX}" "${ROOTFS}/bin/busybox"
for applet in sh cat true echo test; do
    ln -sf busybox "${ROOTFS}/bin/${applet}"
done
echo "busybox applets: $(ls "${ROOTFS}/bin")"
```

- [ ] **Step 5: Document the prerequisite with the actual repository command**

Before the LTP build command in `tools/riscv/ltp/README.md`, state that the
static helper is mandatory and show:

```bash
tools/riscv/nixos/build_busybox.sh
test -x target/nixos/busybox
```

- [ ] **Step 6: Run the focused tests and verify GREEN**

Run the exact command from Step 2.

Expected: 2 tests pass.

- [ ] **Step 7: Commit the BusyBox contract**

```bash
git add tools/riscv/nixos/ltp/build_ltp.sh \
  tools/riscv/nixos/build_busybox.sh \
  tools/riscv/ltp/README.md \
  tools/riscv/tests/test_ltp_guest_runner.py \
  tools/riscv/tests/test_ltp_gate.py
git commit -m "build(riscv): require BusyBox for the LTP gate"
```

### Task 3: Make SMP=4 the LTP-Local Default

**Files:**
- Modify: `tools/riscv/tests/test_ltp_gate.py`
- Modify: `tools/riscv/ltp_gate.py`
- Modify: `Makefile`
- Modify: `tools/riscv/ltp/README.md`

- [ ] **Step 1: Write failing CLI and Makefile tests**

Import `_parse_args`, define the repository Makefile path, and add to
`LtpGatePolicyTests`:

```python
from ltp_gate import _parse_args

REPO_MAKEFILE = REPO / "Makefile"


def test_run_defaults_to_smp4(self) -> None:
    args = _parse_args(
        ["run", "--kernel", "target/osdk/kernel.Image", "--dry-run"]
    )

    self.assertEqual(args.smp, 4)

def test_makefile_keeps_smp4_default_local_to_ltp(self) -> None:
    source = REPO_MAKEFILE.read_text()

    self.assertIn("SMP ?= 1", source)
    self.assertIn("RISCV_LTP_SMP ?= 4", source)
    self.assertIn('--smp "$(RISCV_LTP_SMP)"', source)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
PYTHONPATH=tools/riscv python3 -m unittest \
  tools.riscv.tests.test_ltp_gate.LtpGatePolicyTests.test_run_defaults_to_smp4 \
  tools.riscv.tests.test_ltp_gate.LtpGatePolicyTests.test_makefile_keeps_smp4_default_local_to_ltp -v
```

Expected: the CLI reports `1`, and the Makefile has no `RISCV_LTP_SMP`.

- [ ] **Step 3: Implement only the LTP-local defaults**

Change the CLI parser to:

```python
run.add_argument("--smp", type=int, choices=(1, 4), default=4)
```

Keep `SMP ?= 1` unchanged and add near it:

```make
RISCV_LTP_SMP ?= 4
```

Change the LTP Make recipe to:

```make
	@python3 tools/riscv/ltp_gate.py run \
		--kernel "$(ASTERINAS_RISCV_BOOTI)" --smp "$(RISCV_LTP_SMP)"
```

- [ ] **Step 4: Update operator examples**

Make the five-test SMP=4 smoke and complete SMP=4 baseline the normal path.
Document `RISCV_LTP_SMP=1 make test_riscv_ltp` as the explicit single-hart
diagnostic override; do not require a paired SMP=1 full run.

- [ ] **Step 5: Run the focused tests and verify GREEN**

Run the exact command from Step 2.

Expected: 2 tests pass.

- [ ] **Step 6: Commit the SMP=4 default**

```bash
git add Makefile tools/riscv/ltp_gate.py tools/riscv/ltp/README.md \
  tools/riscv/tests/test_ltp_gate.py
git commit -m "test(riscv): default the LTP gate to SMP=4"
```

### Task 4: Verify, Review, and Restack

**Files:**
- Verify all Stage 3 files changed over `origin/main`
- Restack: `codex/asterinas-main-ltp-stage5-evidence`
- Restack: `codex/asterinas-main-ltp-stage6-arch-suite`

- [ ] **Step 1: Run the complete exact-head local matrix**

Run in the prescribed project Docker image:

```bash
make check
make docs
make test_riscv_ltp_unit
make test_riscv_uboot_booti_unit
python3 tools/riscv/ltp_gate.py run \
  --kernel target/osdk/aster-kernel-osdk-bin.Image \
  --run-id stage3-smp4-dry --skip-build --dry-run \
  --tag getpid01 --tag read01 --tag write01 \
  --tag uname01 --tag clock_gettime01
git diff --check origin/main...HEAD
```

Expected: all checks exit zero; the dry-run selects
`generic-sv39-ltp-smp4`, names `target/ltp/qemu/smp4/stage3-smp4-dry`, and
does not name `target/qemu-uboot/current`.

- [ ] **Step 2: Re-review the final Stage 3 range**

Review `origin/main..HEAD`, requiring zero Critical or Important findings for
guest classification, process cleanup, BusyBox, evidence ownership, or SMP
default behavior.

- [ ] **Step 3: Restack only commits not absorbed by Stage 3**

Preserve the baseline report and later evidence commits on the evidence
branch, dropping patch-equivalent lifecycle/evidence commits already present
in Stage 3. Rebase the named architecture-suite branch onto the resulting
evidence tip. Verify every worktree is clean and every downstream tip has the
new Stage 3 tip as an ancestor.

- [ ] **Step 4: Push and open the Stage 3 pull request**

Push only `codex/asterinas-main-ltp-stage3-gate`. Create a pull request to
`main` that records the exact tested head and local commands. Do not push
later stages and do not query or wait for remote CI.
