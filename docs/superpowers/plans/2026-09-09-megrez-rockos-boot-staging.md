# Megrez RockOS Boot-Staging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Boot the existing Megrez physical-graphics plan from immutable files staged on eMMC partition 1, without retransferring boot artifacts over YMODEM or touching the installed partition-2 Debian root.

**Architecture:** Add one opt-in MMC filename mapping to the existing physical-graphics adapter. When all three MMC names are supplied, the adapter delegates to `BoardSession.load_artifact`, then checks both the returned byte count and the plan CRC32 before booting; without the mapping, the existing RAM/YMODEM recovery path remains unchanged. RockOS is used once, outside the acceptance result, to place the kernel and initramfs under unique `/boot` names; the already installed DTB path and partition-2 root are reused.

**Tech Stack:** Python 3, `argparse`, `unittest`, U-Boot `ext4load`/`crc32`, RockOS shell, the persistent Asterinas development container.

---

### Task 1: Define the opt-in MMC command-line contract

**Files:**
- Modify: `tools/riscv/megrez_physical_graphics.py:20-45,1759-1800`
- Test: `tools/riscv/tests/test_megrez_physical_graphics.py:720-760`

- [ ] **Step 1: Write failing parser tests for the complete, incomplete, and unsafe mappings**

Add a `PhysicalCliTests` helper that constructs the required common arguments, then assert that all three names survive parsing and that partial or shell-unsafe mappings fail closed:

```python
class PhysicalCliTests(unittest.TestCase):
    BASE_ARGUMENTS = (
        "/dev/serial/by-id/test",
        "--plan",
        "/tmp/plan.json",
        "--output-directory",
        "/tmp/evidence",
        "--hdmi-capture",
        "/tmp/capture.png",
    )

    def test_cli_accepts_one_complete_mmc_artifact_mapping(self) -> None:
        gate = load_gate(self)
        values = gate.parse_args(
            self.BASE_ARGUMENTS
            + (
                "--mmc-kernel",
                "asterinas-a1b2c3d4-34bc1cc0.Image",
                "--mmc-initramfs",
                "asterinas-a1b2c3d4-34bc1cc0-stage1.cpio",
                "--mmc-dtb",
                "dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb",
            )
        )
        self.assertEqual(values.mmc_kernel, "asterinas-a1b2c3d4-34bc1cc0.Image")
        self.assertEqual(
            values.mmc_initramfs,
            "asterinas-a1b2c3d4-34bc1cc0-stage1.cpio",
        )
        self.assertEqual(
            values.mmc_dtb,
            "dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb",
        )

    def test_cli_rejects_partial_or_unsafe_mmc_artifact_mapping(self) -> None:
        gate = load_gate(self)
        variants = (
            ("--mmc-kernel", "kernel.Image"),
            (
                "--mmc-kernel",
                "kernel.Image",
                "--mmc-initramfs",
                "stage1.cpio",
            ),
            (
                "--mmc-kernel",
                "kernel;reset",
                "--mmc-initramfs",
                "stage1.cpio",
                "--mmc-dtb",
                "board.dtb",
            ),
        )
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(SystemExit):
                gate.parse_args(self.BASE_ARGUMENTS + variant)
```

- [ ] **Step 2: Run the parser tests and verify they fail before implementation**

Run:

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_physical_graphics.PhysicalCliTests -v
```

Expected: FAIL because `--mmc-kernel`, `--mmc-initramfs`, and `--mmc-dtb` are not recognized.

- [ ] **Step 3: Add safe arguments and enforce an all-or-none contract**

Import the established `safe_artifact_name` validator from `megrez_board_session`, add the three options, and reject partial mappings after `parse_args`:

```python
from tools.riscv.megrez_board_session import (
    MEGREZ_FRAMEBUFFER,
    MEGREZ_USB_HOST_COMMAND,
    BoardSession,
    open_serial,
    safe_artifact_name,
    validate_debug_console_readiness,
    validate_recovery_epoch,
)

