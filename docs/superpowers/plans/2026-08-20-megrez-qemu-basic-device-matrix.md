# Megrez QEMU Basic and Device Matrix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one local, evidence-producing QEMU gate that boots Asterinas through U-Boot into a BusyBox serial shell on the four-hart Megrez Sv48 contract approximation, validates a firmware framebuffer, and runs isolated RISC-V device and compatibility probes without claiming physical-board coverage.

**Architecture:** Extend the existing immutable U-Boot runner with a typed device-set registry, fixed firmware-framebuffer commands, and a QMP screenshot hook. Build a deterministic BusyBox initramfs, then use a focused matrix orchestrator to build/snapshot Sv48 and Sv39 kernels, prepare profile-specific boot disks, execute isolated probes, and atomically aggregate evidence. Keep DW APB UART as a filtered RISC-V ktest and label it contract-only.

**Tech Stack:** Rust 2024 ktests, Python 3 standard library, QEMU 10.2.1, U-Boot 2026.07, BusyBox 1.36.1, Bash, GNU Make, ext4/debugfs tooling.

---

## File map

New files:

- `tools/riscv/qemu_uboot_devices.py`: immutable device-set and framebuffer registry.
- `tools/riscv/qemu_qmp.py`: bounded QMP handshake and screendump client.
- `tools/riscv/qemu_ppm.py`: strict P6 PPM parser and framebuffer audit.
- `tools/riscv/nixos/busybox-static.nix`: pinned static RISC-V BusyBox derivation.
- `tools/riscv/nixos/build_busybox.sh`: atomically publish the verified BusyBox binary.
- `tools/riscv/megrez_qemu_busybox.py`: deterministic BusyBox shell initramfs builder.
- `tools/riscv/megrez_qemu_matrix.py`: artifact builder, probe executor, result aggregation, and CLI.
- `tools/riscv/tests/test_megrez_qemu_matrix.py`: unit and contract tests for all new matrix code.

Modified files:

- `tools/riscv/qemu_uboot_commands.py`: render registered devices and framebuffer U-Boot commands.
- `tools/riscv/qemu_uboot_booti.py`: parse typed device/capture arguments.
- `tools/riscv/qemu_uboot_execution.py`: bind device identity and display evidence to results.
- `tools/riscv/qemu_uboot_execution_io.py`: pin screenshot/display-audit outputs and private capture staging.
- `tools/riscv/qemu_uboot_session.py`: run one typed terminal action before QEMU cleanup.
- `tools/riscv/qemu_uboot_shell.py`: register fixed shell/device interactions.
- `tools/riscv/make_qemu_uboot_initramfs.py`: expose deterministic archive publication for a supplied `/init` payload.
- `tools/riscv/tests/test_qemu_uboot_contracts.py`: device registry and command-rendering tests.
- `tools/riscv/tests/test_qemu_uboot_booti.py`: capture lifecycle and legacy-regression tests.
- `Makefile`: unit and full matrix targets.
- `tools/riscv/README.md`: operator commands and fidelity boundary.
- `docs/porting/README.md`: live Megrez status after evidence is produced.

## Execution precondition

Run the plan from the dedicated worktree root. Create one persistent local
container before Task 1 and reuse it for every command:

```bash
worktree_root="$(pwd -P)"
test "$(git branch --show-current)" = codex/megrez-qemu-basic-matrix
test -z "$(docker ps -aq --filter name='^/codex-megrez-qemu-matrix$')"
docker run -d --name codex-megrez-qemu-matrix \
  --privileged --network=host -v /dev:/dev \
  -v "$worktree_root:/root/asterinas" -w /root/asterinas \
  asterinas-env:uboot-sim tail -f /dev/null
docker exec codex-megrez-qemu-matrix qemu-system-riscv64 --version
docker exec codex-megrez-qemu-matrix nix-instantiate --version
```

Expected: the branch assertion passes, the container name is unused, QEMU
reports 10.2.1, and Nix responds. Do not monitor remote CI; all gates in this
plan are local. Keep the container until the branch is finished so cached
U-Boot and Nix inputs remain available.

## Increment 1: Typed display path and interactive shell

### Task 1: Add an immutable QEMU device-set registry

**Files:**
- Create: `tools/riscv/qemu_uboot_devices.py`
- Modify: `tools/riscv/qemu_uboot_commands.py`
- Test: `tools/riscv/tests/test_qemu_uboot_contracts.py`

- [ ] **Step 1: Write failing registry tests**

Add imports and tests that require a frozen registry and unchanged legacy argv:

```python
from qemu_uboot_devices import (
    HEADLESS,
    MEGREZ_BASIC,
    DeviceKind,
    device_set_by_name,
    validate_registered_device_set,
)

def test_device_sets_are_registered_and_frozen(self) -> None:
    self.assertIs(device_set_by_name("headless"), HEADLESS)
    self.assertIs(device_set_by_name("megrez-basic"), MEGREZ_BASIC)
    self.assertEqual(
        MEGREZ_BASIC.devices,
        (DeviceKind.BOCHS_DISPLAY,),
    )
    with self.assertRaises(FrozenInstanceError):
        MEGREZ_BASIC.name = "changed"

def test_replaced_device_set_is_rejected(self) -> None:
    with self.assertRaisesRegex(ValueError, "registered device set"):
        validate_registered_device_set(
            replace(MEGREZ_BASIC, devices=(DeviceKind.BOCHS_DISPLAY,))
        )
```

Keep the existing
`test_default_qemu_arguments_remain_byte_for_byte_compatible` test unchanged;
it is the exact legacy-argv regression oracle for this task.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
docker exec codex-megrez-qemu-matrix python3 -m unittest \
  tools.riscv.tests.test_qemu_uboot_contracts.ContractCompositionTests -v
```

Expected: import failure for `qemu_uboot_devices`.

- [ ] **Step 3: Implement the closed registry**

Create these public types and values:

```python
class DeviceKind(str, Enum):
    BOCHS_DISPLAY = "bochs-display"
    VIRTIO_KEYBOARD = "virtio-keyboard"
    VIRTIO_RNG = "virtio-rng"
    VIRTIO_NET = "virtio-net"
    VIRTIO_GPU = "virtio-gpu"
    SCRATCH_VIRTIO_BLOCK = "scratch-virtio-block"
    NVME = "nvme"

@dataclass(frozen=True)
class FramebufferContract:
    address: int
    size: int
    width: int
    height: int
    stride: int
    pixel_format: str

@dataclass(frozen=True)
class QemuDeviceSet:
    name: str
    devices: tuple[DeviceKind, ...]
    framebuffer: FramebufferContract | None = None

@dataclass(frozen=True)
class RuntimeDevicePaths:
    capture_root: Path | None = None
    monitor_socket: Path | None = None
    scratch_disk: Path | None = None
    nvme_disk: Path | None = None

