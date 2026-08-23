# Debian RISC-V Input Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an SMP=4 QEMU gate that dynamically finds the Asterinas VirtIO keyboard and verifies a fixed press/release sequence before Debian rootfs integration.

**Architecture:** A static RISC-V `/init` scans evdev capability bitmaps and runs a small event-state machine. A Python runner boots the existing U-Boot handoff, waits for the guest-ready marker, injects keys over a private QEMU monitor socket, and records JSON plus serial evidence.

**Tech Stack:** C11/Linux input UAPI, Python 3 standard library, QEMU RISC-V, U-Boot `booti`, POSIX shell, `unittest`.

---

### Task 1: Host-side protocol and result classifier

**Files:**
- Create: `tools/riscv/debian/input_gate.py`
- Create: `tools/riscv/tests/test_debian_input_gate.py`

- [ ] **Step 1: Write failing protocol tests**

Add tests that import `tools/riscv/debian/input_gate.py` and require:

```python
class InputGateContractTests(unittest.TestCase):
    def test_qemu_argv_uses_smp4_and_two_distinct_input_devices(self) -> None:
        argv = gate.qemu_argv(
            Path("/u-boot"), Path("/boot.ext4"), Path("/tmp/gate-monitor.sock"), 4
        )
        self.assertIn("4", argv[argv.index("-smp") + 1])
        self.assertIn("virtio-tablet-device", argv)
        self.assertIn("virtio-keyboard-device", argv)
        self.assertLess(argv.index("virtio-tablet-device"), argv.index("virtio-keyboard-device"))

    def test_injected_sequence_covers_normal_modifier_and_editing_keys(self) -> None:
        self.assertEqual(gate.KEY_SEQUENCE, ("a", "shift-b", "backspace", "ctrl-c"))

    def test_classification_requires_ready_and_pass_without_panic(self) -> None:
        transcript = gate.READY_MARKER + b"\n" + gate.PASS_MARKER + b"\n"
        self.assertTrue(gate.classify_transcript(transcript).passed)
        self.assertFalse(gate.classify_transcript(gate.PASS_MARKER).passed)
        self.assertFalse(gate.classify_transcript(transcript + b"Kernel panic").passed)
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_debian_input_gate
```

Expected: import failure because `tools/riscv/debian/input_gate.py` does not exist.

- [ ] **Step 3: Implement the minimal protocol module**

Implement immutable constants, a frozen result dataclass, strict positive-SMP
validation, deterministic QEMU argv rendering, and transcript classification:

```python
READY_MARKER = b"__DEBIAN_INPUT_GATE_READY__"
PASS_MARKER = b"__DEBIAN_INPUT_GATE_PASS__"
KEY_SEQUENCE = ("a", "shift-b", "backspace", "ctrl-c")
PANIC_MARKERS = (b"Kernel panic", b"kernel panic", b"BUG:", b"panic!")

@dataclass(frozen=True)
class GateResult:
    ready: bool
    complete: bool
    panics: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.ready and self.complete and not self.panics
```

The QEMU argv must use `-machine virt`, four vCPUs by default, a private UNIX
monitor, serial stdio, the prepared boot disk, tablet before keyboard, and no
network device.