# In parse_args:
parser.add_argument("--mmc-kernel", type=safe_artifact_name)
parser.add_argument("--mmc-initramfs", type=safe_artifact_name)
parser.add_argument("--mmc-dtb", type=safe_artifact_name)
values = parser.parse_args(arguments)
mmc_names = (values.mmc_kernel, values.mmc_initramfs, values.mmc_dtb)
if any(name is not None for name in mmc_names) and not all(
    name is not None for name in mmc_names
):
    parser.error(
        "--mmc-kernel, --mmc-initramfs, and --mmc-dtb must be supplied together"
    )
return values
```

- [ ] **Step 4: Run the parser tests and verify they pass**

Run the Step 2 command.

Expected: 2 tests PASS; `argparse` diagnostics are emitted only for the intentionally invalid cases.

- [ ] **Step 5: Commit the command-line contract**

```bash
git add tools/riscv/megrez_physical_graphics.py \
  tools/riscv/tests/test_megrez_physical_graphics.py
git commit -m "feat(riscv): select staged MMC graphics artifacts"
```

### Task 2: Load and identity-check the staged artifacts

**Files:**
- Modify: `tools/riscv/megrez_physical_graphics.py:1210-1360,1787-1794`
- Test: `tools/riscv/tests/test_megrez_physical_graphics.py`

- [ ] **Step 1: Write a failing unit test proving the MMC path never constructs `BoardTransport`**

Use a fake session and three plan identities. Require exact filename, load address, CRC32, ordering, and transport evidence:

```python
def test_real_operations_load_complete_mmc_mapping_without_board_transport(
    self,
) -> None:
    gate = load_gate(self)
    artifacts = tuple(
        SimpleNamespace(
            name=name,
            load_address=address,
            size=size,
            crc32=crc32,
        )
        for name, address, size, crc32 in (
            ("kernel", 0x80200000, 101, "11111111"),
            ("initramfs", 0x83000000, 202, "22222222"),
            ("megrez_dtb", 0xF0000000, 303, "33333333"),
        )
    )
    names = {
        "kernel": "asterinas-a1b2c3d4-34bc1cc0.Image",
        "initramfs": "asterinas-a1b2c3d4-34bc1cc0-stage1.cpio",
        "megrez_dtb": (
            "dtbs/linux-image-6.6.87-win2030/eswin/"
            "eic7700-milkv-megrez.dtb"
        ),
    }
    session = mock.Mock()
    session.load_artifact.side_effect = (101, 202, 303)
    operations = gate.RealPhysicalGraphicsOperations(
        SimpleNamespace(artifacts=artifacts),
        "/dev/null",
        Path("/unused"),
        Path("/unused-capture"),
        mmc_artifacts=names,
    )
    operations._session = session
    operations._fd = 41

    with mock.patch.object(
        gate,
        "BoardTransport",
        side_effect=AssertionError("YMODEM path must not be constructed"),
    ):
        outcomes = operations.ensure_artifacts(
            SimpleNamespace(artifacts=artifacts), 300
        )

    self.assertEqual(outcomes, ("kernel:mmc", "initramfs:mmc", "megrez_dtb:mmc"))
    self.assertEqual(
        session.load_artifact.call_args_list,
        [
            mock.call("kernel", names["kernel"], 0x80200000, "11111111"),
            mock.call("initramfs", names["initramfs"], 0x83000000, "22222222"),
            mock.call("megrez_dtb", names["megrez_dtb"], 0xF0000000, "33333333"),
        ],
    )
```

- [ ] **Step 2: Write a failing unit test for an MMC byte-count mismatch**

Reuse one minimal kernel identity, make `load_artifact` return a different size, and require a fail-closed error:

```python
def test_real_operations_reject_mmc_size_mismatch(self) -> None:
    gate = load_gate(self)
    artifacts = (
        SimpleNamespace(
            name="kernel",
            load_address=0x80200000,
            size=101,
            crc32="11111111",
        ),
    )
    operations = object.__new__(gate.RealPhysicalGraphicsOperations)
    operations._session = mock.Mock()
    operations._session.load_artifact.return_value = 100
    operations._fd = 41
    operations._mmc_artifacts = {"kernel": "kernel.Image"}
    with self.assertRaisesRegex(gate.HostGateError, "size mismatch"):
        operations.ensure_artifacts(SimpleNamespace(artifacts=artifacts), 300)
```

- [ ] **Step 3: Run both tests and verify they fail before implementation**

Run:

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_physical_graphics.PhysicalMmcArtifactTests -v
```