BOCHS_XRGB8888 = FramebufferContract(
    address=0x4000_0000,
    size=0x0100_0000,
    width=1280,
    height=1024,
    stride=5120,
    pixel_format="x8r8g8b8",
)
HEADLESS = QemuDeviceSet("headless", ())
MEGREZ_BASIC = QemuDeviceSet(
    "megrez-basic",
    (DeviceKind.BOCHS_DISPLAY,),
    BOCHS_XRGB8888,
)
```

Use a `MappingProxyType` registry. Reject duplicate devices, a framebuffer
without `BOCHS_DISPLAY`, and unregistered replacements.

- [ ] **Step 4: Render only closed device kinds**

Extend `qemu_argv` with typed arguments while preserving its default output:

```python
def qemu_argv(
    *,
    uboot: Path,
    boot_disk: Path,
    profile: QemuUbootProfile = GENERIC_SV39,
    device_set: QemuDeviceSet = HEADLESS,
    device_paths: RuntimeDevicePaths | None = None,
    slow_permit: object | None = None,
    guest_reboot: bool = False,
    snapshot_disk: bool = False,
) -> list[str]:
    validate_registered_device_set(device_set)
    argv = _base_qemu_argv(
        uboot=uboot,
        boot_disk=boot_disk,
        profile=profile,
        slow_permit=slow_permit,
        guest_reboot=guest_reboot,
        snapshot_disk=snapshot_disk,
    )
    argv.extend(render_device_argv(device_set, device_paths))
    return argv
```

`render_device_argv` maps enum members to fixed argument tuples. It must not
accept string arguments from the CLI. Extract the current argv body verbatim
into `_base_qemu_argv`; its existing exact-output regression must continue to
pass before any device-specific tests are accepted. For this increment,
`BOCHS_DISPLAY` renders
`("-device", "bochs-display,xres=1280,yres=1024")`, and
`VIRTIO_KEYBOARD` renders
`("-device", "virtio-keyboard-device")`. A framebuffer set additionally
requires an absolute, non-symlinked mode-0700 `capture_root` plus one absolute,
non-symlinked, comma-free `monitor_socket` below that directory and renders
`("-qmp", f"unix:{monitor_socket},server=on,wait=off")`; headless rejects all
runtime device paths.

- [ ] **Step 5: Run tests and commit**

Run:

```bash
docker exec codex-megrez-qemu-matrix make test_riscv_uboot_booti_unit
git diff --check
```

Expected: 176 existing tests plus the new registry tests pass.

Commit:

```bash
git add tools/riscv/qemu_uboot_devices.py \
  tools/riscv/qemu_uboot_commands.py \
  tools/riscv/tests/test_qemu_uboot_contracts.py
git commit -m "test(riscv): register QEMU device sets"
```

### Task 2: Add fixed firmware-framebuffer U-Boot commands

**Files:**
- Modify: `tools/riscv/qemu_uboot_commands.py`
- Test: `tools/riscv/tests/test_qemu_uboot_contracts.py`

- [ ] **Step 1: Write failing exact-command tests**

```python
def test_megrez_basic_injects_one_fixed_framebuffer_before_booti(self) -> None:
    commands = boot_commands(
        profile=MEGREZ_SV48_SVADE_FAST,
        device_set=MEGREZ_BASIC,
    )
    names = [command.name for command in commands]
    self.assertLess(names.index("framebuffer-pci-probe"), names.index("framebuffer-node"))
    self.assertLess(names.index("framebuffer-verify"), names.index("booti"))
    self.assertEqual(names.count("booti"), 1)
    self.assertIn(
        'fdt set /framebuffer@40000000 compatible "simple-framebuffer"',
        [command.text for command in commands],
    )
    self.assertIn(
        "fdt set /framebuffer@40000000 reg <0x0 0x40000000 0x0 0x1000000>",
        [command.text for command in commands],
    )

def test_headless_commands_remain_byte_for_byte_compatible(self) -> None:
    self.assertEqual(
        boot_commands(device_set=HEADLESS),
        boot_commands(),
    )
```

- [ ] **Step 2: Run the tests and confirm RED**

Expected: `boot_commands` rejects the new keyword.

- [ ] **Step 3: Implement the framebuffer command renderer**

Add:

```python
def _framebuffer_plan(device_set: QemuDeviceSet) -> tuple[BootCommand, ...]:
    framebuffer = device_set.framebuffer
    if framebuffer is None:
        return ()
    node = f"/framebuffer@{framebuffer.address:x}"
    return (
        BootCommand("framebuffer-resize", "fdt resize 0x2000", "=>"),
        BootCommand("framebuffer-pci-probe", "pci display 0.1.0", "=>"),
        BootCommand("framebuffer-node", f"fdt mknode / {node[1:]}", "=>"),
        BootCommand(
            "framebuffer-compatible",
            f'fdt set {node} compatible "simple-framebuffer"',
            "=>",
        ),
        BootCommand(
            "framebuffer-reg",
            f"fdt set {node} reg <0x0 {framebuffer.address:#x} "
            f"0x0 {framebuffer.size:#x}>",
            "=>",
        ),
        BootCommand("framebuffer-width", f"fdt set {node} width <{framebuffer.width:#x}>", "=>"),
        BootCommand("framebuffer-height", f"fdt set {node} height <{framebuffer.height:#x}>", "=>"),
        BootCommand("framebuffer-stride", f"fdt set {node} stride <{framebuffer.stride:#x}>", "=>"),
        BootCommand("framebuffer-format", f'fdt set {node} format "{framebuffer.pixel_format}"', "=>"),
        BootCommand("framebuffer-status", f'fdt set {node} status "okay"', "=>"),
        BootCommand("framebuffer-verify", f"fdt print {node}", "simple-framebuffer"),
    )
```

Insert this plan after the payload DTB is selected and before bootargs are
printed. Validate the registered device set at function entry.

- [ ] **Step 4: Run tests and commit**

Run the complete U-Boot unit target and `git diff --check`.

Commit:

```bash
git add tools/riscv/qemu_uboot_commands.py \
  tools/riscv/tests/test_qemu_uboot_contracts.py
