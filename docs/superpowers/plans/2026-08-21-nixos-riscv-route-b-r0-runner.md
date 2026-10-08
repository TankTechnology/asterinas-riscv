# NixOS RISC-V Route B R0 and Runner Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL:
> Use superpowers:subagent-driven-development (recommended)
> or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record the published Route B child-issue structure,
create an auditable `track/nixos` admission matrix,
and add the first testable RISC-V NixOS artifact/preflight contract
without merging the divergent branch wholesale.

**Architecture:** Issue #62 remains the epic,
and eight child issues own independently testable modules.
A Python audit tool combines `git cherry` output with an explicit override file,
so patch equivalence is reproducible
while rewritten PRs and human dispositions remain reviewable.
A separate Python preflight module defines
the RISC-V NixOS boot artifact contract and command construction;
it does not claim that a full closure exists
or modify the x86 runner yet.

**Tech Stack:** Git/GitHub CLI, Python 3 standard library (`argparse`, `dataclasses`, `json`, `pathlib`, `subprocess`, `unittest`), Bash smoke commands, QEMU RISC-V artifact conventions.

---

### Task 1: Verify the published Route B issue graph

**Status:** Completed before this plan was finalized.
This section is a read-only historical verification record,
not an issue-publication recipe.

**Files:**
- Reference: `docs/superpowers/specs/2026-08-21-nixos-riscv-route-b-decomposition-design.md`
- External, read-only: GitHub issues in `TankTechnology/asterinas-riscv`

- [x] **Step 1: Record the fixed issue mapping**

| Module | Issue | Exact title | Priority |
|---|---:|---|---|
| R0 | #63 | `[NixOS RISC-V R0] Reconcile track/nixos and build an admission matrix` | `p0-now` |
| R1-A | #64 | `[NixOS RISC-V R1-A] Build a real reproducible riscv64 NixOS closure` | `p0-now` |
| R1-B | #65 | `[NixOS RISC-V R1-B] Persistent root disk, stage 1, and QEMU runner` | `p0-now` |
| R2 | #66 | `[NixOS RISC-V R2] Boot generated stage 2 and systemd service foundation` | `p1-next` |
| R3 | #67 | `[NixOS RISC-V R3] nix-daemon, persistent generations, DNS, and HTTPS` | `p1-next` |
| R4 | #68 | `[NixOS RISC-V R4] Graphical NixOS closure with interactive browser` | `p1-next` |
| R5 | #69 | `[NixOS RISC-V R5] SMP=4, MIT-SHM, graphical and evidence hardening` | `p2-later` |
| Loop | #70 | `[Kernel] Admit the track/nixos loop device subsystem` | `p1-next` |

Every issue was published with `track/nixos`, `type/infra`,
the listed priority label, and `Parent: #62` in its body.

- [x] **Step 2: Retain read-only verification commands**

Only inspect the already-published records:

```bash
for issue_number in 63 64 65 66 67 68 69 70; do
  gh issue view "$issue_number" -R TankTechnology/asterinas-riscv \
    --json number,title,state,labels,body,url
done
gh issue list -R TankTechnology/asterinas-riscv --state all --limit 100 \
  --json number,title,state,labels,url
```

Expected: each fixed number still has its exact title,
required labels, and `Parent: #62` linkage.

- [x] **Step 3: Preserve the publication boundary**

If any fixed issue is missing or stale, stop this plan.
Open a separate, explicit issue-publication task with fresh approval;
do not repair external state from this historical implementation plan.

### Task 2: Add the track admission parser with failing tests

**Files:**
- Create: `tools/riscv/nixos_track_audit.py`
- Create: `tools/riscv/tests/test_nixos_track_audit.py`

- [x] **Step 1: Write parser and classification tests first**

Create tests covering patch-equivalent, unique, malformed, and explicit
override records:

```python
class TrackAdmissionParserTests(unittest.TestCase):
    def test_parse_cherry_distinguishes_equivalent_and_unique(self) -> None:
        records = parse_cherry(
            "- " + "a" * 40 + " already landed\n"
            "+ " + "b" * 40 + " portable tool\n"
        )
        self.assertEqual(records[0].automatic_disposition, "already-main")
        self.assertEqual(records[1].automatic_disposition, "unclassified")

    def test_override_supplies_auditable_human_disposition(self) -> None:
        record = parse_cherry("+ " + "b" * 40 + " portable tool\n")[0]
        classified = apply_overrides(
            [record],
            {"b" * 40: {"disposition": "portable", "reason": "isolated userspace smoke", "destination": "R0"}},
        )
        self.assertEqual(classified[0].disposition, "portable")
        self.assertEqual(classified[0].destination, "R0")

    def test_malformed_cherry_line_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid git cherry record"):
            parse_cherry("? not-a-hash subject\n")
```