Expected: FAIL because the constructor has no `mmc_artifacts` parameter and `ensure_artifacts` still constructs `BoardTransport`.

- [ ] **Step 4: Add the minimal MMC adapter and exact size check**

Accept an immutable copy of the optional mapping in the constructor, select it before constructing `BoardTransport`, and compare `BoardSession.load_artifact`'s returned size to the plan:

```python
# Constructor keyword and state:
mmc_artifacts: Mapping[str, str] | None = None,
self._mmc_artifacts = None if mmc_artifacts is None else dict(mmc_artifacts)

# At the start of ensure_artifacts, after identities are built:
if self._mmc_artifacts is not None:
    outcomes = []
    for name in BOARD_ARTIFACT_NAMES:
        identity = identities[name]
        actual_size = session.load_artifact(
            name,
            self._mmc_artifacts[name],
            identity.load_address,
            identity.crc32,
        )
        if actual_size != identity.size:
            raise HostGateError(
                f"{name}: MMC size mismatch: expected {identity.size}, "
                f"got {actual_size}"
            )
        outcomes.append(f"{name}:mmc")
    return tuple(outcomes)
```

Import `Mapping` from `collections.abc`. In `main`, construct the mapping only when `values.mmc_kernel` is not `None` and pass it to `RealPhysicalGraphicsOperations`:

```python
mmc_artifacts = (
    None
    if values.mmc_kernel is None
    else {
        "kernel": values.mmc_kernel,
        "initramfs": values.mmc_initramfs,
        "megrez_dtb": values.mmc_dtb,
    }
)
operations = RealPhysicalGraphicsOperations(
    plan,
    values.device,
    values.output_directory,
    values.hdmi_capture,
    mmc_artifacts=mmc_artifacts,
)
```

- [ ] **Step 5: Run focused and full host-tool tests**

Run:

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_physical_graphics.PhysicalMmcArtifactTests -v
python3 -m unittest tools.riscv.tests.test_megrez_physical_graphics -v
python3 -m unittest tools.riscv.tests.test_megrez_board_session -v
```

Expected: all tests PASS. The existing no-mapping tests continue exercising the BoardTransport/YMODEM recovery path.

- [ ] **Step 6: Format, lint, inspect the diff, and commit**

Run inside the persistent container:

```bash
tools/docker/run_dev_container.sh -- bash -lc \
  'ruff format --check tools/riscv/megrez_physical_graphics.py tools/riscv/tests/test_megrez_physical_graphics.py && ruff check tools/riscv/megrez_physical_graphics.py tools/riscv/tests/test_megrez_physical_graphics.py'
git diff --check
git diff -- tools/riscv/megrez_physical_graphics.py \
  tools/riscv/tests/test_megrez_physical_graphics.py
git add tools/riscv/megrez_physical_graphics.py \
  tools/riscv/tests/test_megrez_physical_graphics.py
git commit -m "feat(riscv): load physical graphics artifacts from MMC"
```

Expected: formatting, lint, and diff checks succeed; the commit contains only the adapter and its tests.

### Task 3: Stage the frozen boot bytes through RockOS

**Files:**
- Create at runtime: `target/current-main-physical-graphics/physical/rockos-staging/manifest.txt`
- Create at runtime: `target/current-main-physical-graphics/physical/rockos-staging/staging.serial.log`
- Reuse: `target/current-main-physical-graphics/physical/inputs/kernel.Image`
- Reuse: `target/current-main-physical-graphics/physical/inputs/initramfs.cpio`
- Reuse on partition 1: `dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb`

- [ ] **Step 1: Revalidate the frozen plan and source identities without rebuilding**

Run:

```bash
git status --short
sha256sum \
  target/current-main-physical-graphics/physical/plan.json \
  target/current-main-physical-graphics/physical/inputs/kernel.Image \
  target/current-main-physical-graphics/physical/inputs/initramfs.cpio