git commit -m "feat(riscv): register firmware framebuffer commands"
```

### Task 3: Add bounded QMP screenshot and PPM auditing

**Files:**
- Create: `tools/riscv/qemu_qmp.py`
- Create: `tools/riscv/qemu_ppm.py`
- Test: `tools/riscv/tests/test_megrez_qemu_matrix.py`

- [ ] **Step 1: Write failing PPM parser tests**

```python
class PpmAuditTests(unittest.TestCase):
    def test_accepts_nonempty_1280x1024_p6(self) -> None:
        payload = ppm_bytes(1280, 1024, {(0, 0, 0), (255, 255, 255), (0, 128, 255)})
        audit = audit_ppm(payload, expected_width=1280, expected_height=1024)
        self.assertTrue(audit.passed)
        self.assertGreater(audit.non_black_pixels, 0)
        self.assertGreaterEqual(audit.distinct_colors_lower_bound, 3)

    def test_rejects_short_black_or_wrong_dimension_images(self) -> None:
        with self.assertRaisesRegex(ValueError, "pixel payload"):
            audit_ppm(b"P6\n2 2\n255\n\0", expected_width=2, expected_height=2)
        self.assertFalse(
            audit_ppm(
                b"P6\n2 2\n255\n" + bytes(12),
                expected_width=2,
                expected_height=2,
            ).passed
        )
```

- [ ] **Step 2: Implement strict PPM parsing**

Expose:

```python
@dataclass(frozen=True)
class PpmAudit:
    width: int
    height: int
    max_value: int
    non_black_pixels: int
    distinct_colors_lower_bound: int
    bounding_box: tuple[int, int, int, int] | None
    passed: bool

def audit_ppm(
    payload: bytes,
    *,
    expected_width: int,
    expected_height: int,
) -> PpmAudit:
    match = re.match(rb"\AP6\n([1-9][0-9]*) ([1-9][0-9]*)\n255\n", payload)
    if match is None:
        raise ValueError("PPM must use the strict P6 header")
    width = int(match.group(1))
    height = int(match.group(2))
    if (width, height) != (expected_width, expected_height):
        raise ValueError("PPM dimensions do not match the registered display")
    pixels = payload[match.end():]
    if len(pixels) != width * height * 3:
        raise ValueError("PPM pixel payload has the wrong length")

    colors: set[bytes] = set()
    non_black_pixels = 0
    min_x, min_y = width, height
    max_x = max_y = -1
    for pixel_index in range(width * height):
        offset = pixel_index * 3
        pixel = pixels[offset:offset + 3]
        if len(colors) < 3:
            colors.add(pixel)
        if pixel != b"\x00\x00\x00":
            non_black_pixels += 1
            x = pixel_index % width
            y = pixel_index // width
            min_x, min_y = min(min_x, x), min(min_y, y)
            max_x, max_y = max(max_x, x), max(max_y, y)
    bounding_box = (
        None
        if max_x < 0
        else (min_x, min_y, max_x, max_y)
    )
    passed = non_black_pixels >= 64 and len(colors) >= 3 and bounding_box is not None
    return PpmAudit(
        width=width,
        height=height,
        max_value=255,
        non_black_pixels=non_black_pixels,
        distinct_colors_lower_bound=len(colors),
        bounding_box=bounding_box,
        passed=passed,
    )
```

Accept only P6, max value 255, exact payload length, exact dimensions, at least
64 non-black pixels, at least three colors, and a non-empty foreground bounding
box. `distinct_colors_lower_bound` saturates at three because three is the
registered acceptance threshold; this bounds memory even for adversarial
pixel data.

- [ ] **Step 3: Write a fake-server QMP test**

Create a Unix socket server in a temporary directory. It must send a greeting,
require `qmp_capabilities`, require `screendump` with the exact output path, write
a synthetic PPM, and respond with `{"return": {}}`. Assert the client sends no
extra command and returns the captured bytes.

- [ ] **Step 4: Implement the QMP client**

```python
def capture_screendump(
    socket_path: Path,
    output_path: Path,
    *,
    capture_root: Path,
    timeout: float = 5.0,
) -> bytes:
    validate_capture_paths(
        socket_path=socket_path,
        output_path=output_path,
        capture_root=capture_root,
    )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(os.fspath(socket_path))
        greeting = _read_json_line(client)
        if "QMP" not in greeting:
            raise RuntimeError("QMP greeting is missing")
        _execute(client, "qmp_capabilities")
        _execute(client, "screendump", {"filename": os.fspath(output_path)})
    return output_path.read_bytes()
```

Require an absolute, non-symlinked `capture_root`; reject non-absolute paths,
either child path outside that root, events where a command response is
required, malformed JSON, QMP errors, and trailing data after the PPM payload.

- [ ] **Step 5: Run tests and commit**

```bash
docker exec codex-megrez-qemu-matrix python3 -m unittest \
  tools.riscv.tests.test_megrez_qemu_matrix.PpmAuditTests \
  tools.riscv.tests.test_megrez_qemu_matrix.QmpCaptureTests -v
git diff --check
git add tools/riscv/qemu_qmp.py tools/riscv/qemu_ppm.py \
  tools/riscv/tests/test_megrez_qemu_matrix.py
git commit -m "test(riscv): add bounded QMP display evidence"
```

### Task 4: Integrate display capture into the guarded lifecycle

**Files:**
- Modify: `tools/riscv/qemu_uboot_booti.py`
- Modify: `tools/riscv/qemu_uboot_execution.py`
- Modify: `tools/riscv/qemu_uboot_execution_io.py`
- Modify: `tools/riscv/qemu_uboot_session.py`
- Test: `tools/riscv/tests/test_qemu_uboot_booti.py`

- [ ] **Step 1: Write failing lifecycle tests**

Add tests proving that the terminal action runs after the completion marker but
before SIGTERM, that an action exception still reaps the process group, and that
capture arguments are rejected for `headless`.

```python
def test_terminal_action_runs_before_process_cleanup(self) -> None:
    action = mock.Mock()
    result, _received = self._run_serial_interaction(
        ready_chunks=(RX_READY_LINE + b"\n", b"/ # "),
        completion_chunks=(RX_ACK_LINE + b"\n",),
        terminal_action=action,
    )
    action.assert_called_once_with()
    self.assertTrue(result.cleanup_complete)

def test_terminal_action_failure_still_reaps_qemu(self) -> None:
    action = mock.Mock(side_effect=RuntimeError("capture failed"))
    with self.assertRaisesRegex(RuntimeError, "capture failed"):
        self._run_serial_interaction(
            ready_chunks=(RX_READY_LINE + b"\n", b"/ # "),
            completion_chunks=(RX_ACK_LINE + b"\n",),
            terminal_action=action,
        )
    self.assert_no_fake_qemu_processes()
