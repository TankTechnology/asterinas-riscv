# Megrez Debian Persistent Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove a signed Debian 13 `riscv64` ext2 root on Megrez with current Asterinas, including an exact partition-2 inventory, conditional Asterinas-only provisioning, a nonce that survives two physical boots, and a final interactive Bash handoff.

**Architecture:** Add a shell-specific immutable bundle because the existing browser plan cannot represent separate generic-Sv39 and Megrez-Sv48 kernels. Reuse the existing rootfs contract, QEMU two-boot gate, Stage1, partition-2 write policy, compressed installer, `BoardSession`, and shell command classifier. Keep host/QEMU evidence ahead of all board mutation; the board path first records geometry, skips installation when current evidence is sufficient, performs at most one permitted install otherwise, then runs two bounded Bash boots before a separate unbounded operator handoff.

**Tech Stack:** Python 3 standard library, `unittest`, existing Asterinas Debian rootfs modules, U-Boot serial/YMODEM and read-only eMMC protocol, Bash/C Stage1, Rust MMC ktests, QEMU RISC-V generic Sv39, Megrez Sv48, Docker image `asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached`.

---

## File Map and Boundaries

- Create `tools/riscv/megrez_debian_shell_contract.py`: canonical bundle and
  one-open artifact identities only.
- Create `tools/riscv/megrez_debian_shell_evidence.py`: QEMU evidence and the
  physical pre-board permit; inventory and physical results remain owned by
  the board workflow that produces them.
- Create `tools/riscv/megrez_debian_shell_board.py`: physical inventory,
  and conditional-install decisions. It consumes the pure contract and
  existing board/session protocol; it does not build a rootfs.
- Create `tools/riscv/megrez_debian_shell_physical.py`: pure serial phases,
  two-boot orchestration, physical evidence, and final handoff policy.
- Create `tools/riscv/megrez_debian_shell_physical_io.py`: descriptor-pinned
  publication, serial/YMODEM staging, and the concrete physical adapter.
- Create `tools/riscv/megrez_debian_shell.py`: small CLI that dispatches plan,
  check, QEMU, permit, inventory, install-if-needed, gate, and handoff.
- Create `tools/riscv/tests/test_megrez_debian_shell.py`: contract, workflow,
  PTY/subprocess-seam, stale-evidence, and ordering tests.
- Modify `tools/riscv/megrez_board_session.py`: add exact partition geometry
  evidence and an opt-in Debian shell command phase; preserve every existing
  profile.
- Modify `tools/riscv/tests/test_megrez_board_session.py`: parser, split marker,
  shell command, timeout, and recovery regressions.
- Modify `tools/riscv/megrez_debian_install.py`: extract one validated
  `NetworkInstallRequest` core so the old browser workflow and new shell
  workflow share the same installer implementation.
- Modify `tools/riscv/tests/test_megrez_install_workflow.py`: prove old behavior
  remains and the new request performs no automatic retry.
- Modify `Makefile`: add one focused host test target.
- Modify `tools/riscv/debian/rootfs/README.md` and `tools/riscv/README.md`: exact
  operator commands, Sv39/Sv48 split, recovery limits, and evidence locations.
- Create `docs/porting/evidence/2026-08-30-megrez-debian-persistent-shell.md`:
  final hashes, commands, QEMU result, board inventory, optional install, two
  boot results, and final handoff. Create it only after the real evidence exists.

The new modules must stay focused: contract, QEMU evidence, and each physical
workflow module under 500 lines, board inventory/install decisions under 600
lines, and CLI under 300 lines. Stop and split by the boundaries above if any
limit would be exceeded; do not grow another thousand-line orchestrator.

### Task 1: Freeze the dual-platform persistent-shell bundle

**Files:**
- Create: `tools/riscv/megrez_debian_shell_contract.py`
- Create: `tools/riscv/tests/test_megrez_debian_shell.py`
- Modify: `Makefile`

- [ ] **Step 1: Write contract tests before production code**

Add `PersistentShellContractTests` with an in-memory fixture whose canonical
artifact order is exact:

```python
SHELL_ARTIFACT_ORDER = (
    "qemu_kernel",
    "megrez_kernel",
    "stage1",
    "installer_base",
    "qemu_uboot",
    "qemu_dtb",
    "megrez_dtb",
    "root_image",
    "root_manifest",
    "packages_lock",
    "package_checksums",
    "in_release",
)

def test_plan_separates_sv39_qemu_from_sv48_megrez(self) -> None:
    plan = self.valid_plan()
    self.assertEqual(plan.qemu_paging, "sv39")
    self.assertEqual(plan.megrez_paging, "sv48")
    self.assertEqual(plan.smp, 4)
    self.assertEqual(tuple(item.name for item in plan.artifacts), SHELL_ARTIFACT_ORDER)

def test_plan_rejects_swapped_paging_dirty_commit_and_wrong_partition(self) -> None:
    for broken in (
        replace(self.valid_plan(), qemu_paging="sv48"),
        replace(self.valid_plan(), megrez_paging="sv39"),
        replace(self.valid_plan(), git_commit="dirty"),
        replace(self.valid_plan(), partition_start_lba=P2_START_LBA + 1),
        replace(self.valid_plan(), partition_nr_sectors=P2_NR_SECTORS - 1),
    ):
        with self.subTest(broken=broken):
            with self.assertRaises(ShellContractError):
                broken.validate()

def test_plan_json_is_exact_canonical_and_duplicate_key_safe(self) -> None:
    payload = self.valid_plan().canonical_bytes()
    self.assertEqual(PersistentShellPlan.from_bytes(payload).canonical_bytes(), payload)
    with self.assertRaisesRegex(ShellContractError, "duplicate JSON key"):
        PersistentShellPlan.from_bytes(payload.replace(b'"smp":4', b'"smp":4,"smp":4'))
```

Also test unknown/missing fields at the top level and within artifacts,
non-absolute paths, symlinks, wrong size/hash/CRC syntax, duplicate artifact
names, unsafe boot arguments, either recovery interval outside its frozen
value, and the
exact no-write final bootargs.

- [ ] **Step 2: Run the focused test and record the expected RED**

Run:

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_debian_shell.PersistentShellContractTests -v
```

Expected: exit 1 with `ModuleNotFoundError: No module named
'tools.riscv.megrez_debian_shell_contract'`. Existing test modules must not be
imported through a temporary fallback.

- [ ] **Step 3: Implement exact canonical types and one-open artifact identity**

Define these public types and constants:

```python
P2_START_LBA = 0x000F_A022
P2_NR_SECTORS = 0x0080_0000