```

Expected hashes:

```text
34bc1cc073e1e74f5ec3d4718b494035243f197e96639a7b744e6720510bf048  plan.json
f333402e9102e9094cead70cee6ed59e161fb0d226bff461e1a66cc1fca73607  kernel.Image
37465c5579547d5f00a0db8fbe5d30cea85a553ecaaff08d611d819ce934555f  initramfs.cpio
```

Expected: Git is clean. Do not run `make kernel`, install `cargo-osdk`, recreate the container, or inspect partition 2.

- [ ] **Step 2: Create uniquely named HTTP inputs and the canonical manifest**

Run from the worktree, after the implementation commit:

```bash
stage_dir="$PWD/target/current-main-physical-graphics/physical/rockos-staging"
head_short="$(git rev-parse --short=8 HEAD)"
plan_short=34bc1cc0
kernel_name="asterinas-${head_short}-${plan_short}.Image"
initramfs_name="asterinas-${head_short}-${plan_short}-stage1.cpio"
mkdir -p "$stage_dir"
test ! -e "$stage_dir/$kernel_name"
test ! -e "$stage_dir/$initramfs_name"
cp --reflink=auto \
  target/current-main-physical-graphics/physical/inputs/kernel.Image \
  "$stage_dir/$kernel_name"
cp --reflink=auto \
  target/current-main-physical-graphics/physical/inputs/initramfs.cpio \
  "$stage_dir/$initramfs_name"
sha256sum "$stage_dir/$kernel_name" "$stage_dir/$initramfs_name" \
  > "$stage_dir/SHA256SUMS"
```

Write `manifest.txt` with the exact current Git commit, plan SHA-256, filenames, byte sizes, SHA-256 values, CRC32 values (`d7578e4d` and `3345c3f5`), and load addresses (`0x80200000` and `0x83000000`). Re-read it and require the two expected SHA-256 strings before serving.

- [ ] **Step 3: Start the existing bounded HTTP server on the private host address**

Run the existing `_RootServer` helper in a long-lived shell session, serving only `$stage_dir` on `10.100.19.216:18081`:

```bash
python3 -c 'import signal,threading; from pathlib import Path; from tools.riscv.megrez_debian_install import _root_server; stop=threading.Event(); signal.signal(signal.SIGTERM, lambda *_: stop.set()); signal.signal(signal.SIGINT, lambda *_: stop.set()); server=_root_server("10.100.19.216",18081,Path("target/current-main-physical-graphics/physical/rockos-staging")); server.__enter__(); stop.wait(); server.__exit__(None,None,None)'
```

Expected: the server binds only the private address and remains active until staging is complete.

- [ ] **Step 4: Boot the explicit RockOS entry and publish only verified new files**

At the fresh U-Boot prompt run:

```text
sysboot mmc 1:1 any 0x88200000 /extlinux/extlinux.conf
1
```

Log in through the serial console without putting credentials in argv, environment variables, the repository, or `staging.serial.log`. In RockOS, verify `/boot` is partition 1 and has at least 32 MiB free. For each unique file, download from `http://10.100.19.216:18081/` into `/tmp/<name>.part`, require the manifest byte size and SHA-256, require that `/boot/<name>` does not already exist, then install it read-only. Run `sync`, hash both `/boot` files again, and record only the redacted verification output.

Expected: the installed kernel is 15,476,128 bytes with SHA-256 `f333402e9102e9094cead70cee6ed59e161fb0d226bff461e1a66cc1fca73607`; the installed initramfs is 568,320 bytes with SHA-256 `37465c5579547d5f00a0db8fbe5d30cea85a553ecaaff08d611d819ce934555f`. No partition-2 command is issued.

- [ ] **Step 5: Reboot normally and require a fresh U-Boot prompt**

Run `sudo reboot` in RockOS, stop the HTTP server after both installed-file hashes have been captured, and retain `staging.serial.log` plus `manifest.txt` under the staging directory.

Expected: a new ordered OpenSBI/U-Boot epoch reaches `=>`; no reset, power cycle, or YMODEM recovery is used.

### Task 4: Run the physical Firefox interaction gate from MMC

**Files:**
- Create at runtime: `target/current-main-physical-graphics/physical/mmc-graphics-final/`
- Use external capture path: `target/current-main-physical-graphics/physical/hdmi-capture.png`

- [ ] **Step 1: Refresh the preboard permit for the final clean implementation commit**