```

Extend the existing `_run_serial_interaction` test helper with the optional
`terminal_action` keyword and pass it through to `run_serial_session`; do not
introduce a second fake-QEMU helper.

- [ ] **Step 2: Add the internal terminal-action hook**

Extend `run_serial_session` and `_run_serial_session` with
`terminal_action: Callable[[], None] | None = None`. After `protocol.run()`
returns successfully and before the `finally` cleanup, call the action exactly
once. Do not call it for a timeout, expected-negative run, or missing marker.

- [ ] **Step 3: Pin capture outputs and stage QMP writes**

Extend `_RunPaths`, `_resolve_run_paths`, and `_pin_run_outputs` with optional
`screenshot` and `display_audit` outputs. `open_execution_workspace` must create
a mode-0700 capture directory and expose run-private `qmp.sock` and `shot.ppm`
paths. QEMU writes only to that private staging path; after QEMU exits, publish
the PPM and JSON audit through `PinnedOutputDirectory.atomic_write`.

- [ ] **Step 4: Bind capture identity into the result**

Add to `PreparedRunResult`:

```python
device_set: str
screenshot_sha256: str | None
display_audit: dict[str, object] | None
```

Extend the CLI with `--device-set`, `--screenshot`, and `--display-audit`.
Require both display outputs for a framebuffer device set and forbid them for
headless sets. Pass the registered set into `qemu_argv` and `boot_commands`.

- [ ] **Step 5: Run regression tests and commit**

```bash
docker exec codex-megrez-qemu-matrix make test_riscv_uboot_booti_unit
git diff --check
git add tools/riscv/qemu_uboot_booti.py tools/riscv/qemu_uboot_execution.py \
  tools/riscv/qemu_uboot_execution_io.py tools/riscv/qemu_uboot_session.py \
  tools/riscv/tests/test_qemu_uboot_booti.py
git commit -m "feat(riscv): capture guarded framebuffer evidence"
```

### Task 5: Build a deterministic BusyBox shell initramfs

**Files:**
- Modify: `tools/riscv/make_qemu_uboot_initramfs.py`
- Create: `tools/riscv/nixos/busybox-static.nix`
- Create: `tools/riscv/nixos/build_busybox.sh`
- Create: `tools/riscv/megrez_qemu_busybox.py`
- Test: `tools/riscv/tests/test_megrez_qemu_matrix.py`

- [ ] **Step 1: Write failing archive and applet tests**

Require two builder outputs to be byte-identical. Parse the newc archive and
assert `/init`, `/bin/busybox`, `/bin/sh`, `/mnt`, `/share`, and the mountpoint
directories exist with exact types and modes. Assert the checked Nix output has
these applet links:

```python
REQUIRED_BUSYBOX_APPLETS = (
    "ash", "cat", "chmod", "dd", "ip", "ls", "mkdir", "mount",
    "mountpoint", "printf", "rm", "rmdir", "setsid", "sh", "sync",
    "test", "umount", "uname", "wc", "wget",
)
```

- [ ] **Step 2: Add the static BusyBox derivation and publisher**

Create `busybox-static.nix` with the image-pinned Nixpkgs package and an exact
version assertion:

```nix
let
  pkgs = import <nixpkgs> { };
  busybox = pkgs.pkgsCross.riscv64.busybox.override { enableStatic = true; };
in
assert busybox.version == "1.36.1";
busybox
```

`build_busybox.sh` accepts exactly one output path, runs
`nix-build --no-out-link tools/riscv/nixos/busybox-static.nix`, verifies every
name in `REQUIRED_BUSYBOX_APPLETS` exists below the returned `bin/`, verifies
`riscv64-linux-gnu-readelf -h` reports RISC-V and `readelf -l` has no `INTERP`
header, then copies `bin/busybox` to a same-directory `mktemp` file with mode
0755 and atomically renames it over only an absent or regular non-symlink
output. The script uses `set -euo pipefail`, resolves the repository with
`pwd -P`, and installs a trap that removes only its exact temporary file. Its
implementation is:

```bash
#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 OUTPUT" >&2
    exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "$script_dir/../../.." && pwd -P)"