@dataclass(frozen=True)
class FrozenArtifact:
    name: str
    path: str
    size: int
    sha256: str
    crc32: str

    @classmethod
    def from_path(cls, name: str, path: Path) -> "FrozenArtifact":
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ShellContractError(f"{name} must be a regular file")
            digest = hashlib.sha256()
            crc = 0
            size = 0
            while chunk := os.read(descriptor, 1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
                crc = zlib.crc32(chunk, crc)
            if size != metadata.st_size:
                raise ShellContractError(f"{name} changed while reading")
            return cls(name, str(path.absolute()), size, digest.hexdigest(), f"{crc:08x}")
        finally:
            os.close(descriptor)

@dataclass(frozen=True)
class PersistentShellPlan:
    schema_version: int
    git_commit: str
    artifacts: tuple[FrozenArtifact, ...]
    smp: int
    qemu_paging: str
    megrez_paging: str
    gate_bootargs: str
    final_bootargs: str
    gate_reboot_after: int
    long_operation_reboot_after: int
    partition_start_lba: int
    partition_nr_sectors: int

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()
```

`PersistentShellPlan.validate()` must require schema 1, a 40-lowercase-hex Git
commit, exact artifact order, SMP=4, `sv39` only for QEMU, `sv48` only for
Megrez, exact partition constants, one
`asterinas.reboot_after=180` token in `gate_bootargs`, an exact 180-second gate
recovery interval, an exact 600-second verifier/installer recovery interval,
and no reboot or partition-write token in `final_bootargs`. Both bootarg strings must reject
control bytes, quotes, backticks, `$`, `;`, `&`, and shell metacharacters.

Use duplicate-key-rejecting `json.loads`, exact key sets at every depth, sorted
canonical JSON with a final newline, and narrow `ShellContractError` wrapping
for decode/shape errors while preserving filesystem `OSError`.

- [ ] **Step 4: Bind the bundle to the existing signed rootfs contract**

Add:

```python
def validate_rootfs_identity(plan: PersistentShellPlan) -> RootfsManifest:
    artifacts = plan.artifact_map()
    manifest = load_manifest(Path(artifacts["root_manifest"].path))
    validated = validate_frozen_root(
        Path(artifacts["root_image"].path),
        manifest,
        Path(artifacts["packages_lock"].path),
    )
    checksums = load_package_checksums(Path(artifacts["package_checksums"].path))
    if validated.schema_version != 1 or validated.profile != "minimal-m1":
        raise ShellContractError("persistent shell requires the minimal-m1 root")
    if checksums != validated.downloaded_packages:
        raise ShellContractError("package checksums do not match the manifest")
    if validated.root_image_sha256 != artifacts["root_image"].sha256:
        raise ShellContractError("root image identity differs from the bundle")
    if validated.signed_metadata_sha256 != artifacts["in_release"].sha256:
        raise ShellContractError("retained InRelease differs from the manifest")
    return validated
```

Test a wrong root hash, wrong package sidecar, unsupported root label/profile,
and a fully valid fixture through injected small-file helpers; retain one real
contract round trip against the existing built artifact in the final task.

- [ ] **Step 5: Add the focused Make target and run GREEN checks**

Add:

```make
.PHONY: test_riscv_megrez_debian_shell
test_riscv_megrez_debian_shell:
	python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_megrez_debian_shell -v
```

Run:

```bash
make test_riscv_megrez_debian_shell
python3 -m py_compile tools/riscv/megrez_debian_shell_contract.py \
  tools/riscv/tests/test_megrez_debian_shell.py
ruff check tools/riscv/megrez_debian_shell_contract.py \
  tools/riscv/tests/test_megrez_debian_shell.py
ruff format --check tools/riscv/megrez_debian_shell_contract.py \
  tools/riscv/tests/test_megrez_debian_shell.py
git diff --check
```

Expected: all discovered tests pass and every static command exits 0.

- [ ] **Step 6: Commit the contract**

```bash
git add Makefile tools/riscv/megrez_debian_shell_contract.py \
  tools/riscv/tests/test_megrez_debian_shell.py
git commit -m "test(riscv): define Megrez Debian shell bundle"
```

### Task 2: Bind a fresh QEMU gate to a physical pre-board permit

**Files:**
- Modify: `tools/riscv/megrez_debian_shell_contract.py`
- Create: `tools/riscv/megrez_debian_shell_evidence.py`
- Create: `tools/riscv/megrez_debian_shell.py`
- Modify: `tools/riscv/tests/test_megrez_debian_shell.py`

- [ ] **Step 1: Write RED tests for QEMU evidence and permit issuance**

Add tests that construct a real canonical `result.json` fixture and assert:

```python
def test_qemu_result_requires_two_sv39_smp4_boots_and_exact_inputs(self) -> None:
    evidence = validate_qemu_result(self.plan, self.qemu_result_path)
    self.assertTrue(evidence.passed)
    self.assertEqual(evidence.plan_sha256, self.plan.plan_sha256)
    self.assertEqual(evidence.boot_count, 2)

def test_permit_reopens_artifacts_and_rejects_dirty_or_stale_results(self) -> None:
    with self.assertRaises(ShellPermitError):
        issue_shell_permit(
            replace(self.plan, git_commit="0" * 40),
            self.qemu_evidence_path,
            self.permit_path,
            git_identity=lambda _repo: "1" * 40,
        )
    self.assertFalse(self.permit_path.exists())
```

Reject `passed:false`, the wrong root/kernel/DTB hash, fewer or more than two
QEMU argv entries, any argv without exact `-smp 4`, CPU other than
`rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true`, network/display
devices, KVM, an unclean/different commit, and an output symlink. Verify stale
`permit.json` is removed before validation.

- [ ] **Step 2: Run the focused RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_debian_shell.PersistentShellQemuPermitTests -v
```

Expected: failures for missing `QemuShellEvidence`, `ShellPermit`,
`validate_qemu_result`, and `issue_shell_permit`.

- [ ] **Step 3: Implement exact evidence types**

Add immutable canonical types:

```python
@dataclass(frozen=True)
class QemuShellEvidence:
    schema_version: int
    passed: bool
    reason: str
    plan_sha256: str
    native_result_sha256: str
    boot_count: int
    qemu_kernel_sha256: str
    root_image_sha256: str

@dataclass(frozen=True)
class ShellPermit:
    schema_version: int
    passed: bool
    reason: str
    plan_sha256: str
    qemu_evidence_sha256: str
    git_commit: str
    megrez_kernel_sha256: str
    stage1_crc32: str
    megrez_dtb_crc32: str
    root_image_sha256: str
    gate_bootargs: str
    gate_reboot_after: int
    long_operation_reboot_after: int
```

Both use exact field sets, duplicate-key rejection, canonical JSON, and strict
lowercase hashes. `validate_qemu_result()` must read the native result once,
require `passed is True`, `reason == "pass"`, two boot argv lists, exact input
hashes, full transcript log names, no networking/display, and the generic Sv39
CPU contract. Store the native result hash, not a path-only reference.

- [ ] **Step 4: Implement the QEMU command adapter and permit publication**

Add this pure command constructor to `megrez_debian_shell.py`:

```python
def qemu_gate_argv(plan: PersistentShellPlan, output: Path) -> tuple[str, ...]:
    files = plan.artifact_map()
    return (
        sys.executable, "-m", "tools.riscv.debian.rootfs.rootfs_gate",
        "--kernel", files["qemu_kernel"].path,
        "--uboot", files["qemu_uboot"].path,
        "--dtb", files["qemu_dtb"].path,
        "--stage1-initramfs", files["stage1"].path,
        "--root-image", files["root_image"].path,
        "--root-manifest", files["root_manifest"].path,
        "--packages-lock", files["packages_lock"].path,
        "--package-checksums", files["package_checksums"].path,
        "--output-directory", str(output),
        "--smp", "4",
    )
```

`run_qemu_gate()` invalidates shell-level stale evidence, runs the exact command
once, validates the native result, and atomically publishes
`qemu-evidence.json`. `issue_shell_permit()` reopens and rehashes every bundle
artifact, reruns the rootfs contract, verifies both DTBs contain exactly four
enabled CPUs, requires the current clean commit, and atomically publishes the
permit last.

- [ ] **Step 5: Run GREEN and regression tests**

```bash
make test_riscv_megrez_debian_shell
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_rootfs.DebianRootfsGateProtocolTests \
  tools.riscv.tests.test_debian_rootfs.DebianRootfsGateOrchestrationTests \
  tools.riscv.tests.test_debian_rootfs.DebianRootfsGateBackendSessionTests -v
python3 -m py_compile tools/riscv/megrez_debian_shell.py \
  tools/riscv/megrez_debian_shell_contract.py
ruff check tools/riscv/megrez_debian_shell.py \
  tools/riscv/megrez_debian_shell_contract.py \
  tools/riscv/tests/test_megrez_debian_shell.py
ruff format --check tools/riscv/megrez_debian_shell.py \
  tools/riscv/megrez_debian_shell_contract.py \
  tools/riscv/tests/test_megrez_debian_shell.py
git diff --check
```

Expected: all pass; no QEMU or board process is launched by these tests.

- [ ] **Step 6: Commit QEMU evidence and permit support**

```bash
git add tools/riscv/megrez_debian_shell.py \
  tools/riscv/megrez_debian_shell_contract.py \
  tools/riscv/tests/test_megrez_debian_shell.py
git commit -m "feat(riscv): bind Debian shell preboard evidence"
```

### Task 3: Add read-only partition inventory and install decision

**Files:**
- Modify: `tools/riscv/megrez_board_session.py`
- Modify: `tools/riscv/tests/test_megrez_board_session.py`
- Modify: `tools/riscv/debian/rootfs/megrez_installer.py`
- Modify: `tools/riscv/tests/test_megrez_debian_installer.py`
- Create: `tools/riscv/megrez_debian_shell_board.py`
- Modify: `tools/riscv/tests/test_megrez_debian_shell.py`

- [ ] **Step 1: Write U-Boot geometry parser and ordering RED tests**

Freeze the commands and marker format:

```python
PARTITION_MARKER = re.compile(
    r"__ASTERINAS_PARTITION_(?P<number>[123])__"
    r"start=(?P<start>[0-9a-f]+) size=(?P<size>[0-9a-f]+)"
)

def test_read_partition_geometry_uses_current_uboot_values(self) -> None:
    session = self.fake_session()
    geometry = read_partition_geometry(session)
    self.assertEqual(
        session.commands,
        [
            "mmc dev 1", "mmc rescan",
            "part start mmc 1 1 ast_p1_start", "part size mmc 1 1 ast_p1_size",
            "echo __ASTERINAS_PARTITION_1__start=${ast_p1_start} size=${ast_p1_size}",
            "part start mmc 1 2 ast_p2_start", "part size mmc 1 2 ast_p2_size",
            "echo __ASTERINAS_PARTITION_2__start=${ast_p2_start} size=${ast_p2_size}",
            "part start mmc 1 3 ast_p3_start", "part size mmc 1 3 ast_p3_size",
            "echo __ASTERINAS_PARTITION_3__start=${ast_p3_start} size=${ast_p3_size}",
        ],
    )
    self.assertEqual(geometry[2], (P2_START_LBA, P2_NR_SECTORS))
```

Reject missing, duplicate, decimal, prefixed, zero-sized, out-of-order, or
wrong-p2 output before `booti`. Assert no `saveenv`, `mmc write`, `mw`, `reset`,
or Linux command is sent.

- [ ] **Step 2: Run the geometry RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_board_session.MegrezPartitionInventoryTests -v
```

Expected: failure because `read_partition_geometry` and the exact result type
do not exist.

- [ ] **Step 3: Implement read-only geometry collection**

Add:

```python
@dataclass(frozen=True)
class PartitionGeometry:
    number: int
    start_lba: int
    nr_sectors: int

def read_partition_geometry(session: BoardSession) -> tuple[PartitionGeometry, ...]:
    session.command("mmc dev 1")
    session.command("mmc rescan")
    result = []
    for number in (1, 2, 3):
        session.command(f"part start mmc 1 {number} ast_p{number}_start")
        session.command(f"part size mmc 1 {number} ast_p{number}_size")
        output = session.command(
            f"echo __ASTERINAS_PARTITION_{number}__"
            f"start=${{ast_p{number}_start}} size=${{ast_p{number}_size}}"
        )
        match = PARTITION_MARKER.search(output)
        if match is None or int(match["number"]) != number:
            raise RuntimeError(f"partition {number} geometry is missing")
        result.append(PartitionGeometry(number, int(match["start"], 16), int(match["size"], 16)))
    if (result[1].start_lba, result[1].nr_sectors) != (P2_START_LBA, P2_NR_SECTORS):
        raise RuntimeError("partition 2 does not match the frozen write contract")
    return tuple(result)
```

Call this only when the new shell workflow explicitly requests inventory. Do
not change the default board-session command sequence for existing profiles.

- [ ] **Step 4: Make verifier output distinguish matching from install-needed**

Keep the existing full-hash verifier read-only and add one ordered inventory
marker before hashing:

```sh
emit "DEBIAN_INVENTORY_READY target=/dev/mmcblk0p2 bytes=$size write=disabled"
```

The exact classifier is:

```python
def classify_inventory_log(text: str, expected_sha256: str) -> str:
    if f"DEBIAN_VERIFY_PASS sha256={expected_sha256} bytes=1073741824" in text:
        return "matching"
    if "DEBIAN_VERIFY_FAIL reason=image-hash" in text:
        return "needs-install"
    raise InventoryError("partition root identity was not measurable")
```

Tests must prove `needs-install` is accepted only for the exact image-hash
failure after `DEBIAN_INVENTORY_READY`; missing block device, wrong size,
timeout, panic, reboot before ready, or an unordered marker is
`not-measurable` and cannot authorize installation.

- [ ] **Step 5: Implement `InventoryResult` and the read-only workflow**

Add a canonical result with fields:

```python
@dataclass(frozen=True)
class InventoryResult:
    schema_version: int
    status: str  # matching, needs-install, not-measurable
    reason: str
    plan_sha256: str
    permit_sha256: str
    partitions: tuple[PartitionGeometry, ...]
    expected_root_sha256: str
    install_result_sha256: str | None
    serial_sha256: str | None
```

`run_inventory()` first invalidates old output, validates the bundle/permit,
records all three U-Boot geometries, and checks for a current matching install
result. If that result is bound to the same plan, root SHA, kernel, Stage1, DTB,
and partition geometry, publish `matching` without a full device read. Otherwise
build `--verify-only`, boot it without the hardware watchdog, and classify its
complete bounded transcript. Publish `not-measurable` on any ambiguous failure;
never silently turn it into `needs-install`.

The verifier bootargs are read-only and use the long-operation timer:

```python
def verifier_bootargs(plan: PersistentShellPlan) -> str:
    return " ".join((
        "console=ttyS0",
        "cpu_no_boost_1_6ghz",
        "loglevel=info",
        "init=/init",
        f"asterinas.reboot_after={plan.long_operation_reboot_after}",
    ))
```

They must not contain `asterinas.mmc_write_partition2`, a network selector, or
the diagnostic hardware-watchdog option.

The CLI accepts optional `--install-result` and `--prior-inventory` inputs.
Both are exact held-file identities rather than auto-discovered paths. After an
install, the new inventory must have the same partition 1/2/3 geometry tuple as
the pre-install inventory; any change is `not-measurable` even if the root hash
matches.

- [ ] **Step 6: Run inventory GREEN and regressions**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_board_session.MegrezPartitionInventoryTests \
  tools.riscv.tests.test_megrez_debian_installer.MegrezDebianInstallerTests \
  tools.riscv.tests.test_megrez_debian_shell.PersistentShellInventoryTests -v
python3 -m py_compile tools/riscv/megrez_board_session.py \
  tools/riscv/megrez_debian_shell_board.py \
  tools/riscv/debian/rootfs/megrez_installer.py
ruff check tools/riscv/megrez_board_session.py \
  tools/riscv/megrez_debian_shell_board.py \
  tools/riscv/debian/rootfs/megrez_installer.py \
  tools/riscv/tests/test_megrez_board_session.py \
  tools/riscv/tests/test_megrez_debian_shell.py
ruff format --check tools/riscv/megrez_board_session.py \
  tools/riscv/megrez_debian_shell_board.py \
  tools/riscv/debian/rootfs/megrez_installer.py \
  tools/riscv/tests/test_megrez_board_session.py \
  tools/riscv/tests/test_megrez_debian_shell.py
git diff --check
```

Expected: all pass without opening a real serial device.

- [ ] **Step 7: Commit read-only inventory**

```bash
git add tools/riscv/megrez_board_session.py \
  tools/riscv/debian/rootfs/megrez_installer.py \
  tools/riscv/megrez_debian_shell_board.py \
  tools/riscv/tests/test_megrez_board_session.py \
  tools/riscv/tests/test_megrez_debian_installer.py \
  tools/riscv/tests/test_megrez_debian_shell.py
git commit -m "feat(riscv): inventory Megrez Debian partition"
```

### Task 4: Reuse the protected installer for one conditional write

**Files:**
- Modify: `tools/riscv/megrez_debian_install.py`
- Modify: `tools/riscv/tests/test_megrez_install_workflow.py`
- Modify: `tools/riscv/megrez_debian_shell_board.py`
- Modify: `tools/riscv/tests/test_megrez_debian_shell.py`

- [ ] **Step 1: Write refactor-preservation and one-attempt RED tests**

Add:

```python
def test_shell_install_skips_matching_inventory_without_side_effect(self) -> None:
    result = install_if_needed(self.plan, self.permit, self.matching_inventory, run=self.forbidden)
    self.assertEqual(result.reason, "already-matching")
    self.assertEqual(self.calls, [])

def test_shell_install_runs_exactly_once_for_needs_install(self) -> None:
    result = install_if_needed(self.plan, self.permit, self.needs_install, run=self.success)
    self.assertTrue(result.passed)
    self.assertEqual(self.run_count, 1)

def test_legacy_browser_install_uses_the_same_request_core(self) -> None:
    run_network_install(
        self.browser_plan,
        self.browser_permit_path,
        "/dev/ttyUSB0",
        self.output,
        self.base_cpio,
        self.transport_directory,
        self.root_url,
        artifact_validator=self.artifact_validator,
        git_identity=self.git_identity,
        build_installer=self.build_installer,
        server_factory=self.server_factory,
        run_command=self.run_command,
        repository_root=self.repository,
    )
    self.assertEqual(self.core_requests[0].root_sha256, self.root_sha256)
```

Reject `not-measurable`, stale plan/permit/inventory, changed p2 geometry,
changed root artifact, write bootargs without the exact root SHA, any raw-disk
target, and a second invocation using the same permit after a failed attempt.

- [ ] **Step 2: Run the install RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_install_workflow.MegrezInstallWorkflowTests \
  tools.riscv.tests.test_megrez_debian_shell.PersistentShellInstallTests -v
```

Expected: failures because `NetworkInstallRequest` and `install_if_needed` do
not exist and the legacy function still owns the whole transaction.

- [ ] **Step 3: Extract one shared install request without changing payloads**

Define:

```python
@dataclass(frozen=True)
class NetworkInstallRequest:
    plan_sha256: str
    git_commit: str
    kernel: Path
    kernel_size: int
    kernel_crc32: str
    installer_base: Path
    megrez_dtb_crc32: str
    root_image: Path
    root_sha256: str
    reboot_after: int
    bootargs: str

def _run_network_install_request(
    request: NetworkInstallRequest,
    device: str,
    output: Path,
    transport_directory: Path,
    root_url: str,
    *,
    build_installer: BuildInstaller,
    server_factory: ServerFactory,
    run_command: RunCommand,
) -> StageResult:
    request.validate()
    return _execute_validated_network_install(
        request,
        device,
        output,
        transport_directory,
        root_url,
        build_installer=build_installer,
        server_factory=server_factory,
        run_command=run_command,
    )
```

Move the existing build/server/board-command/publication body into this core
without changing its exact installer initramfs, chunk, HTTP, YMODEM kernel, or
recovery protocol. The old `run_network_install()` continues to validate its
browser plan and permit, creates `NetworkInstallRequest`, and calls the core.
The new shell workflow validates `ShellPermit` and `InventoryResult`, creates
the same request from `megrez_kernel`, `installer_base`, `megrez_dtb`, and `root_image`,
then calls the core.

Replace the existing retry loop with one subprocess invocation. Chunk files
remain resumable for a separately authorized future attempt, but one permit
never launches the board twice.

- [ ] **Step 4: Enforce exact write bootargs and single-use evidence**

The shell request bootargs are exactly:

```python
def installer_bootargs(plan: PersistentShellPlan, root_sha256: str) -> str:
    return " ".join((
        "console=ttyS0",
        "cpu_no_boost_1_6ghz",
        "loglevel=info",
        "init=/init",
        "asterinas.net=eic7700-rj45,10.100.19.200/21",
        "asterinas.neighbor=eic7700-rj45,10.100.19.216,04:7c:16:47:50:4e",
        "asterinas.mmc_write_partition2",
        f"asterinas.debian_install_sha256={root_sha256}",
        f"asterinas.reboot_after={plan.long_operation_reboot_after}",
    ))
```

Publish `attempt.json` before serial launch and refuse a second launch for the
same permit hash. Publish `result.json` only after the exact install marker and
fresh U-Boot recovery epoch. A failed attempt remains failed until Task 3 is
rerun to create a new inventory and a new permit.

- [ ] **Step 5: Run GREEN and preservation checks**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_install_workflow \
  tools.riscv.tests.test_megrez_debian_installer \
  tools.riscv.tests.test_megrez_debian_shell -v
python3 -m py_compile tools/riscv/megrez_debian_install.py \
  tools/riscv/megrez_debian_shell_board.py
ruff check tools/riscv/megrez_debian_install.py \
  tools/riscv/megrez_debian_shell_board.py \
  tools/riscv/tests/test_megrez_install_workflow.py \
  tools/riscv/tests/test_megrez_debian_shell.py
ruff format --check tools/riscv/megrez_debian_install.py \
  tools/riscv/megrez_debian_shell_board.py \
  tools/riscv/tests/test_megrez_install_workflow.py \
  tools/riscv/tests/test_megrez_debian_shell.py
git diff --check
```

Expected: all pass; existing published outputs remain unchanged in every
injected pre-publication failure.

- [ ] **Step 6: Commit the conditional installer adapter**

```bash
git add tools/riscv/megrez_debian_install.py \
  tools/riscv/megrez_debian_shell_board.py \
  tools/riscv/tests/test_megrez_install_workflow.py \
  tools/riscv/tests/test_megrez_debian_shell.py
git commit -m "feat(riscv): condition Megrez Debian installation"
```

### Task 5: Run the existing shell protocol over the physical serial path

**Files:**
- Modify: `tools/riscv/megrez_board_session.py`
- Modify: `tools/riscv/tests/test_megrez_board_session.py`
- Create: `tools/riscv/megrez_debian_shell_physical.py`
- Create: `tools/riscv/megrez_debian_shell_physical_io.py`
- Modify: `tools/riscv/tests/test_megrez_debian_shell.py`

- [ ] **Step 1: Write shell-phase PTY and classification RED tests**

Use a socket pair or PTY fixture that emits split shell-ready and command
markers. Freeze these cases:

```python
def test_debian_shell_boot_uses_gate_protocol_and_normal_reboot(self) -> None:
    result = run_debian_shell_phase(
        self.session,
        boot_number=1,
        nonce="a" * 64,
        debian_release="13.6",
        packages=self.packages,
        deadline=self.deadline,
        reboot=True,
    )
    self.assertTrue(result.passed)
    self.assertEqual(self.session.sent[-1], b"sync; reboot -f\n")

def test_two_boot_gate_resets_serial_epoch_and_reuses_nonce(self) -> None:
    result = run_physical_gate(self.plan, self.permit, self.inventory, operations=self.operations)
    self.assertTrue(result.passed)
    self.assertEqual(self.operations.boot_numbers, [1, 2])
    self.assertEqual(self.operations.nonces, [self.operations.nonces[0]] * 2)
```

Also test out-of-order/stale shell markers, echo-only false positives, command
nonzero status, fatal marker after apparent success, transcript byte cap,
deadline across all commands, boot-1 recovery before boot 2, wrong nonce in
boot 2, second-probe absence, first signal cleanup, and stale `passed:true`
invalidation before opening serial.

- [ ] **Step 2: Run the physical protocol RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_board_session.MegrezDebianShellPhaseTests \
  tools.riscv.tests.test_megrez_debian_shell.PersistentShellPhysicalGateTests -v
```

Expected: missing `run_debian_shell_phase`, `run_physical_gate`, and physical
result types.

- [ ] **Step 3: Extend `BoardSession` with an opt-in Debian shell phase**

Add `debian-shell-gate` and `debian-shell-handoff` final profiles, both using
`__DEBIAN_ROOTFS_SHELL_READY__`. Immediately after `boot_loaded_artifacts()`
returns the current kernel-enter evidence, gate mode transfers ownership of the
still-unconsumed post-boot serial stream to the existing pure protocol:

```python
def run_debian_shell_phase(
    session: BoardSession,
    *,
    boot_number: int,
    nonce: str,
    debian_release: str,
    packages: tuple[tuple[str, str], ...],
    deadline: float,
    reboot: bool,
) -> GateResult:
    console = SerialConsole(session.fd, max_bytes=8 * 1024 * 1024)
    console.wait_for(b"__DEBIAN_ROOTFS_SHELL_READY__", deadline)
    commands = shell_commands(boot_number=boot_number, nonce=nonce)
    for command in commands:
        start = console.checkpoint()
        console.send(command.payload.encode() + b"\n", deadline)
        console.wait_for(command.end_marker.encode(), deadline, start=start)
    transcript = console.transcript
    result = classify_boot(
        transcript,
        commands,
        boot_number=boot_number,
        expected_debian_release=debian_release,
        expected_packages=packages,
        expected_nonce=nonce,
    )
    session._log(transcript.decode(errors="replace"))
    if result.passed and reboot:
        console.send(b"sync; reboot -f\n", deadline)
    return result
```

Do not run the ordinary milestone loop before this function, because that
would consume the shell-ready marker. Use one absolute deadline for
shell-ready, all commands, and reboot request. The caller must validate a fresh
OpenSBI → U-Boot → prompt epoch after each gate boot. Handoff mode uses the
ordinary marker loop, stops after the ready marker, closes only the host
descriptor, and does not send `reboot`, `poweroff`, or any write command.

- [ ] **Step 4: Implement two physical boots with exact current transfers**

`run_physical_gate()` must:

1. validate plan, permit, successful/matching inventory, and unchanged
   artifacts before serial open;
2. invalidate `boot1.serial.log`, `boot2.serial.log`, and `result.json`;
3. stage one verified LZMA-alone kernel and Stage1 for serial YMODEM, then load
   the frozen DTB read-only from eMMC partition 1 and verify its exact CRC32;
4. invoke the board-session adapter for boot 1 with `gate_bootargs`, boot number
   1, and a new `secrets.token_hex(32)` nonce;
5. require a fresh recovery epoch;
6. invoke it again for boot 2 with the same nonce;
7. reclassify both complete logs, redact nonce plaintext, and publish the
   result last.

The current U-Boot probes GMAC `0x50400000`, but Asterinas reaches the physical
RJ45 through the other GMAC. Therefore inventory, gate, and handoff must not
depend on U-Boot TFTP. YMODEM changes no host address or route, and the eMMC
DTB load is read-only. The separate installer may use the network only after
Asterinas has selected and verified its RJ45 path.

- [ ] **Step 5: Define and publish the physical result**

Add:

```python
@dataclass(frozen=True)
class PhysicalShellResult:
    schema_version: int
    passed: bool
    reason: str
    plan_sha256: str
    permit_sha256: str
    inventory_sha256: str
    nonce_sha256: str
    boot1_serial_sha256: str
    boot2_serial_sha256: str
    boot1_recovered: bool
    boot2_recovered: bool
```

Publish `passed:true` only after both boot logs have been completely scanned,
both recovery epochs were seen, the nonce matched, and the output directory
was fsynced. Any setup/boot/command/recovery/classification/signal error publishes
or returns a stable failure without leaving stale success.

- [ ] **Step 6: Run GREEN, PTY, and leak checks**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_board_session.MegrezDebianShellPhaseTests \
  tools.riscv.tests.test_megrez_debian_shell.PersistentShellPhysicalGateTests -v
make test_riscv_megrez_debian_shell
python3 -m py_compile tools/riscv/megrez_board_session.py \
  tools/riscv/megrez_debian_shell_physical.py \
  tools/riscv/megrez_debian_shell_physical_io.py
ruff check tools/riscv/megrez_board_session.py \
  tools/riscv/megrez_debian_shell_physical.py \
  tools/riscv/megrez_debian_shell_physical_io.py \
  tools/riscv/tests/test_megrez_board_session.py \
  tools/riscv/tests/test_megrez_debian_shell.py
ruff format --check tools/riscv/megrez_board_session.py \
  tools/riscv/megrez_debian_shell_physical.py \
  tools/riscv/megrez_debian_shell_physical_io.py \
  tools/riscv/tests/test_megrez_board_session.py \
  tools/riscv/tests/test_megrez_debian_shell.py
git diff --check
pgrep -af 'dnsmasq.*asterinas-megrez-tftp|megrez_board_session.py.*debian-shell' \
  && exit 1 || true
```

Expected: all tests/static checks pass and the final leak check prints nothing.

- [ ] **Step 7: Commit the physical two-boot gate**

```bash
git add tools/riscv/megrez_board_session.py \
  tools/riscv/megrez_debian_shell_physical.py \
  tools/riscv/megrez_debian_shell_physical_io.py \
  tools/riscv/tests/test_megrez_board_session.py \
  tools/riscv/tests/test_megrez_debian_shell.py
git commit -m "feat(riscv): gate Megrez Debian persistence"
```

### Task 6: Add the operator CLI and executable documentation

**Files:**
- Modify: `tools/riscv/megrez_debian_shell.py`
- Modify: `tools/riscv/tests/test_megrez_debian_shell.py`
- Modify: `tools/riscv/debian/rootfs/README.md`
- Modify: `tools/riscv/README.md`

- [ ] **Step 1: Write strict CLI and documentation RED tests**

The parser must expose exactly:

```text
plan
check
qemu
permit
inventory
install-if-needed
gate
handoff
```

Tests require absolute non-symlink inputs, an output below repository `target`,
positive finite deadlines, safe interface/device strings, `--yes` for every
physical subcommand, and pre-side-effect validation. `handoff` must reject a
missing/nonpassing physical result. Add documentation assertions for the exact
Sv39/Sv48 build commands, read-only-first sequence, no-Linux rule, no hardware
watchdog on hash/install, and final serial attach command.

- [ ] **Step 2: Run the CLI RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_debian_shell.PersistentShellCliTests \
  tools.riscv.tests.test_megrez_debian_shell.PersistentShellDocumentationTests -v
```

Expected: parser/subcommands and documentation assertions fail.

- [ ] **Step 3: Implement the small dispatcher**

The dispatcher must perform no business logic beyond loading exact canonical
objects and calling the Task 2–5 functions:

```python
def main(arguments: Sequence[str] | None = None) -> int:
    parser = _parser()
    values = parser.parse_args(arguments)
    try:
        if values.command == "plan":
            create_plan(values)
        elif values.command == "check":
            check_plan(values.plan)
        elif values.command == "qemu":
            run_qemu_gate(load_plan(values.plan), values.output)
        elif values.command == "permit":
            issue_shell_permit(load_plan(values.plan), values.qemu_evidence, values.output)
        elif values.command == "inventory":
            run_inventory_from_cli(values)
        elif values.command == "install-if-needed":
            install_from_cli(values)
        elif values.command == "gate":
            run_gate_from_cli(values)
        else:
            handoff_from_cli(values)
    except (ShellContractError, ShellWorkflowError, OSError) as error:
        print(f"megrez-debian-shell: {error}", file=sys.stderr)
        return 2
    return 0
```

`handoff` validates the passing physical result and boots the same frozen
Megrez kernel, DTB, Stage1, and root using `final_bootargs`. It waits only for
`__DEBIAN_ROOTFS_SHELL_READY__`, then releases the serial descriptor without
rebooting. Print the exact follow-up command:

```text
picocom --baud 115200 --flow n --parity n --databits 8 /dev/ttyUSB0
```

- [ ] **Step 4: Document exact operator stages and recovery policy**

Add one section headed `Megrez persistent Debian shell` to both READMEs. It
must state:

- generic QEMU uses `FEATURES=riscv_sv39_mode`; Megrez uses the default Sv48
  build and they are separate artifacts;
- `inventory` is read-only and runs before `install-if-needed`;
- matching inventory skips installation;
- installation is Asterinas-only and may write only `/dev/mmcblk0p2`;
- the short EIC7700X watchdog is forbidden during full hash/install;
- `gate` performs two bounded boots and `handoff` is allowed only after pass;
- systemd, network, and desktop are the next milestones, not claims of this one.

Include commands using a stable run directory:

```bash
RUN="$PWD/target/megrez-debian-shell/$(git rev-parse --short=12 HEAD)"
python3 -m tools.riscv.megrez_debian_shell check "$RUN/plan.json"
sudo -E python3 -m tools.riscv.megrez_debian_shell qemu \
  "$RUN/plan.json" --output "$RUN/qemu"
python3 -m tools.riscv.megrez_debian_shell permit \
  "$RUN/plan.json" --qemu-evidence "$RUN/qemu/qemu-evidence.json" \
  --output "$RUN/permit.json"
sudo -E python3 -m tools.riscv.megrez_debian_shell inventory \
  "$RUN/plan.json" /dev/ttyUSB0 --permit "$RUN/permit.json" \
  --output "$RUN/inventory-before" --yes
sudo -E python3 -m tools.riscv.megrez_debian_shell install-if-needed \
  "$RUN/plan.json" /dev/ttyUSB0 --permit "$RUN/permit.json" \
  --inventory "$RUN/inventory-before/result.json" --output "$RUN/install" --yes
if jq -e '.status == "needs-install"' "$RUN/inventory-before/result.json"; then
  sudo -E python3 -m tools.riscv.megrez_debian_shell inventory \
    "$RUN/plan.json" /dev/ttyUSB0 --permit "$RUN/permit.json" \
    --prior-inventory "$RUN/inventory-before/result.json" \
    --install-result "$RUN/install/result.json" \
    --output "$RUN/inventory-after" --yes
  cp "$RUN/inventory-after/result.json" "$RUN/inventory-current.json"
else
  cp "$RUN/inventory-before/result.json" "$RUN/inventory-current.json"
fi
sudo -E python3 -m tools.riscv.megrez_debian_shell gate \
  "$RUN/plan.json" /dev/ttyUSB0 --permit "$RUN/permit.json" \
  --inventory "$RUN/inventory-current.json" --output "$RUN/physical" \
  --host-interface enp12s0 --yes
sudo -E python3 -m tools.riscv.megrez_debian_shell handoff \
  "$RUN/plan.json" /dev/ttyUSB0 --result "$RUN/physical/result.json" \
  --host-interface enp12s0 --yes
```

- [ ] **Step 5: Run the complete host gate once**

```bash
make test_riscv_megrez_debian_shell
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_board_session \
  tools.riscv.tests.test_megrez_debian_installer \
  tools.riscv.tests.test_megrez_install_workflow \
  tools.riscv.tests.test_debian_rootfs -v
python3 -m py_compile tools/riscv/megrez_debian_shell.py \
  tools/riscv/megrez_debian_shell_board.py \
  tools/riscv/megrez_debian_shell_contract.py
ruff check tools/riscv/megrez_debian_shell.py \
  tools/riscv/megrez_debian_shell_board.py \
  tools/riscv/megrez_debian_shell_contract.py \
  tools/riscv/megrez_board_session.py \
  tools/riscv/megrez_debian_install.py \
  tools/riscv/tests/test_megrez_debian_shell.py
ruff format --check tools/riscv/megrez_debian_shell.py \
  tools/riscv/megrez_debian_shell_board.py \
  tools/riscv/megrez_debian_shell_contract.py \
  tools/riscv/megrez_board_session.py \
  tools/riscv/megrez_debian_install.py \
  tools/riscv/tests/test_megrez_debian_shell.py
git diff --check
```

Expected: all pass. Do not repeat already-passing full host suites during the
physical evidence task unless code changes after this gate.

- [ ] **Step 6: Commit CLI and documentation**

```bash
git add tools/riscv/megrez_debian_shell.py \
  tools/riscv/tests/test_megrez_debian_shell.py \
  tools/riscv/debian/rootfs/README.md tools/riscv/README.md
git commit -m "docs(riscv): operate Megrez Debian shell gate"
```

### Task 7: Build current artifacts, run QEMU, then perform the bounded board gate

**Files:**
- Create: `docs/porting/evidence/2026-08-30-megrez-debian-persistent-shell.md`
- Modify only if a focused reproducer fails: files owned by Tasks 1–6

- [ ] **Step 1: Preflight local tools, proxy, serial ownership, and artifacts**

Run read-only checks first:

```bash
git status --short
docker image inspect asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  --format '{{.Id}}'
curl -I --max-time 10 --proxy http://127.0.0.1:17892 \
  https://static.rust-lang.org/
command -v qemu-system-riscv64 fdtget dtc picocom sb
fuser -v /dev/ttyUSB0 || true
```

Expected: clean tracked worktree, pinned image present, proxy HTTP response,
all tools found, and no unexplained serial owner. Stop a stale serial client
cleanly before proceeding; do not request a physical reset merely to acquire
the port.

- [ ] **Step 2: Build separate current Sv39 and Sv48 kernels**

Use one named container at a time, explicit proxy inheritance, and remove it on
exit. Build the QEMU kernel first and copy it before rebuilding the shared
target as Sv48:

```bash
RUN="$PWD/target/megrez-debian-shell/$(git rev-parse --short=12 HEAD)"
mkdir -p "$RUN/artifacts"

docker run --rm --privileged --network=host -v /dev:/dev \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  -e HTTP_PROXY=http://127.0.0.1:17892 \
  -e HTTPS_PROXY=http://127.0.0.1:17892 \
  -e http_proxy=http://127.0.0.1:17892 \
  -e https_proxy=http://127.0.0.1:17892 \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  bash -lc 'make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode'
cp target/osdk/aster-kernel/aster-kernel-osdk-bin.Image \
  "$RUN/artifacts/asterinas-qemu-sv39.booti"

docker run --rm --privileged --network=host -v /dev:/dev \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  -e HTTP_PROXY=http://127.0.0.1:17892 \
  -e HTTPS_PROXY=http://127.0.0.1:17892 \
  -e http_proxy=http://127.0.0.1:17892 \
  -e https_proxy=http://127.0.0.1:17892 \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  bash -lc 'make kernel TARGET_ARCH=riscv64 SMP=4'
cp target/osdk/aster-kernel/aster-kernel-osdk-bin.Image \
  "$RUN/artifacts/asterinas-megrez-sv48.booti"
```

If the actual OSDK output path differs, discover it with
`find target/osdk -type f -name 'aster-kernel-osdk-bin.*' -print` and record the
resolved path in evidence before copying; never copy an older target by guess.

- [ ] **Step 3: Build Stage1 and generate both four-hart DTBs**

```bash
tools/riscv/debian/rootfs/build_stage1.sh "$RUN/artifacts/stage1.cpio"
gzip -dc test/initramfs/build/initramfs.cpio.gz \
  >"$RUN/artifacts/installer-base.cpio"
QEMU_OUT="$PWD/target/qemu-uboot/debian-shell-$(git rev-parse --short=12 HEAD)"
ASTERINAS_RISCV_BOOTI="$RUN/artifacts/asterinas-qemu-sv39.booti" \
ASTERINAS_INITRAMFS="$RUN/artifacts/stage1.cpio" \
QEMU_UBOOT_PROFILE=generic-sv39-ltp-smp4 \
QEMU_UBOOT_OUT_DIR="$QEMU_OUT" \
QEMU_UBOOT_BUILD_DIR="$PWD/target/qemu-uboot/cache/u-boot-build" \
  tools/riscv/prepare_qemu_uboot_booti.sh prepare
cp "$QEMU_OUT/qemu-virt.dtb" "$RUN/artifacts/qemu-virt-smp4.dtb"
cp target/qemu-uboot/cache/u-boot-build/u-boot "$RUN/artifacts/u-boot"
cp target/megrez-sdhci-sdma-4960dc2d0/artifacts/eic7700-milkv-megrez.dtb \
  "$RUN/artifacts/eic7700-milkv-megrez.dtb"
fdtget -l "$RUN/artifacts/qemu-virt-smp4.dtb" /cpus
fdtget -l "$RUN/artifacts/eic7700-milkv-megrez.dtb" /cpus
```

Before accepting the copied board DTB, re-run the current Megrez contract and
record why its bytes remain board-authoritative; if current source generates a
different validated DTB, use the current generated file and record its hash.

- [ ] **Step 4: Validate the existing signed root and create the plan**

Use the signed base persistent-root artifact, not a desktop/systemd label:

```bash
ROOT="$PWD/target/debian-riscv/rootfs"
python3 -m tools.riscv.debian.rootfs.contract verify \
  --image "$ROOT/debian-root.ext2" \
  --manifest "$ROOT/rootfs-manifest.json" \
  --packages-lock "$ROOT/packages.lock"
python3 -m tools.riscv.debian.rootfs.megrez_installer \
  --base-cpio "$RUN/artifacts/installer-base.cpio" \
  --root-image "$ROOT/debian-root.ext2" \
  --manifest "$ROOT/rootfs-manifest.json" \
  --packages-lock "$ROOT/packages.lock" --verify-only \
  --output "$RUN/artifacts/inventory-verifier-smoke.cpio"

python3 -m tools.riscv.megrez_debian_shell plan \
  --qemu-kernel "$RUN/artifacts/asterinas-qemu-sv39.booti" \
  --megrez-kernel "$RUN/artifacts/asterinas-megrez-sv48.booti" \
  --stage1 "$RUN/artifacts/stage1.cpio" \
  --installer-base "$RUN/artifacts/installer-base.cpio" \
  --qemu-uboot "$RUN/artifacts/u-boot" \
  --qemu-dtb "$RUN/artifacts/qemu-virt-smp4.dtb" \
  --megrez-dtb "$RUN/artifacts/eic7700-milkv-megrez.dtb" \
  --root-image "$ROOT/debian-root.ext2" \
  --root-manifest "$ROOT/rootfs-manifest.json" \
  --packages-lock "$ROOT/packages.lock" \
  --package-checksums "$ROOT/source-metadata/package-checksums" \
  --in-release "$ROOT/source-metadata/InRelease" \
  --gate-reboot-after 180 --long-operation-reboot-after 600 \
  --output "$RUN/plan.json"
```

If `target/debian-riscv/rootfs` is absent, run the existing signed rootfs
builder once as documented in `tools/riscv/debian/rootfs/README.md`; do not
substitute the present `desktop-m5-*` images because their label/profile is a
different contract.

- [ ] **Step 5: Run the fresh QEMU gate and issue the permit**

```bash
sudo -E python3 -m tools.riscv.megrez_debian_shell qemu \
  "$RUN/plan.json" --output "$RUN/qemu"
python3 -m tools.riscv.megrez_debian_shell permit \
  "$RUN/plan.json" --qemu-evidence "$RUN/qemu/qemu-evidence.json" \
  --output "$RUN/permit.json"
jq -e '.passed == true and .reason == "pass"' "$RUN/qemu/result.json"
```

Expected: QEMU starts exactly twice with generic Sv39/SMP=4, passes all Debian
identity/persistence checks, and leaves no QEMU process or Unix monitor socket.
Do not monitor remote CI.

- [ ] **Step 6: Run one read-only physical inventory**

```bash
sudo -E python3 -m tools.riscv.megrez_debian_shell inventory \
  "$RUN/plan.json" /dev/ttyUSB0 --permit "$RUN/permit.json" \
  --output "$RUN/inventory-before" --yes
jq -e '.status == "matching" or .status == "needs-install"' \
  "$RUN/inventory-before/result.json"
```

Expected: exact p2 start/size, all three partition geometries recorded, and a
stable status. `not-measurable` stops the plan and triggers host/log diagnosis;
it does not authorize installation or an automatic retry. Do not arm the
hardware watchdog for this full-root read.

- [ ] **Step 7: Install only when the inventory requires it**

```bash
sudo -E python3 -m tools.riscv.megrez_debian_shell install-if-needed \
  "$RUN/plan.json" /dev/ttyUSB0 --permit "$RUN/permit.json" \
  --inventory "$RUN/inventory-before/result.json" --output "$RUN/install" --yes
jq -e '.passed == true' "$RUN/install/result.json"

if jq -e '.status == "needs-install"' "$RUN/inventory-before/result.json"; then
  sudo -E python3 -m tools.riscv.megrez_debian_shell inventory \
    "$RUN/plan.json" /dev/ttyUSB0 --permit "$RUN/permit.json" \
    --prior-inventory "$RUN/inventory-before/result.json" \
    --install-result "$RUN/install/result.json" \
    --output "$RUN/inventory-after" --yes
  cp "$RUN/inventory-after/result.json" "$RUN/inventory-current.json"
else
  cp "$RUN/inventory-before/result.json" "$RUN/inventory-current.json"
fi
jq -e '.status == "matching"' "$RUN/inventory-current.json"
```

Expected: matching inventory produces `already-matching` without a board write;
`needs-install` performs one Asterinas-only install and recovers to U-Boot.
After a real install, the command reruns inventory, compares all three
partition geometries with the pre-write result, and selects only the new
matching result. It never reuses the pre-write inventory for the physical gate.

- [ ] **Step 8: Run the two-boot physical persistence gate**

```bash
sudo -E python3 -m tools.riscv.megrez_debian_shell gate \
  "$RUN/plan.json" /dev/ttyUSB0 --permit "$RUN/permit.json" \
  --inventory "$RUN/inventory-current.json" --output "$RUN/physical" \
  --host-interface enp12s0 --yes
jq -e '.passed == true and .boot1_recovered == true and .boot2_recovered == true' \
  "$RUN/physical/result.json"
```

Expected: two fresh firmware epochs, exact Debian/package/ext2 identities, same
nonce across boots, second-probe success, redacted logs, and no fatal marker.
If it fails, preserve the first result and diagnose it before another board
attempt; do not ask for repeated physical resets as a test loop.

- [ ] **Step 9: Perform the final interactive handoff**

```bash
sudo -E python3 -m tools.riscv.megrez_debian_shell handoff \
  "$RUN/plan.json" /dev/ttyUSB0 --result "$RUN/physical/result.json" \
  --host-interface enp12s0 --yes
picocom --baud 115200 --flow n --parity n --databits 8 /dev/ttyUSB0
```

Expected: Bash prints `__DEBIAN_ROOTFS_SHELL_READY__`, remains running, and no
diagnostic watchdog or `asterinas.reboot_after` forces a reset.

- [ ] **Step 10: Write and verify the evidence report**

Record exact commit, every artifact size/SHA-256/CRC32, QEMU argv and result,
partition geometries, whether installation was skipped or executed, both boot
log hashes, nonce hash, recovery epochs, final handoff marker, elapsed times,
and known non-goals. The report must explicitly say QEMU was Sv39 and Megrez
was Sv48.

Run final cheap checks:

```bash
git diff --check
git status --short
jq -e '.passed == true' "$RUN/qemu/result.json"
jq -e '.status == "matching"' "$RUN/inventory-current.json"
jq -e '.passed == true' "$RUN/physical/result.json"
pgrep -af 'qemu-system-riscv64|dnsmasq.*tftp-root|megrez_board_session.py' \
  && exit 1 || true
```

Expected: evidence is internally consistent, no helper process remains, and
only the new report is uncommitted.

- [ ] **Step 11: Commit the evidence report**

```bash
git add docs/porting/evidence/2026-08-30-megrez-debian-persistent-shell.md
git commit -m "docs(riscv): record Megrez Debian shell pass"
```

Do not claim systemd, network, browser, or desktop completion in this commit.