Verify the tree is clean, then invoke the existing permit issuer in the persistent container while binding its Git identity callback to `git rev-parse HEAD`. Require the permit to reference the unchanged plan SHA-256 `34bc1cc073e1e74f5ec3d4718b494035243f197e96639a7b744e6720510bf048` and the current commit. Do not rebuild any artifact.

- [ ] **Step 2: Start the physical gate with the staged MMC names**

Run with the names generated in Task 3:

```bash
python3 -m tools.riscv.megrez_physical_graphics \
  /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0 \
  --plan target/current-main-physical-graphics/physical/plan.json \
  --output-directory \
    target/current-main-physical-graphics/physical/mmc-graphics-final \
  --hdmi-capture \
    "$PWD/target/current-main-physical-graphics/physical/hdmi-capture.png" \
  --mmc-kernel "$kernel_name" \
  --mmc-initramfs "$initramfs_name" \
  --mmc-dtb \
    dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb \
  --artifact-timeout 120 \
  --boot-timeout 300 \
  --cycle-timeout 180 \
  --hdmi-timeout 300 \
  --recovery-timeout 930
```

Expected before `booti`: all three entries in `transport.json` are `*:mmc`; U-Boot reports exactly 15,476,128 kernel bytes, 568,320 initramfs bytes, and 154,800 DTB bytes, with CRC32 values `d7578e4d`, `3345c3f5`, and `4afcb20e` respectively.

- [ ] **Step 3: Supply three real keyboard/mouse cycles and a new HDMI capture**

For each printed nonce, type it on the board's USB keyboard, move the USB mouse, and click the amber button. During cycle 3, create a new PNG or JPEG at the absolute `--hdmi-capture` path showing the cyan Firefox page.

Expected: each cycle produces nonce-bound trusted input, DOM, and screenshot markers. The HDMI file must be new or changed after gate invalidation and settle before ingestion.

- [ ] **Step 4: Verify automatic recovery and the terminal result**

Allow the fixed Asterinas recovery timer to reboot the board; require a fresh OpenSBI/U-Boot/prompt epoch. Then run:

```bash
python3 -m json.tool \
  target/current-main-physical-graphics/physical/mmc-graphics-final/result.json
sha256sum -c \
  target/current-main-physical-graphics/physical/mmc-graphics-final/sha256sums.txt
```

Expected: `passed` is `true`, `reason` is `physical-graphics-pass`, `recovered` is `true`, there are three interaction cycles, HDMI evidence is present, and every retained hash verifies.

### Task 5: Final verification and evidence handoff

**Files:**
- Inspect: `target/current-main-physical-graphics/physical/rockos-staging/`
- Inspect: `target/current-main-physical-graphics/physical/mmc-graphics-final/`

- [ ] **Step 1: Run source and focused regression verification from the persistent container**

```bash
tools/docker/run_dev_container.sh -- bash -lc \
  'python3 -m unittest tools.riscv.tests.test_megrez_physical_graphics tools.riscv.tests.test_megrez_board_session -v && ruff format --check tools/riscv/megrez_physical_graphics.py tools/riscv/tests/test_megrez_physical_graphics.py && ruff check tools/riscv/megrez_physical_graphics.py tools/riscv/tests/test_megrez_physical_graphics.py'
git diff --check
git status --short
```

Expected: all tests and checks pass and the source tree is clean.

- [ ] **Step 2: Audit the physical evidence for forbidden fallback and partition-2 activity**

```bash
rg -n -i \
  'loady|ymodem|mmc write|partition.?2|DEBIAN_INSTALL_(START|PASS|FAIL)|kernel panic|not syncing|oops:' \
  target/current-main-physical-graphics/physical/rockos-staging/staging.serial.log \
  target/current-main-physical-graphics/physical/mmc-graphics-final/physical.serial.log
```

Expected: no YMODEM load, MMC write, installer marker, partition-2 operation, or fatal kernel marker appears. RockOS `/boot` publication may appear only in the staging transcript.

- [ ] **Step 3: Report the measured outcome**

Record the RockOS network staging duration, all three U-Boot `ext4load` durations, time from `booti` to graphical readiness, Firefox process/service stability, three physical interaction cycles, HDMI identity, and recovery result. Distinguish a staging-only success from a complete Firefox physical-graphics pass; do not claim the latter unless `result.json` is present, hash-valid, and passing.