- [x] **Step 2: Run the focused test and confirm RED**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_nixos_track_audit -v
```

Expected: import failure because `nixos_track_audit.py` does not yet exist.

- [x] **Step 3: Implement the minimal parser and override model**

Implement frozen `AdmissionRecord` values, strict 40-hex parsing, duplicate
hash rejection, disposition validation against
`already-main|existing-pr|portable|rewrite|retire|unclassified`, and JSON output.
The public functions are:

```python
def parse_cherry(text: str) -> list[AdmissionRecord]: ...
def load_overrides(path: Path) -> dict[str, dict[str, str]]: ...
def apply_overrides(records: Sequence[AdmissionRecord], overrides: Mapping[str, Mapping[str, str]]) -> list[AdmissionRecord]: ...
def render_manifest(base: str, track: str, records: Sequence[AdmissionRecord]) -> dict[str, object]: ...
```

The CLI accepts exact commit IDs for a reproducible point-in-time audit:

```text
--base b54aad2f89ce529691dd9944dac53bf33c8dcb93
--track 44172c41cb914e510ec45fb8b65441b0fafa4c6b
--overrides tools/riscv/nixos/track-admission-overrides.v1.json
--output tools/riscv/nixos/track-admission.v1.json
```

It runs `git cherry -v BASE TRACK`, verifies both objects are commits, writes
deterministic sorted/indented JSON, and reports counts per disposition.

- [x] **Step 4: Run focused tests and confirm GREEN**

Run the same unittest command.

Expected: all parser tests pass.

- [x] **Step 5: Commit the parser slice**

```bash
git add tools/riscv/nixos_track_audit.py tools/riscv/tests/test_nixos_track_audit.py
git commit -m "Add NixOS track admission audit"
```

### Task 3: Commit the initial five-way admission inventory

**Files:**
- Create: `tools/riscv/nixos/track-admission-overrides.v1.json`
- Create: `tools/riscv/nixos/track-admission.v1.json`
- Create: `tools/riscv/nixos/TRACK-ADMISSION-M1-report.md`
- Modify: `tools/riscv/tests/test_nixos_track_audit.py`

- [x] **Step 1: Add schema/inventory assertions before the inventory**

Add tests that load the committed manifest and assert:

```python
self.assertEqual(manifest["schema_version"], 1)
self.assertEqual(manifest["base"], "b54aad2f89ce529691dd9944dac53bf33c8dcb93")
self.assertEqual(manifest["track"], "44172c41cb914e510ec45fb8b65441b0fafa4c6b")
self.assertEqual(len(manifest["records"]), 108)
self.assertNotIn("unclassified", manifest["counts"])
self.assertEqual(sum(manifest["counts"].values()), 108)
```

Also assert every record has a nonempty reason and destination, except
`already-main`, which may use `destination="main"`.

- [x] **Step 2: Run the focused tests and confirm RED**

Expected: failure because the committed manifest does not exist.

- [x] **Step 3: Populate explicit dispositions**

Classify all 108 non-merge commits using these rules:

```text
- patch-id equivalent or rewritten merged PR -> already-main
- represented by open PR #43-#47/#49-#53/#55 -> existing-pr
- Nix M2-M9 user-space builders/reproducers with current value -> portable
- systemd M2 on the pre-D-Bus tree and old integration hooks -> rewrite
- superseded LTP orchestration/stale result-only reports -> retire
- isolated unmerged kernel change with a focused repro path -> portable, with a dedicated destination issue/PR
```

The loop commits `7f081686e8`, `b625619642`, and `f0ecc340a9` must all target the
loop child issue. The old Nix-track LTP harness must not replace the Stage 6
gate already in `main`.

- [x] **Step 4: Generate and inspect the manifest**

Run:

```bash
python3 tools/riscv/nixos_track_audit.py \
  --base b54aad2f89ce529691dd9944dac53bf33c8dcb93 \
  --track 44172c41cb914e510ec45fb8b65441b0fafa4c6b \
  --overrides tools/riscv/nixos/track-admission-overrides.v1.json \
  --output tools/riscv/nixos/track-admission.v1.json