- [ ] **Step 4: Run the tests and verify GREEN**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_debian_input_gate
```

Expected: all protocol/classifier tests pass.

- [ ] **Step 5: Commit**

```bash
git add tools/riscv/debian/input_gate.py tools/riscv/tests/test_debian_input_gate.py
git commit -m "test(riscv): define Debian input gate contract"
```

### Task 2: Guest evdev capability discovery and event state machine

**Files:**
- Create: `tools/riscv/debian/input_gate_init.c`
- Modify: `tools/riscv/tests/test_debian_input_gate.py`

- [ ] **Step 1: Write failing guest-source tests**

Add a test that compiles the guest source natively in self-test mode and runs
the result:

```python
def test_guest_state_machine_self_test(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
        binary = Path(directory) / "input-gate-selftest"
        subprocess.run(
            ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
             "-DINPUT_GATE_SELF_TEST", str(GUEST_SOURCE), "-o", str(binary)],
            check=True,
        )
        completed = subprocess.run([binary], check=False, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("input gate state machine: PASS", completed.stdout)
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_debian_input_gate
```

Expected: failure because `input_gate_init.c` is absent.

- [ ] **Step 3: Implement the pure event state machine**

Define a compact expected sequence using Linux input constants:

```c
static const struct expected_event EXPECTED[] = {
    { KEY_A, 1 }, { KEY_A, 0 },
    { KEY_LEFTSHIFT, 1 }, { KEY_B, 1 }, { KEY_B, 0 }, { KEY_LEFTSHIFT, 0 },
    { KEY_BACKSPACE, 1 }, { KEY_BACKSPACE, 0 },
    { KEY_LEFTCTRL, 1 }, { KEY_C, 1 }, { KEY_C, 0 }, { KEY_LEFTCTRL, 0 },
};
```

Ignore non-`EV_KEY` events and key-repeat value `2`. Reject out-of-order key
press/release events. In `INPUT_GATE_SELF_TEST` mode, feed the exact sequence,
verify completion, then feed an out-of-order sequence and verify rejection.

- [ ] **Step 4: Implement evdev discovery and the guest main loop**

In normal mode:

- open `/dev/input/event0` through `/dev/input/event31` non-blocking;
- query `EVIOCGBIT(EV_KEY, ...)`;
- require `KEY_A`, `KEY_B`, `KEY_C`, `KEY_ENTER`, `KEY_BACKSPACE`,
  `KEY_LEFTSHIFT`, and `KEY_LEFTCTRL`;
- print the ready marker with node and `EVIOCGNAME` result;
- poll and read complete `struct input_event` records;
- print the pass marker and exit zero only after the full sequence;
- print a precise failure marker and exit nonzero on timeout or invalid order.

- [ ] **Step 5: Run the tests and verify GREEN**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_debian_input_gate
```

Expected: the native self-test and host protocol tests pass.

- [ ] **Step 6: Commit**

```bash
git add tools/riscv/debian/input_gate_init.c tools/riscv/tests/test_debian_input_gate.py
git commit -m "test(riscv): add evdev keyboard probe"
```

### Task 3: Reproducible initramfs builder

**Files:**
- Create: `tools/riscv/debian/build_input_gate.sh`
- Modify: `tools/riscv/tests/test_debian_input_gate.py`

- [ ] **Step 1: Write failing builder contract tests**

Add tests that invoke `build_input_gate.sh --print-tools` and
`build_input_gate.sh --print-entries`, requiring exactly:

```text
riscv64-linux-gnu-gcc
cpio
```

and:

```text
.
init
```

Also require an unknown option to return status 2 without creating output.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_debian_input_gate
```

Expected: failure because the builder does not exist.

- [ ] **Step 3: Implement the deterministic builder**

The script must:

```bash
set -euo pipefail
CC="${RISC_V_CC:-riscv64-linux-gnu-gcc}"
"${CC}" -std=c11 -O2 -static -no-pie -Wall -Wextra -Werror \
    -o "${STAGE}/init" "${SRC_DIR}/input_gate_init.c"
printf '.\ninit\n' | cpio --quiet --reproducible -o -H newc \
    -D "${STAGE}" > "${OUTPUT_TMP}"
mv "${OUTPUT_TMP}" "${OUTPUT}"
```

Use `mktemp -d`, a trap for cleanup, an atomic temporary output in the final
directory, and mode `0755` on `/init`. Do not delete an existing valid output
until the replacement has been built successfully.

- [ ] **Step 4: Run the tests and verify GREEN**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_debian_input_gate
```

Expected: all builder-contract and earlier tests pass.

- [ ] **Step 5: Cross-build inside the project container**

Run:

```bash
docker run --rm -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702 \
  bash tools/riscv/debian/build_input_gate.sh
```

Expected: `target/debian-riscv/input-gate/initramfs.cpio` is non-empty and
`cpio -it` prints only `.` and `init`.

- [ ] **Step 6: Commit**

```bash
git add tools/riscv/debian/build_input_gate.sh tools/riscv/tests/test_debian_input_gate.py
git commit -m "build(riscv): package Debian input gate"
```

### Task 4: QEMU orchestration and evidence

**Files:**
- Modify: `tools/riscv/debian/input_gate.py`
- Modify: `tools/riscv/tests/test_debian_input_gate.py`

- [ ] **Step 1: Write failing orchestration tests**

Use fake boot and monitor objects to require this order:

```text
U-Boot prompt -> booti commands -> guest ready -> a -> shift-b -> backspace -> ctrl-c -> guest pass
```

Require cleanup after success, timeout, monitor connection failure, and early
QEMU exit. Require JSON to contain `smp`, `ready`, `complete`, `panics`,
`passed`, and SHA-256 identities for U-Boot, boot disk, kernel, DTB, and
initramfs.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_debian_input_gate
```

Expected: failures for the missing orchestration and evidence functions.

- [ ] **Step 3: Implement private preparation and QEMU lifecycle**

Add CLI arguments for explicit kernel, U-Boot, DTB, initramfs, output directory,
SMP, and timeouts. Validate all inputs as non-symlink regular files. Create a
private ext4 boot disk containing:

```text
/asterinas.booti
/initramfs.cpio.gz
/qemu-virt.dtb
```

Drive U-Boot with the existing generic `virtio scan`, `ext4load`, `fdt addr`,
and `booti` sequence. Use a private UNIX monitor socket below the output
directory and QEMU HMP `sendkey` for the fixed reviewed sequence.

- [ ] **Step 4: Implement evidence publication**

Write the complete serial transcript first, then atomically publish
`result.json` with sorted keys. Include artifact SHA-256 values and the exact
QEMU argv. Do not publish `passed: true` until the pass marker has been read and
the transcript has been checked for panic markers.

- [ ] **Step 5: Run the tests and verify GREEN**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_debian_input_gate
```

Expected: all orchestration, cleanup, and evidence tests pass.

- [ ] **Step 6: Commit**

```bash
git add tools/riscv/debian/input_gate.py tools/riscv/tests/test_debian_input_gate.py
git commit -m "test(riscv): automate VirtIO keyboard gate"
```

### Task 5: Operator entry point and local SMP=4 gate

**Files:**
- Create: `tools/riscv/debian/README.md`
- Modify: `Makefile`
- Modify: `tools/riscv/README.md`

- [ ] **Step 1: Add the unit-test target**

Add:

```make
.PHONY: test_riscv_debian_input_unit
test_riscv_debian_input_unit:
	python3 -m unittest tools.riscv.tests.test_debian_input_gate
```

- [ ] **Step 2: Document exact build and run commands**

Document the official Debian 13 riscv64 image/package provenance, explain that
this milestone is the kernel-facing prerequisite rather than a full Debian
rootfs, and provide exact container build and SMP=4 run commands with explicit
artifact paths.

- [ ] **Step 3: Run the unit and format checks**

Run:

```bash
make test_riscv_debian_input_unit
python3 -m py_compile tools/riscv/debian/input_gate.py tools/riscv/tests/test_debian_input_gate.py
git diff --check
```

Expected: zero failures and zero whitespace errors.

- [ ] **Step 4: Run the fresh QEMU SMP=4 gate**

Run the documented container command with the current `origin/main` kernel,
the already prepared U-Boot binary/DTB cache, and the newly built initramfs.

Expected evidence:

```text
__DEBIAN_INPUT_GATE_READY__
__DEBIAN_INPUT_GATE_PASS__
```

and `target/debian-riscv/input-gate/run/result.json` contains
`"passed": true`, `"smp": 4`, and an empty panic list.

- [ ] **Step 5: Commit documentation and Makefile integration**

```bash
git add Makefile tools/riscv/README.md tools/riscv/debian/README.md
git commit -m "docs(riscv): document Debian input gate"
```

- [ ] **Step 6: Review final branch state**

Run:

```bash
git status --short --branch
git log --oneline origin/main..HEAD
git diff --check origin/main...HEAD
```

Expected: a clean branch containing only the design, plan, input-gate code,
tests, Makefile entry, and operator documentation.
