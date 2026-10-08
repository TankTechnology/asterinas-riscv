# Megrez Debug Workflow M1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the immutable launch-plan contract, a `plan/check/board --dry-run` user entry, and a PTY-backed U-Boot/XMODEM simulator so artifact and transport mistakes fail before a physical-board run.

**Architecture:** A pure contract module owns one-open artifact identities and canonical plan JSON. A thin CLI owns parsing, result publication, and deterministic dry-run actions without duplicating XMODEM or BoardSession internals. A PTY test server exercises the production XMODEM sender against both real U-Boot completion protocols.

**Tech Stack:** Python 3 standard library, `dataclasses`, `hashlib`, `json`, `zlib`, `pty`, `termios`, `unittest`, GNU Make.

---

### Task 1: Freeze artifact identity from one open file

**Files:**
- Create: `tools/riscv/megrez_debug_contract.py`
- Create: `tools/riscv/tests/test_megrez_debug.py`

- [ ] **Step 1: Write the failing identity tests**

Create one artifact, compute its expected SHA-256 and CRC32, and assert the exact frozen value. Patch `Path.open` so the pathname is replaced immediately after the production fd opens; assert the identity still describes the held fd and the callback ran exactly once. Add rejection cases for empty, over-64-MiB, symlink, directory, and opened-inode mismatch.

```python
identity = ArtifactIdentity.from_path("kernel", artifact, 0x80200000)
self.assertEqual(identity.size, len(payload))
self.assertEqual(identity.sha256, hashlib.sha256(payload).hexdigest())
self.assertEqual(identity.crc32, f"{zlib.crc32(payload):08x}")
self.assertEqual(open_count, 1)
```

- [ ] **Step 2: Run RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_debug.MegrezDebugArtifactTests -v
```

Expected: import error because `tools.riscv.megrez_debug_contract` does not exist.

- [ ] **Step 3: Implement the identity boundary**

Define:

```python
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024

@dataclass(frozen=True)
class ArtifactIdentity:
    name: str
    path: str
    load_address: int
    size: int
    sha256: str
    crc32: str

    @classmethod
    def from_path(cls, name: str, path: Path, load_address: int) -> "ArtifactIdentity":
        if name not in ARTIFACT_NAMES or load_address <= 0 or load_address % 4:
            raise DebugContractError("invalid artifact identity")
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise DebugContractError("artifact must be a regular non-symlink file")
        digest = hashlib.sha256()
        crc = 0
        size = 0
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise DebugContractError("artifact identity changed before open")
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_ARTIFACT_BYTES:
                    raise DebugContractError("artifact exceeds 64 MiB")
                digest.update(chunk)
                crc = zlib.crc32(chunk, crc)
        if size == 0 or size != opened.st_size:
            raise DebugContractError("artifact is empty or changed while reading")
        return cls(
            name=name,
            path=str(path.absolute()),
            load_address=load_address,
            size=size,
            sha256=digest.hexdigest(),
            crc32=f"{crc:08x}",
        )
```

Validate logical names against the closed set `kernel`, `initramfs`, `qemu_dtb`, `megrez_dtb`; require a positive 4-byte-aligned address. Use `lstat`, then one `Path.open("rb")`, `os.fstat`, and a bounded loop that updates SHA-256 and `zlib.crc32` from the same chunks. Reject symlinks, inode replacement before open, short/long reads, empty files, and files larger than 64 MiB. Preserve ordinary `OSError` subclasses for missing paths and raise `DebugContractError` for contract violations.

- [ ] **Step 4: Run GREEN and static checks**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_debug.MegrezDebugArtifactTests -v
python3 -m py_compile tools/riscv/megrez_debug_contract.py \
  tools/riscv/tests/test_megrez_debug.py
ruff check tools/riscv/megrez_debug_contract.py tools/riscv/tests/test_megrez_debug.py
ruff format --check tools/riscv/megrez_debug_contract.py tools/riscv/tests/test_megrez_debug.py
```

Expected: all focused tests and static checks pass.

- [ ] **Step 5: Commit**

```bash
git add tools/riscv/megrez_debug_contract.py tools/riscv/tests/test_megrez_debug.py
git commit -m "feat(riscv): freeze Megrez debug artifacts"
```