```

Expected: 108 records, zero unclassified records, and summary counts for all
five dispositions.

- [x] **Step 5: Write the M1 report from the reviewed manifest**

The report records the branch divergence, five-way counts, already-merged PRs,
open PRs that must not be duplicated, first portable batch, rewrite batch,
retired LTP assets, and the exact command to regenerate the JSON. It explicitly
states that `git cherry` omits the one merge commit from the 109-ahead branch
count.

- [x] **Step 6: Run tests and commit**

```bash
python3 -m unittest tools.riscv.tests.test_nixos_track_audit -v
git diff --check
git add tools/riscv/nixos/track-admission-overrides.v1.json \
        tools/riscv/nixos/track-admission.v1.json \
        tools/riscv/nixos/TRACK-ADMISSION-M1-report.md \
        tools/riscv/tests/test_nixos_track_audit.py
git commit -m "Classify NixOS track admission candidates"
```

### Task 4: Add the RISC-V NixOS artifact/preflight contract with TDD

**Files:**
- Create: `tools/nixos/riscv_preflight.py`
- Create: `tools/riscv/tests/test_nixos_riscv_preflight.py`

- [x] **Step 1: Write failing artifact-contract tests**

Cover a complete artifact set, grouped missing inputs,
internally fixed SMP=4 behavior, snapshot root writes,
and shell-safe dry-run rendering:

```python
class RiscvNixosPreflightTests(unittest.TestCase):
    def test_default_contract_uses_smp4_and_snapshot_root_disk(self) -> None:
        contract = ArtifactContract.from_repo(Path("/repo"))
        argv = qemu_argv(contract)
        self.assertEqual(argv[argv.index("-smp") + 1], "4")
        root_drive = argv[argv.index("-drive", argv.index("-drive") + 1) + 1]
        self.assertIn("snapshot=on", root_drive)
        self.assertIn("id=rootdisk", root_drive)

    def test_preflight_reports_every_missing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            failures = check_artifacts(ArtifactContract.from_repo(Path(directory)))
        self.assertEqual({failure.kind for failure in failures}, {"uboot", "boot-disk", "root-disk", "dtb"})

    def test_boot_and_root_disks_are_distinct(self) -> None:
        contract = replace(
            ArtifactContract.from_repo(Path("/repo")),
            root_disk=Path("/repo/target/nixos/riscv64/boot.ext4"),
        )
        with self.assertRaisesRegex(ValueError, "boot and root disk"):
            qemu_argv(contract)
```

- [x] **Step 2: Run focused tests and confirm RED**

```bash
python3 -m unittest tools.riscv.tests.test_nixos_riscv_preflight -v
```

Expected: import failure because the module is absent.

- [x] **Step 3: Implement the focused preflight module**

Define:

```python
@dataclass(frozen=True)
class ArtifactContract:
    uboot: Path
    boot_disk: Path
    root_disk: Path
    dtb: Path

    @classmethod
    def from_repo(cls, repo: Path) -> "ArtifactContract": ...

@dataclass(frozen=True)
class PreflightFailure:
    kind: str
    path: Path
    remedy: str