output="$1"
if [[ "$output" != /* ]]; then
    output="$repo_root/$output"
fi
output_parent="$(dirname -- "$output")"
output_name="$(basename -- "$output")"
if [[ -z "$output_name" || "$output_name" == . || "$output_name" == .. ]]; then
    echo "BusyBox output must name a file" >&2
    exit 1
fi
mkdir -p -- "$output_parent"
output_parent="$(cd -- "$output_parent" && pwd -P)"
output="$output_parent/$output_name"
case "$output_parent/" in
    "$repo_root/target/"*) ;;
    *) echo "BusyBox output must be below the repository target directory" >&2; exit 1 ;;
esac
if [[ -L "$output" || (-e "$output" && ! -f "$output") ]]; then
    echo "BusyBox output must be absent or a regular non-symlink file" >&2
    exit 1
fi

nix_build="$(command -v nix-build)"
readelf="$(command -v riscv64-linux-gnu-readelf)"
store_path="$($nix_build --no-out-link "$script_dir/busybox-static.nix")"
busybox="$store_path/bin/busybox"
applets=(ash cat chmod dd ip ls mkdir mount mountpoint printf rm rmdir setsid sh sync test umount uname wc wget)
for applet in "${applets[@]}"; do
    [[ -e "$store_path/bin/$applet" ]] || {
        echo "static BusyBox is missing applet: $applet" >&2
        exit 1
    }
done
"$readelf" -h "$busybox" | grep -Eq 'Machine:.*RISC-V'
if "$readelf" -l "$busybox" | grep -q INTERP; then
    echo "BusyBox must be statically linked" >&2
    exit 1
fi

temporary="$(mktemp --tmpdir="$output_parent" ".${output_name}.tmp.XXXXXX")"
cleanup() {
    [[ -z "$temporary" ]] || rm -f -- "$temporary"
}
trap cleanup EXIT
install -m 0755 -- "$busybox" "$temporary"
mv -fT -- "$temporary" "$output"
temporary=""
```

- [ ] **Step 3: Publish supplied init payloads through the existing secure I/O**

Add a public helper without changing the marker builder behavior:

```python
def publish_initramfs(
    output: Path,
    *,
    init_payload: bytes,
    extra_entries: Sequence[InitramfsEntry],
) -> None:
    archive = make_newc_archive(init_payload, extra_entries=extra_entries)
    _write_output_atomic(output, gzip.compress(archive, compresslevel=9, mtime=0))
```

- [ ] **Step 4: Define the fixed init script**

Use this exact payload in `megrez_qemu_busybox.py`:

```sh
#!/bin/busybox sh
set -eu
/bin/busybox mount -t proc proc /proc
/bin/busybox mount -t sysfs sysfs /sys
/bin/busybox mount -t devtmpfs devtmpfs /dev
/bin/busybox chmod 1777 /tmp
exec /bin/busybox setsid -c /bin/busybox sh -c '
  exec </dev/ttyS0 >/dev/ttyS0 2>&1
  echo ASTERINAS_MEGREZ_QEMU_SHELL_READY_20260820
  exec /bin/busybox ash -i
'
```

Archive BusyBox as mode 0755, create `bin/sh -> busybox`, create `/mnt`,
`/mnt/scratch`, and `/share`, and add a deterministic 1280x64 XRGB8888
four-color payload at `/share/display-pattern.xrgb`. Its 5120-byte stride
matches both registered displays. Generate it with this fixed function:

```python
def display_pattern_xrgb8888() -> bytes:
    colors = (
        b"\x00\x00\xff\x00",
        b"\x00\xff\x00\x00",
        b"\xff\x00\x00\x00",
        b"\xff\xff\xff\x00",
    )
    return b"".join(
        colors[x // 320]
        for y in range(64)
        for x in range(1280)
    )
```

Refuse an empty, non-regular, symlinked, non-RISC-V, or dynamically linked
BusyBox input by parsing its ELF and program headers before reading it into the
archive.

- [ ] **Step 5: Run tests and commit**

```bash
docker exec codex-megrez-qemu-matrix python3 -m unittest \
  tools.riscv.tests.test_megrez_qemu_matrix.BusyBoxInitramfsTests -v
docker exec codex-megrez-qemu-matrix make test_riscv_uboot_booti_unit
git diff --check
git add tools/riscv/make_qemu_uboot_initramfs.py \
  tools/riscv/nixos/busybox-static.nix tools/riscv/nixos/build_busybox.sh \
  tools/riscv/megrez_qemu_busybox.py \
  tools/riscv/tests/test_megrez_qemu_matrix.py
git commit -m "feat(riscv): build deterministic BusyBox shell image"
```

### Task 6: Register the fixed shell interaction

**Files:**
- Modify: `tools/riscv/qemu_uboot_shell.py`
- Test: `tools/riscv/tests/test_megrez_qemu_matrix.py`
- Test: `tools/riscv/tests/test_qemu_uboot_booti.py`

- [ ] **Step 1: Write a failing interaction contract test**

```python
def test_basic_shell_interaction_is_fixed(self) -> None:
    interaction = interaction_by_name("megrez-qemu-basic-shell")
    self.assertEqual(
        interaction.ready_line,
        b"ASTERINAS_MEGREZ_QEMU_SHELL_READY_20260820",
    )
    self.assertEqual(interaction.input_steps[0].ready_token, b"/ # ")
    self.assertIn(b"mountpoint -q /proc", interaction.input_steps[0].input_bytes)
    self.assertIn(b"uname -a", interaction.input_steps[0].input_bytes)
    self.assertIn(b"/share/display-pattern.xrgb", interaction.input_steps[0].input_bytes)
    self.assertNotIn(b"saveenv", interaction.input_steps[0].input_bytes)
```

- [ ] **Step 2: Add the exact command program**

```python
BASIC_SHELL_COMMANDS = (
    b"mountpoint -q /proc && mountpoint -q /sys && mountpoint -q /dev && "
    b"[ -r /proc/1/status ] && [ \"$(pwd)\" = / ] && uname -a && ls / && cd /tmp && "
    b"[ \"$(pwd)\" = /tmp ] && "
    b"mkdir asterinas-basic && printf 'basic-shell\\n' > asterinas-basic/token && "
    b"[ \"$(cat asterinas-basic/token)\" = basic-shell ] && "
    b"rm asterinas-basic/token && rmdir asterinas-basic && cd / && "
    b"[ -c /dev/fb0 ] && dd if=/share/display-pattern.xrgb of=/dev/fb0 "
    b"bs=327680 count=1 && "
    b"printf 'ASTERINAS_MEGREZ_QEMU_BASIC_ACK_20260820\\n'\n"
)
```

Register it as one `SerialInputStep` waiting for `/ # ` and completing on the
ACK line.

- [ ] **Step 3: Run interaction/session tests and commit**

Run the new test, the existing serial-interaction controller tests, then the
complete U-Boot unit target.

Commit:

```bash
git add tools/riscv/qemu_uboot_shell.py \
  tools/riscv/tests/test_megrez_qemu_matrix.py \
  tools/riscv/tests/test_qemu_uboot_booti.py
git commit -m "test(riscv): gate the BusyBox serial shell"
```

## Increment 2: Artifact builder and primary basic gate

### Task 7: Snapshot separate Sv48 and Sv39 build artifacts

**Files:**
- Create: `tools/riscv/megrez_qemu_matrix.py`
- Test: `tools/riscv/tests/test_megrez_qemu_matrix.py`

- [ ] **Step 1: Write failing mocked-build tests**

Test that `build_artifacts` runs these exact commands in order and copies each
Image before the next build overwrites the OSDK output:

```python
expected = (
    ("make", "kernel", "TARGET_ARCH=riscv64", "CONSOLE=ttyS0", "SMP=4"),
    ("make", "kernel", "TARGET_ARCH=riscv64", "FEATURES=riscv_sv39_mode", "CONSOLE=ttyS0"),
    (
        "tools/riscv/nixos/build_busybox.sh",
        os.fspath(build_root / "busybox"),
    ),
)
```

Assert that zero, two, or empty kernel candidates fail before publication and
that the returned record contains full SHA-256 identities.

- [ ] **Step 2: Implement build records and kernel discovery**

```python
@dataclass(frozen=True)
class BuildArtifacts:
    source_commit: str
    sv48_kernel: Path
    sv39_kernel: Path
    busybox: Path
    initramfs: Path
    sha256: Mapping[str, str]
```

Accept only these OSDK output candidates:

```python
KERNEL_CANDIDATES = (
    Path("target/osdk/aster-kernel-osdk-bin.Image"),
    Path("target/osdk/aster-kernel/aster-kernel-osdk-bin.Image"),
)
```

Require exactly one non-empty regular candidate after each build. Set
`build_root = repo / "target" / "megrez-qemu-basic" / "build" / source_commit`
and snapshot to `build_root / "sv48.Image"` and `build_root / "sv39.Image"`
using an exclusive temporary file, `fsync`, and atomic replacement.

- [ ] **Step 3: Build BusyBox and the shell archive**

Call the checked-in BusyBox builder, then
`megrez_qemu_busybox.build(busybox, initramfs)`. Record all four identities in
`build.json`.

- [ ] **Step 4: Run tests and commit**

```bash
docker exec codex-megrez-qemu-matrix python3 -m unittest \
  tools.riscv.tests.test_megrez_qemu_matrix.BuildArtifactTests -v
git diff --check
git add tools/riscv/megrez_qemu_matrix.py \
  tools/riscv/tests/test_megrez_qemu_matrix.py
git commit -m "test(riscv): snapshot Megrez matrix build inputs"
```

### Task 8: Run the primary Sv48/Svade shell and display gate

**Files:**
- Modify: `tools/riscv/megrez_qemu_matrix.py`
- Modify: `Makefile`
- Test: `tools/riscv/tests/test_megrez_qemu_matrix.py`

- [ ] **Step 1: Write failing dry-run and lifecycle tests**

Require dry-run JSON to contain profile `megrez-sv48-svade-fast`, four harts,
device set `megrez-basic`, serial interaction `megrez-qemu-basic-shell`, one
private result directory, one run-owned U-Boot snapshot, and display outputs.
Mock preparation and runner subprocesses and assert preparation and U-Boot
snapshotting complete before execution.

- [ ] **Step 2: Implement run-owned layout**

```python
@dataclass(frozen=True)
class RunLayout:
    root: Path
    prepared: Path
    serial_log: Path
    marker_event: Path
    result_json: Path
    screenshot: Path
    display_audit: Path
    checksums: Path
```

Generate `YYYYMMDDTHHMMSSZ-<12-char-source>` IDs internally. Refuse an existing
or symlinked result root. Add `--result-path-file PATH`: after allocating the
run root, atomically write its repository-relative POSIX path to this
caller-owned regular-file output. Reject symlinks and parent directories
outside the repository. This remains valid on both sides of the Docker bind
mount and gives the final audit an exact path without a mutable `current`
symlink or directory guessing.

- [ ] **Step 3: Prepare and execute the exact primary profile**

Preparation environment:

```python
env.update(
    ASTERINAS_RISCV_BOOTI=str(artifacts.sv48_kernel),
    ASTERINAS_INITRAMFS=str(artifacts.initramfs),
    QEMU_UBOOT_PROFILE="megrez-sv48-svade-fast",
    QEMU_UBOOT_OUT_DIR=str(layout.prepared),
)
```

Runner command tuple:

```python
command = (
    sys.executable,
    "tools/riscv/qemu_uboot_booti.py",
    "run",
    "--profile", "megrez-sv48-svade-fast",
    "--device-set", "megrez-basic",
    "--serial-interaction", "megrez-qemu-basic-shell",
    "--uboot", os.fspath(layout.prepared / "u-boot"),
    "--boot-disk", os.fspath(layout.prepared / "boot.ext4"),
    "--manifest", os.fspath(layout.prepared / "artifacts.json"),
    "--dtb-audit", os.fspath(layout.prepared / "qemu-dtb-audit.json"),
    "--serial-log", os.fspath(layout.serial_log),
    "--marker-event", os.fspath(layout.marker_event),
    "--result", os.fspath(layout.result_json),
    "--screenshot", os.fspath(layout.screenshot),
    "--display-audit", os.fspath(layout.display_audit),
)
```

Immediately after preparation, query the registered profile's
`uboot-binary` field, resolve that exact regular file below
`target/qemu-uboot/cache/u-boot-build`, reject a symlink or empty file, and
atomically snapshot it as `layout.prepared / "u-boot"`. Record and recheck its
SHA-256 before and after the run exactly like the kernel, initramfs, DTB, and
boot disk inputs. Before launch, require `layout.prepared / "u-boot.config"` to
contain exact enabled lines for `CONFIG_CMD_PCI`, `CONFIG_VIDEO`, and
`CONFIG_VIDEO_BOCHS`. Never run QEMU directly from the mutable shared cache.

- [ ] **Step 4: Finalize evidence on every exit**

Use one outer `finally` to hash every regular evidence file except
`SHA256SUMS`, sorted by relative POSIX path. The primary run passes only if the
runner result is PASS, the shell ACK is unique, the framebuffer marker is
unique, the PPM audit passes, and cleanup is complete.

- [ ] **Step 5: Add Make targets and commit**

```make
.PHONY: test_riscv_megrez_qemu_basic_unit
test_riscv_megrez_qemu_basic_unit:
	@PYTHONPATH=tools/riscv python3 -m unittest \
		tools.riscv.tests.test_megrez_qemu_matrix -v

.PHONY: test_riscv_megrez_qemu_basic
test_riscv_megrez_qemu_basic: test_riscv_uboot_booti_unit test_riscv_megrez_qemu_basic_unit
	@PYTHONPATH=tools/riscv python3 tools/riscv/megrez_qemu_matrix.py run \
		--repo "$(CURDIR)" \
		--result-path-file "$(CURDIR)/target/megrez-qemu-basic/final-run-path.txt"
```

Run unit and dry-run targets, then commit:

```bash
git add Makefile tools/riscv/megrez_qemu_matrix.py \
  tools/riscv/tests/test_megrez_qemu_matrix.py
git commit -m "feat(riscv): add Megrez QEMU basic gate"
```

## Increment 3: Isolated device probes

### Task 9: Add block, entropy, and input probes

**Files:**
- Modify: `tools/riscv/qemu_uboot_devices.py`
- Modify: `tools/riscv/qemu_uboot_commands.py`
- Modify: `tools/riscv/qemu_uboot_shell.py`
- Modify: `tools/riscv/megrez_qemu_matrix.py`
- Test: `tools/riscv/tests/test_megrez_qemu_matrix.py`

- [ ] **Step 1: Write failing device argv tests**

Require exact isolated sets named `scratch-block`, `entropy`, and `input`.
Scratch block must render a snapshot drive plus
`virtio-blk-device,drive=scratch`; entropy must render only
`virtio-rng-device`; input must render only `virtio-keyboard-device` beyond the
base boot device.

- [ ] **Step 2: Add typed runtime paths**

Use the `RuntimeDevicePaths` type introduced in Task 1. Require `scratch_disk`
exactly for scratch-block and reject commas, symlinks, missing regular files,
and unused path fields.

- [ ] **Step 3: Add fixed guest interactions**

Scratch block:

```sh
mount -t ext2 /dev/vdb /mnt/scratch &&
printf 'scratch-block\n' > /mnt/scratch/token &&
[ "$(cat /mnt/scratch/token)" = scratch-block ] &&
rm /mnt/scratch/token && sync && umount /mnt/scratch &&
printf 'ASTERINAS_MEGREZ_SCRATCH_BLOCK_ACK_20260820\n'
```

Entropy:

```sh
dd if=/dev/hwrng of=/tmp/rng.bin bs=16 count=1 &&
[ "$(wc -c < /tmp/rng.bin)" -eq 16 ] && rm /tmp/rng.bin &&
printf 'ASTERINAS_MEGREZ_ENTROPY_ACK_20260820\n'
```

Input:

```sh
[ -c /dev/input/event0 ] &&
printf 'ASTERINAS_MEGREZ_INPUT_ACK_20260820\n'
```

Bind each probe to both functional and serial evidence in its registered
contract:

```python
DEVICE_SERIAL_MARKERS = MappingProxyType(
    {
        "scratch-block": (b"spawn the virtio-block thread",),
        "entropy": (b"device ID 4",),
        "input": (
            b"input device capabilities set: KEY=true",
            b"successfully connected handler class serial_keyboard to device",
        ),
    }
)
```

Require every registered marker at least once and the probe ACK exactly once.
The entropy marker is the existing virtio-MMIO device-type ID; the successful
bounded `/dev/hwrng` read is the second half of its driver evidence.

- [ ] **Step 4: Create deterministic ext2 scratch images**

Create a zeroed 32 MiB file with mode 0600, run `mkfs.ext2 -F -U
00000000-0000-0000-0000-000000000001`, verify with `debugfs -R stats`, and use
snapshot mode in QEMU.

- [ ] **Step 5: Run tests and commit**

Run the matrix unit target and U-Boot unit target.

Commit:

```bash
git add tools/riscv/qemu_uboot_devices.py tools/riscv/qemu_uboot_commands.py \
  tools/riscv/qemu_uboot_shell.py tools/riscv/megrez_qemu_matrix.py \
  tools/riscv/tests/test_megrez_qemu_matrix.py
git commit -m "test(riscv): add core QEMU device probes"
```

### Task 10: Add a deterministic virtio-network probe

**Files:**
- Modify: `tools/riscv/qemu_uboot_devices.py`
- Modify: `tools/riscv/qemu_uboot_shell.py`
- Modify: `tools/riscv/megrez_qemu_matrix.py`
- Test: `tools/riscv/tests/test_megrez_qemu_matrix.py`

- [ ] **Step 1: Write failing network contract tests**

Require `-netdev user,id=megreznet` and
`-device virtio-net-device,netdev=megreznet`. Test a local HTTP handler that
returns exactly one generated token and rejects every other path and method.

- [ ] **Step 2: Implement the run-private endpoint**

Bind an ephemeral host TCP port to `127.0.0.1`, keep the bound server alive for
the whole QEMU run, and derive the guest URL as
`f"http://10.0.2.2:{port}/asterinas-token"`. The validated port is generated by
the orchestrator and is not a CLI argument.

- [ ] **Step 3: Render a typed network interaction**

Construct, but do not accept from a caller:

```python
command = (
    "ip link set eth0 up && "
    "ip addr add 10.0.2.15/24 dev eth0 && "
    "ip route add default via 10.0.2.2 dev eth0 && "
    f"wget -qO /tmp/net-token http://10.0.2.2:{port}/asterinas-token && "
    f"[ \"$(cat /tmp/net-token)\" = {token} ] && rm /tmp/net-token && "
    "printf 'ASTERINAS_MEGREZ_NETWORK_ACK_20260820\\n'\n"
).encode()
```

Validate `port` as 1–65535 and `token` as 32 lowercase hexadecimal characters
before rendering. Require the serial markers
`virtio-net: send queue interrupt` and `virtio-net: recv queue interrupt` in
addition to the unique network ACK and the server's exactly-one successful
token request.

- [ ] **Step 4: Run tests and commit**

Commit after the matrix and U-Boot unit targets pass:

```bash
git add tools/riscv/qemu_uboot_devices.py tools/riscv/qemu_uboot_shell.py \
  tools/riscv/megrez_qemu_matrix.py \
  tools/riscv/tests/test_megrez_qemu_matrix.py
git commit -m "test(riscv): add isolated virtio network probe"
```

### Task 11: Add virtio-GPU and NVMe probes

**Files:**
- Modify: `tools/riscv/qemu_uboot_devices.py`
- Modify: `tools/riscv/qemu_uboot_shell.py`
- Modify: `tools/riscv/megrez_qemu_busybox.py`
- Modify: `tools/riscv/megrez_qemu_matrix.py`
- Test: `tools/riscv/tests/test_megrez_qemu_matrix.py`

- [ ] **Step 1: Write failing GPU and NVMe argv tests**

GPU must render `virtio-gpu-device,xres=1280,yres=800` with a private QMP socket
and no `bochs-display`. NVMe must render a snapshot drive and exactly
`nvme,serial=asterinas-nvme,drive=nvme0`.

- [ ] **Step 2: Add GPU evidence**

The GPU interaction requires `/dev/fb0`, writes the fixed
`/share/display-pattern.xrgb` 1280x64 four-color payload through `dd`, emits a fixed ACK,
and then captures a PPM. Audit that PPM against the registered 1280x800 GPU
dimensions. The GPU result is PASS only when the guest ACK, virtio-GPU
registration marker, QMP capture, and PPM audit all pass.

- [ ] **Step 3: Add NVMe scratch evidence**

Create a separate deterministic 32 MiB ext2 image and run:

```sh
mount -t ext2 /dev/nvme0n1 /mnt/scratch &&
printf 'nvme-scratch\n' > /mnt/scratch/token &&
[ "$(cat /mnt/scratch/token)" = nvme-scratch ] &&
rm /mnt/scratch/token && sync && umount /mnt/scratch &&
printf 'ASTERINAS_MEGREZ_NVME_ACK_20260820\n'
```

Also require the serial substrings
`Controller identified - Serial: asterinas-nvme` and
`Namespace 1: NSZE=` before accepting the unique NVMe ACK. Require the GPU
markers `virtio-gpu: RESOURCE_CREATE_2D ok`, `virtio-gpu: SET_SCANOUT ok`, and
`virtio-gpu: FLUSH ok` before accepting its capture.

- [ ] **Step 4: Keep executed failures visible**

The aggregate model must classify a launched GPU or NVMe failure as `FAIL` or
`ERROR`. It must not convert it to `UNSUPPORTED` or `SKIP`.

- [ ] **Step 5: Run tests and commit**

```bash
docker exec codex-megrez-qemu-matrix make test_riscv_megrez_qemu_basic_unit
docker exec codex-megrez-qemu-matrix make test_riscv_uboot_booti_unit
git diff --check
git add tools/riscv/qemu_uboot_devices.py tools/riscv/qemu_uboot_shell.py \
  tools/riscv/megrez_qemu_busybox.py tools/riscv/megrez_qemu_matrix.py \
  tools/riscv/tests/test_megrez_qemu_matrix.py
git commit -m "test(riscv): add GPU and NVMe probes"
```

## Increment 4: Compatibility, contract reporting, and handoff

### Task 12: Aggregate compatibility profiles and DW APB contract evidence

**Files:**
- Modify: `tools/riscv/megrez_qemu_matrix.py`
- Modify: `Makefile`
- Test: `tools/riscv/tests/test_megrez_qemu_matrix.py`

- [ ] **Step 1: Write a failing matrix composition test**

Require ordered entries:

```python
EXPECTED_ENTRIES = (
    "primary-svade",
    "svadu",
    "generic-sv39",
    "sifive-u",
    "scratch-block",
    "entropy",
    "network",
    "input",
    "gpu",
    "nvme",
    "dw-apb-uart-contract",
    "xhci-usb-keyboard",
)
```

The last entry is checked-in `UNSUPPORTED`; DW APB is `CONTRACT_ONLY`; every
other entry is `QEMU_EXECUTION`.

- [ ] **Step 2: Prepare compatibility boots with the correct Image**

Use this closed mapping:

```python
COMPATIBILITY_RUNS = (
    ("svadu", "megrez-sv48-svadu-fast", "sv48"),
    ("generic-sv39", "generic-sv39", "sv39"),
    ("sifive-u", "sifive-u-asterinas-smoke", "sv39"),
)
```

Use the Sv48 Image for both Megrez profiles. Use the Sv39 Image for
`generic-sv39` and `sifive-u-asterinas-smoke`. Each profile gets a distinct
prepared directory, run-owned U-Boot snapshot, and a headless shell
interaction. The snapshot source name comes from each profile's registered
`uboot-binary` field, so SiFive U freezes `u-boot.bin` while the virt profiles
freeze `u-boot`. Do not share writable boot disks between runs and never point a
runner at the shared U-Boot build directory.

- [ ] **Step 3: Run the filtered DW APB and UART-selection ktests**

Execute these two commands from the UART crate in order:

```python
contract_commands = (
    ("cargo", "osdk", "test", "dw_apb"),
    ("cargo", "osdk", "test", "uart_selection"),
)
contract_env = os.environ | {"OSDK_TARGET_ARCH": "riscv64"}
```

Run each with `cwd=repo / "kernel/comps/uart"`, `check=False`, a finite timeout,
and combined stdout/stderr. Capture both complete outputs, with the exact argv
as a header, in `contracts/dw-apb-uart.log`. Publish `dw-apb-uart.json` with
scope `CONTRACT_ONLY`, both commands and exit statuses, source commit, output
SHA-256, and PASS only when both processes return zero, both ktest runs report
`KTEST_EXIT=0`, and the output includes
`dw_apb_accepts_the_megrez_contract` plus
`uart_selection_checks_the_complete_compatible_list`. This binds the fake-MMIO
DW APB behavior, reviewed access layout, compatible classification, and
firmware-selected stdout path without claiming a QEMU DW APB execution.

- [ ] **Step 4: Implement aggregate verdicts**

```python
class EvidenceScope(str, Enum):
    QEMU_EXECUTION = "QEMU_EXECUTION"
    CONTRACT_ONLY = "CONTRACT_ONLY"
    UNSUPPORTED = "UNSUPPORTED"
    PHYSICAL_UNTESTED = "PHYSICAL_UNTESTED"

class MatrixStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    UNSUPPORTED = "UNSUPPORTED"
```

`matrix.json` returns PASS only when every launched entry passes and the single
registered unsupported entry is exactly xHCI/USB keyboard. Always append a
`PHYSICAL_UNTESTED` summary for EIC7700 clocks, reset, cache controller, DWC3,
and HDMI scanout.

- [ ] **Step 5: Run unit tests and commit**

```bash
docker exec codex-megrez-qemu-matrix make test_riscv_megrez_qemu_basic_unit
docker exec codex-megrez-qemu-matrix make test_riscv_uboot_booti_unit
git diff --check
git add Makefile tools/riscv/megrez_qemu_matrix.py \
  tools/riscv/tests/test_megrez_qemu_matrix.py
git commit -m "test(riscv): aggregate the Megrez QEMU matrix"
```

### Task 13: Document, run, review, and integrate

**Files:**
- Modify: `tools/riscv/README.md`
- Modify: `docs/porting/README.md`
- Modify: `docs/superpowers/plans/2026-08-20-megrez-qemu-basic-device-matrix.md`

- [ ] **Step 1: Document the exact operator commands**

Add container prerequisites, the one-command gate, the unit-only target, output
layout, expected runtime, and status meanings. State prominently:

```text
This matrix is a QEMU contract approximation. It does not emulate the EIC7700
SoC and does not establish current Milk-V Megrez board support.
```

- [ ] **Step 2: Run formatting and static checks**

```bash
docker exec codex-megrez-qemu-matrix make check
docker exec codex-megrez-qemu-matrix make docs
docker exec codex-megrez-qemu-matrix make test_riscv_uboot_booti_unit
docker exec codex-megrez-qemu-matrix make test_riscv_megrez_qemu_basic_unit
bash -n tools/riscv/nixos/build_busybox.sh
git diff --check
```

Expected: every command returns zero.

- [ ] **Step 3: Run the complete local matrix**

```bash
docker exec codex-megrez-qemu-matrix make test_riscv_megrez_qemu_basic
```

Expected: primary Svade, Svadu, generic Sv39, SiFive U, block, entropy,
network, input, GPU, NVMe, and DW APB contract entries pass; xHCI/USB keyboard
is the single registered unsupported entry; `matrix.json` reports PASS. The
command atomically records the exact repository-relative run path in
`target/megrez-qemu-basic/final-run-path.txt`.

- [ ] **Step 4: Audit final evidence**

Verify:

```bash
read -r matrix_run_rel < target/megrez-qemu-basic/final-run-path.txt
case "$matrix_run_rel" in target/megrez-qemu-basic/results/*) ;; *) exit 1 ;; esac
matrix_run_dir="$PWD/$matrix_run_rel"
test -d "$matrix_run_dir"
python3 -m json.tool "$matrix_run_dir/matrix.json"
(cd "$matrix_run_dir" && sha256sum -c SHA256SUMS)
if pgrep -af '[q]emu-system-riscv64'; then exit 1; fi
git status --short
```

Expected: JSON is valid, every checksum matches, no matrix-owned QEMU process
remains, and only intended source/document changes are tracked. The path file
must point below the run-owned results directory; do not use a `current`
symlink.

- [ ] **Step 5: Request code review and fix findings**

Review the complete diff against the design, focusing on output-directory
safety, QMP path handling, input identity, process cleanup, legacy argv drift,
status inflation, and physical-board claim boundaries. Add regression tests for
every accepted finding and rerun the affected matrix layer.

- [ ] **Step 6: Commit the verified documentation**

```bash
git add tools/riscv/README.md docs/porting/README.md \
  docs/superpowers/plans/2026-08-20-megrez-qemu-basic-device-matrix.md
git commit -m "docs(riscv): record the Megrez QEMU device matrix"
```

- [ ] **Step 7: Finish the branch locally**

Use `superpowers:verification-before-completion`, then
`superpowers:finishing-a-development-branch`. Push a normal `codex/` branch,
open or update the PR, and merge only after the exact head has the complete
local evidence and review verdict. Do not wait for remote CI.