### Task 2: Add an exact canonical launch-plan schema

**Files:**
- Modify: `tools/riscv/megrez_debug_contract.py`
- Modify: `tools/riscv/tests/test_megrez_debug.py`

- [ ] **Step 1: Write failing schema tests**

Create four small artifacts and require a round-trip plan with exact schema `1`, profile `tcp-probe`, SMP `4`, `sv39=True`, `reboot_after=180`, a nonempty ordered marker tuple, and the closed artifact-name set. Assert byte-identical canonical output across two writes. Reject duplicate keys at top level and inside artifacts, unknown/missing keys, booleans-as-integers, SMP other than 4, `sv39=False`, unsafe bootargs, reordered artifact names, invalid hashes/CRC, and a recomputed plan-hash mismatch.

```python
encoded = plan.canonical_bytes()
self.assertEqual(encoded, DebugPlan.from_bytes(encoded).canonical_bytes())
self.assertEqual(plan.plan_sha256, hashlib.sha256(encoded).hexdigest())
```

- [ ] **Step 2: Run RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_debug.MegrezDebugPlanTests -v
```

Expected: `DebugPlan` or its JSON methods are absent.

- [ ] **Step 3: Implement `DebugPlan` and `StageResult`**

```python
@dataclass(frozen=True)
class DebugPlan:
    schema_version: int
    profile: str
    artifacts: tuple[ArtifactIdentity, ...]
    bootargs: str
    smp: int
    sv39: bool
    markers: tuple[str, ...]
    reboot_after: int

@dataclass(frozen=True)
class StageResult:
    schema_version: int
    stage: str
    passed: bool
    reason: str
    plan_sha256: str
    evidence: tuple[str, ...]
```

Use `json.loads(data, object_pairs_hook=reject_duplicate_pairs)` to reject duplicate keys at every depth. Serialize with `sort_keys=True`, `separators=(",", ":")`, UTF-8, and a final newline. `plan_sha256` hashes canonical plan bytes. Validate bootargs with the existing no-control/no-U-Boot-separator policy and require the exact four artifact names in canonical order.

- [ ] **Step 4: Run GREEN and commit**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_debug.MegrezDebugPlanTests -v
git diff --check
git add tools/riscv/megrez_debug_contract.py tools/riscv/tests/test_megrez_debug.py
git commit -m "feat(riscv): define Megrez debug plan"
```

### Task 3: Add `plan`, `check`, and `board --dry-run`

**Files:**
- Create: `tools/riscv/megrez_debug.py`
- Modify: `tools/riscv/tests/test_megrez_debug.py`
- Modify: `Makefile`

- [ ] **Step 1: Write failing CLI tests**

Invoke the CLI from an arbitrary working directory with artifact paths containing spaces. Require `plan` to create mode-0644 canonical JSON by same-directory temporary file, fsync, and atomic replace; reject output symlinks/directories without mutation. Require `check` to return zero and print only `MEGREZ_DEBUG_CHECK_PASS plan=<64-lower-hex>`.

Require `board --dry-run` to perform no serial open and emit this ordered action set:

```python
(
    {"action": "require-simulation", "tier": "fast"},
    {"action": "probe-uboot-baud", "choices": [115200, 1500000]},
    {"action": "cache-or-transfer", "artifact": "kernel", "address": 0x80200000},
    {"action": "cache-or-transfer", "artifact": "initramfs", "address": 0x83000000},
    {"action": "cache-or-transfer", "artifact": "megrez_dtb", "address": 0xF0000000},
    {"action": "boot-once", "reboot_after": 180},
    {"action": "capture-markers"},
    {"action": "await-automatic-recovery"},
)
```

The QEMU DTB must be absent from physical actions. Assert missing simulation evidence is `plan-simulation-missing` outside `--dry-run`.

- [ ] **Step 2: Run RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_debug.MegrezDebugCliTests -v
```

Expected: module/CLI not found.

- [ ] **Step 3: Implement the thin CLI**

Create exact subcommands:

```text
megrez_debug.py plan --kernel PATH --initramfs PATH --qemu-dtb PATH \
  --megrez-dtb PATH --bootargs TEXT --marker TEXT --output PLAN