def check_artifacts(contract: ArtifactContract) -> tuple[PreflightFailure, ...]: ...
def qemu_argv(contract: ArtifactContract, *, qemu: str = "qemu-system-riscv64") -> list[str]: ...
```

Default paths are:

```text
target/qemu-uboot/cache/u-boot-build/u-boot
target/nixos/riscv64/boot.ext4
target/nixos/riscv64/root.ext2
target/nixos/riscv64/qemu-virt.dtb
```

The module CLI supports `--check` and `--print-qemu`.
It validates file type and nonzero size,
reports all failures in one run,
returns 2 for missing inputs,
and never starts QEMU.
SMP is not part of `ArtifactContract` or a caller override;
the runner keeps it internally fixed at four harts.
The generated command uses `-machine virt`,
the current Sv39-compatible CPU contract, `-m 2G`, `-smp 4`,
two virtio-mmio block devices, virtio-gpu/input/net,
serial stdio, and `snapshot=on` for the root disk.

- [x] **Step 4: Run focused tests and confirm GREEN**

Expected: all preflight tests pass.

- [x] **Step 5: Verify the real checkout failure is actionable**

```bash
python3 tools/nixos/riscv_preflight.py --check
```

Expected before R1-A/R1-B artifacts exist: exit 2 with one line per missing
artifact and an exact producer/remedy; no traceback and no QEMU process.

- [x] **Step 6: Commit the preflight slice**

```bash
git add tools/nixos/riscv_preflight.py tools/riscv/tests/test_nixos_riscv_preflight.py
git commit -m "Define RISC-V NixOS boot artifact preflight"
```

### Task 5: Admit the first safe `track/nixos` batch

**Files:**
- Create from reviewed source: `tools/riscv/nixos/m7/scm_repro.c`
- Create: `tools/riscv/nixos/m7/README.md`
- Create: `tools/riscv/tests/test_nixos_m7_assets.py`
- Modify: `tools/riscv/nixos/track-admission-overrides.v1.json`
- Modify: `tools/riscv/nixos/track-admission.v1.json`
- Modify: `tools/riscv/nixos/TRACK-ADMISSION-M1-report.md`

- [x] **Step 1: Add a failing source-asset test**

The test asserts that the admitted reproducer is the isolated SCM_RIGHTS and
SO_PEERCRED probe, carries an SPDX header, contains no absolute `target/` or
sibling-checkout dependency, and documents its original commit
`8a7396a1fae4dfce21b2d0e19794b83dd7771bd8`.

- [x] **Step 2: Run the focused test and confirm RED**

```bash
python3 -m unittest tools.riscv.tests.test_nixos_m7_assets -v
```

Expected: missing admitted asset.

- [x] **Step 3: Port only the standalone reproducer**

Extract `tools/riscv/nixos/m7/scm_repro.c` from the source commit, update it only
for current headers/style, and add a README that states:

```text
Purpose: validate AF_UNIX SCM_RIGHTS and SO_PEERCRED required by nix-daemon.
Provenance: track/nixos commit 8a7396a1fae4dfce21b2d0e19794b83dd7771bd8.
Scope: source fixture only; build/boot integration belongs to the R3 child issue.
```

Do not copy the old M7 rootfs builder, cached Nix closure, or report claims in
this batch.

- [x] **Step 4: Run tests, regenerate the matrix, and commit**

Mark the source commit admitted while keeping its destination as the R3 child
issue. Run the audit and both focused test modules, then commit:

```bash
python3 tools/riscv/nixos_track_audit.py \
  --base b54aad2f89ce529691dd9944dac53bf33c8dcb93 \
  --track 44172c41cb914e510ec45fb8b65441b0fafa4c6b \
  --overrides tools/riscv/nixos/track-admission-overrides.v1.json \
  --output tools/riscv/nixos/track-admission.v1.json
python3 -m unittest tools.riscv.tests.test_nixos_track_audit tools.riscv.tests.test_nixos_m7_assets -v
git diff --check
git add tools/riscv/nixos/m7 tools/riscv/tests/test_nixos_m7_assets.py \
        tools/riscv/nixos/track-admission-overrides.v1.json \
        tools/riscv/nixos/track-admission.v1.json \
        tools/riscv/nixos/TRACK-ADMISSION-M1-report.md
git commit -m 'Admit `nix-daemon` credential reproducer'
```

### Task 6: Run local verification and update issue evidence

**Files:**
- Modify only if checks expose a defect: files from Tasks 2-5
- External: R0, R1-B, R3 child issues

- [x] **Step 1: Run the focused suite**

```bash
python3 -m unittest \
  tools.riscv.tests.test_nixos_track_audit \
  tools.riscv.tests.test_nixos_riscv_preflight \
  tools.riscv.tests.test_nixos_m7_assets -v
```

Expected: all tests pass.

- [x] **Step 2: Run repository checks proportionate to the touched files**

```bash
python3 -m py_compile tools/riscv/nixos_track_audit.py tools/nixos/riscv_preflight.py
python3 tools/nixos/riscv_preflight.py --check; test $? -eq 2
git diff --check origin/main...HEAD
git status --short --branch
```

Expected: Python compiles, preflight returns the documented missing-artifact
status, no whitespace errors, and only planned commits differ from `main`.

- [ ] **Step 3: Post evidence to the child issues**

Comment on R0 with matrix counts and commit hashes, R1-B with preflight output,
and R3 with the admitted SCM reproducer provenance. Do not claim QEMU NixOS boot
success; this slice establishes contracts and auditability only.

- [ ] **Step 4: Request code review before integration**

Run the repository's Asterinas code-review skill in diff mode against
`origin/main...HEAD`, address actionable findings, rerun focused verification,
and prepare a PR. Do not push or merge until the review and local checks pass.