megrez_debug.py check PLAN
megrez_debug.py board PLAN DEVICE --simulation-result RESULT [--dry-run]
```

`plan` uses fixed addresses `0x80200000`, `0x83000000`, and `0xf0000000`; both target DTBs use the DTB address because only one is present per target. `check` reopens every current artifact and requires exact identity equality. `board --dry-run` validates only the plan, prints actions, never opens serial, and does not require a result file. Non-dry-run validates a passed `fast` StageResult with a matching plan hash, then exits with `plan-board-not-implemented` until M4 rather than touching hardware.

Add `test_riscv_megrez_debug_unit` to run only `tools.riscv.tests.test_megrez_debug`.

- [ ] **Step 4: Run GREEN and commit**

```bash
make test_riscv_megrez_debug_unit
python3 -m py_compile tools/riscv/megrez_debug.py
ruff check tools/riscv/megrez_debug.py tools/riscv/tests/test_megrez_debug.py
ruff format --check tools/riscv/megrez_debug.py tools/riscv/tests/test_megrez_debug.py
git diff --check
git add Makefile tools/riscv/megrez_debug.py tools/riscv/tests/test_megrez_debug.py
git commit -m "feat(riscv): add Megrez debug workflow entry"
```

### Task 4: Simulate both U-Boot XMODEM protocols through a PTY

**Files:**
- Modify: `tools/riscv/tests/test_megrez_xmodem.py`
- Modify: `tools/riscv/megrez_xmodem.py` only if the PTY exposes a production defect

- [ ] **Step 1: Add a bounded PTY U-Boot peer**

Open `pty.openpty()`, run a daemon peer thread with a five-second total deadline, and exercise the public `transfer()` function. The peer parses the paced `loadx` command, emits `press ENTER` only for the 115200-start path, requests CRC mode with `C`, validates every XMODEM-1K sequence/complement/CRC, ACKs packets and EOT, and emits exact total-size/start-address evidence.

For initial 115200 require delayed ESC before prompt. For current 1.5 Mbps emit the prompt directly and reject ESC. Assert received unpadded bytes equal the source and no peer thread remains alive.

- [ ] **Step 2: Run focused PTY tests**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_xmodem.MegrezXmodemTests.test_pty_initial_baud_transfer \
  tools.riscv.tests.test_megrez_xmodem.MegrezXmodemTests.test_pty_existing_transfer_baud -v
```

Expected: both pass, or a bounded RED identifies an exact production serial-state defect.

- [ ] **Step 3: Apply only an evidence-backed fix if needed**

Preserve the existing artifact cap, retry limit, prompt anchoring, and two completion branches. Any production change must first have an assertion showing the wrong byte/order/deadline and then change the smallest relevant helper.

- [ ] **Step 4: Run combined fast gate and commit**

```bash
make test_riscv_megrez_debug_unit
make test_riscv_megrez_gmac_unit
python3 -m py_compile tools/riscv/megrez_debug_contract.py \
  tools/riscv/megrez_debug.py tools/riscv/megrez_xmodem.py \
  tools/riscv/tests/test_megrez_debug.py tools/riscv/tests/test_megrez_xmodem.py
ruff check tools/riscv/megrez_debug_contract.py tools/riscv/megrez_debug.py \
  tools/riscv/megrez_xmodem.py tools/riscv/tests/test_megrez_debug.py \
  tools/riscv/tests/test_megrez_xmodem.py
ruff format --check tools/riscv/megrez_debug_contract.py tools/riscv/megrez_debug.py \
  tools/riscv/megrez_xmodem.py tools/riscv/tests/test_megrez_debug.py \
  tools/riscv/tests/test_megrez_xmodem.py
git diff --check
git add tools/riscv/megrez_xmodem.py tools/riscv/tests/test_megrez_xmodem.py
git commit -m "test(riscv): simulate Megrez XMODEM transport"
```

Expected: host gates pass in seconds, with no Docker, QEMU, network, or board access.

## Completion boundary

This plan ends after M1/M2. A subsequent plan connects `simulate --tier fast` to the existing QEMU probe gate; another connects the frozen board actions to physical serial. Physical orchestration therefore cannot drift before its artifact and transport contract is executable without hardware.
