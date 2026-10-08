# Asterinas Debug Root Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit, one-boot-only root serial console to the Asterinas Debian systemd desktop without adding a password, permanent sudo policy, or rootfs rebuild.

**Architecture:** Stage1 parses `--debug-console=root` only beside `--root-init=systemd`, mounts the unchanged Debian root, and creates a runtime systemd unit plus console-getty override under the `/run` tmpfs. A small shared Python protocol verifies the resulting root shell in QEMU and through the existing paced Megrez serial runner; normal boots never create the runtime unit.

**Tech Stack:** Static C11 Stage1, systemd runtime units, Bash, Python 3 standard library, `unittest`, existing Asterinas QEMU gate primitives, U-Boot paced serial, RISC-V SMP=4.

---

## File map

- Create `tools/riscv/debian/rootfs/stage1_debug_console.h`: narrow C API for ephemeral runtime files.
- Create `tools/riscv/debian/rootfs/stage1_debug_console.c`: safe `/run` directory/file/link creation and exact unit contents.
- Modify `tools/riscv/debian/rootfs/stage1_init.c`: option parsing and a debug-console handoff step after `/run` mount.
- Modify `tools/riscv/debian/rootfs/build_stage1.sh`: compile the new C unit into the existing initramfs.
- Create `tools/riscv/debian/rootfs/debug_console_protocol.py`: shared framed serial commands and fail-closed classification.
- Create `tools/riscv/debian/rootfs/debug_console_qemu_gate.py`: bounded graphical QEMU boot on the desktop M5 lifecycle.
- Create `tools/riscv/tests/test_debian_debug_console.py`: runtime-file, protocol, and QEMU-adapter tests.
- Modify `tools/riscv/tests/test_debian_rootfs.py`: Stage1 parser, handoff, and builder regressions.
- Modify `tools/riscv/megrez_board_session.py`: closed debug-root final profile and command phase.
- Modify `tools/riscv/tests/test_megrez_board_session.py`: parser, command, timeout, and recovery tests.
- Modify `Makefile`: focused host and QEMU targets.
- Modify `tools/riscv/debian/rootfs/README.md` and `tools/riscv/README.md`: bounded gate and handoff commands.
- Create `docs/porting/evidence/2026-09-07-megrez-debug-root-console.md`: create only after real evidence exists.

### Task 1: Freeze the Stage1 option contract

**Files:**
- Modify: `tools/riscv/debian/rootfs/stage1_init.c:84-205`
- Modify: `tools/riscv/tests/test_debian_rootfs.py:520-585`

- [ ] **Step 1: Add failing native Stage1 cases**

Extend the case tuple in `test_native_self_test_covers_discovery_and_handoff_failures`:

```python
"root-init-systemd-debug-root",
"root-init-debug-with-interactive",
"root-init-debug-duplicate",
"root-init-debug-unknown",
"root-init-debug-control-character",
```

The positive case requires systemd mode and `debug_root_console == 1`.
Every negative case requires `parse_root_init` to return non-zero.

- [ ] **Step 2: Run the focused test and observe RED**

```bash
python3 -m unittest \
  tools.riscv.tests.test_debian_rootfs.DebianStage1Tests.test_native_self_test_covers_discovery_and_handoff_failures -v
```

Expected: FAIL because the new case names are unknown.

- [ ] **Step 3: Replace the mode-only parse result with an exact configuration**

Add this production shape to `stage1_init.c`:

```c
struct RootInitConfig {
    enum RootInitMode mode;
    int debug_root_console;
};

static int parse_root_init(int argc, char **argv,
                           struct RootInitConfig *config)
{
    config->mode = ROOT_INIT_INTERACTIVE;
    config->debug_root_console = 0;
    int selector_seen = 0;
    int debug_seen = 0;
    for (int index = 1; index < argc; ++index) {
        if (strcmp(argv[index], "--root-init=interactive") == 0 ||
            strcmp(argv[index], "--root-init=systemd") == 0) {
            if (selector_seen++)
                return -1;
            config->mode = strcmp(argv[index], "--root-init=systemd") == 0
                               ? ROOT_INIT_SYSTEMD : ROOT_INIT_INTERACTIVE;
        } else if (strcmp(argv[index], "--debug-console=root") == 0) {
            if (debug_seen++)
                return -1;
            config->debug_root_console = 1;
        } else {
            return -1;
        }
    }
    return config->debug_root_console && config->mode != ROOT_INIT_SYSTEMD
               ? -1 : 0;
}
```

Store the configuration in `ProductionContext`. Add the five exact C
self-test branches; accept no abbreviations or alternate values.

- [ ] **Step 4: Run the focused test and observe GREEN**

Run Step 2 again. Expected: the complete Stage1 case table passes.

- [ ] **Step 5: Commit the parser contract**

```bash
git add tools/riscv/debian/rootfs/stage1_init.c \
  tools/riscv/tests/test_debian_rootfs.py
git commit -m "feat(riscv): parse opt-in debug root console"
```

### Task 2: Materialize the ephemeral systemd console

**Files:**
- Create: `tools/riscv/debian/rootfs/stage1_debug_console.h`
- Create: `tools/riscv/debian/rootfs/stage1_debug_console.c`
- Modify: `tools/riscv/debian/rootfs/stage1_init.c:87-345,893-970`
- Modify: `tools/riscv/debian/rootfs/build_stage1.sh:45-115`
- Modify: `tools/riscv/tests/test_debian_rootfs.py:488-790`

- [ ] **Step 1: Add failing filesystem harness tests**

Compile a host harness against `stage1_debug_console.c`:

```c
#include "stage1_debug_console.h"
int main(int argc, char **argv)
{
    return argc == 2 ? stage1_prepare_debug_console(argv[1]) : 2;
}
```

After running it against a temporary root, assert the exact relative service
link, `TTYPath=/dev/ttyS0`, `StandardInput=tty-force`, `Restart=always`, the
runtime UID marker, and the negated console-getty condition. A second test
pre-creates a symlink at one destination and requires failure without mutation.

- [ ] **Step 2: Run the filesystem tests and observe RED**

```bash
python3 -m unittest \
  tools.riscv.tests.test_debian_rootfs.DebianStage1Tests.test_debug_console_runtime_tree_is_exact \
  tools.riscv.tests.test_debian_rootfs.DebianStage1Tests.test_debug_console_rejects_symlink_destination -v
```

Expected: ERROR because `stage1_debug_console.[ch]` do not exist.

- [ ] **Step 3: Implement no-follow runtime-tree creation**

Expose only:

```c
#ifndef ASTERINAS_STAGE1_DEBUG_CONSOLE_H
#define ASTERINAS_STAGE1_DEBUG_CONSOLE_H
int stage1_prepare_debug_console(const char *root);
#endif
```

Use `lstat` for existing directories, `open` with
`O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC` for files, complete-write loops,
and an exact relative symlink. Generate this unit:

```ini
[Unit]
Description=Asterinas opt-in root serial console
After=systemd-user-sessions.service
ConditionPathExists=/run/asterinas-debug-console.enabled

[Service]
Type=simple
ExecStart=/bin/bash --noprofile --rcfile /run/asterinas-debug-console.bashrc -i
TTYPath=/dev/ttyS0
StandardInput=tty-force
StandardOutput=tty
StandardError=tty
TTYReset=yes
TTYVHangup=yes
Restart=always
RestartSec=1
```

The rc file must derive the UID:

```bash
printf 'ASTERINAS_DEBUG_CONSOLE_READY uid=%s\n' "$(id -u)"
PS1='root@asterinas-debug:\w# '
```

- [ ] **Step 4: Insert a distinct handoff step**

Add `HANDOFF_PREPARE_DEBUG_CONSOLE` only when debug is enabled, after
`HANDOFF_MOUNT_RUN` and before `HANDOFF_MOUNT_TMP`. The production switch calls
`stage1_prepare_debug_console("/newroot")`; report
`handoff-enter/done action=debug-console` and fail with `debug-console` on any
error. Existing interactive and normal-systemd sequences remain byte-for-byte
equivalent at the step level.

- [ ] **Step 5: Compile both C files everywhere**

```bash
"$COMPILER" -std=c11 -O2 -static -no-pie -Wall -Wextra -Werror \
    "$SCRIPT_DIR/stage1_init.c" "$SCRIPT_DIR/stage1_debug_console.c" \
    -o "$STAGE/init"
```

Update the Python `compile_stage1` helper the same way. `--print-entries` must
remain exactly `.` and `init`.

- [ ] **Step 6: Run Stage1 and builder tests**

```bash
python3 -m unittest tools.riscv.tests.test_debian_rootfs.DebianStage1Tests -v
```

Expected: all tests pass, including normal handoff without debug preparation.

- [ ] **Step 7: Commit the runtime console**

```bash
git add tools/riscv/debian/rootfs/stage1_debug_console.c \
  tools/riscv/debian/rootfs/stage1_debug_console.h \
  tools/riscv/debian/rootfs/stage1_init.c \
  tools/riscv/debian/rootfs/build_stage1.sh \
  tools/riscv/tests/test_debian_rootfs.py
git commit -m "feat(riscv): inject ephemeral systemd root console"
```

### Task 3: Define a shared serial acceptance protocol

**Files:**
- Create: `tools/riscv/debian/rootfs/debug_console_protocol.py`
- Create: `tools/riscv/tests/test_debian_debug_console.py`
- Modify: `Makefile`

- [ ] **Step 1: Write failing pure classifier tests**

Use a fixed 32-hex nonce and require five framed commands. The passing fixture
must produce:

```python
DebugConsoleEvidence(
    uid=0,
    pid1="systemd",
    root_device="/dev/mmcblk0p2",
    root_filesystem="ext2",
    graphical_state="active",
    desktop_state="active",
)
```

Separate tests reject UID 1000, a non-systemd PID 1, wrong Megrez root
partition, non-ext2 root, inactive graphical or desktop units, non-zero command
status, duplicate/reordered markers, stale nonce markers, ANSI/OSC control
input, and transcripts over 8 MiB.

- [ ] **Step 2: Run the new tests and observe RED**

```bash
python3 -m unittest tools.riscv.tests.test_debian_debug_console -v
```

Expected: import failure because `debug_console_protocol.py` is absent.

- [ ] **Step 3: Implement fixed framed probes**

Create frozen `DebugConsoleCommand` and `DebugConsoleEvidence` dataclasses.
`debug_console_commands(nonce)` returns fixed commands for:

```bash
id -u
tr -d '\n' </proc/1/comm; printf '\n'
awk '$2 == "/" { print $1, $3; exit }' /proc/mounts
systemctl is-active graphical.target
systemctl is-active asterinas-desktop-m5.service
```

Each payload prints exact begin, status, and end lines containing the validated
nonce. Accept `/dev/vd[a-z]` for QEMU or exactly `/dev/mmcblk0p2` for Megrez,
and require filesystem `ext2`.

`run_debug_console_phase(serial, deadline, nonce, *, ready_seen=False)` waits for
`ASTERINAS_DEBUG_CONSOLE_READY uid=0`, sends each fixed command, drains its
framed response, and calls the pure classifier. `ready_seen=True` is permitted
only for the physical adapter that has already consumed the exact readiness
line while enforcing ordered boot milestones. It sends no reboot, password,
caller-controlled shell text, or mutating command.

- [ ] **Step 4: Run protocol tests and static checks**

```bash
python3 -m unittest tools.riscv.tests.test_debian_debug_console -v
python3 -m py_compile \
  tools/riscv/debian/rootfs/debug_console_protocol.py \
  tools/riscv/tests/test_debian_debug_console.py
ruff check tools/riscv/debian/rootfs/debug_console_protocol.py \
  tools/riscv/tests/test_debian_debug_console.py
```

Expected: every command exits 0.

- [ ] **Step 5: Add and run a focused Make target**

```make
.PHONY: test_riscv_debian_debug_console
test_riscv_debian_debug_console:
	@python3 -m unittest \
		tools.riscv.tests.test_debian_debug_console \
		tools.riscv.tests.test_debian_rootfs.DebianStage1Tests -v
```

Run `make test_riscv_debian_debug_console`; expect all tests to pass.

- [ ] **Step 6: Commit the protocol**

```bash
git add Makefile tools/riscv/debian/rootfs/debug_console_protocol.py \
  tools/riscv/tests/test_debian_debug_console.py
git commit -m "test(riscv): define debug root console protocol"
```

### Task 4: Gate the feature in graphical QEMU

**Files:**
- Create: `tools/riscv/debian/rootfs/debug_console_qemu_gate.py`
- Modify: `tools/riscv/tests/test_debian_debug_console.py`
- Modify: `Makefile`

- [ ] **Step 1: Write failing QEMU-adapter tests**

Require `DebugConsoleQemuOperations` to append the debug selector after the
existing systemd selector, call the established desktop M5 protocol first,
then call the debug protocol exactly once. A failed debug classification must
raise `GateFailure`; publication must include structured debug evidence; and
the existing cleanup must still drain serial and terminate QEMU.

```python
self.assertTrue(operations.BOOTARGS.endswith(
    "-- --root-init=systemd --debug-console=root"
))
operations.run_protocol(session, config)
base_protocol.assert_called_once_with(session, config)
debug_protocol.assert_called_once()
```

- [ ] **Step 2: Run adapter tests and observe RED**

```bash
python3 -m unittest \
  tools.riscv.tests.test_debian_debug_console.DebugConsoleQemuAdapterTests -v
```

Expected: import or attribute failure for the absent QEMU adapter.

- [ ] **Step 3: Subclass the existing desktop M5 lifecycle**

Implement `DebugConsoleQemuOperations(DesktopM5QemuOperations)`:

```python
ARTIFACT_PREFIX = "debug-root-console"
BOOTARGS = DESKTOP_M5_QEMU_BOOTARGS + " --debug-console=root"

def run_protocol(self, session, config):
    super().run_protocol(session, config)
    nonce = secrets.token_hex(16)
    self.debug_evidence = run_debug_console_phase(
        session["serial"], time.monotonic() + config.command_timeout, nonce
    )
```

Override accepted identities to allow schema-5 `desktop-m5-network` and
schema-7 `browser-web`. Reuse the framebuffer screenshot, fixture server,
pinned output, signal cleanup, and desktop classifier. Publish the debug
evidence alongside the existing serial log and screenshot metadata.

- [ ] **Step 4: Add the QEMU Make target**

Add `test_riscv_debian_debug_console_qemu_gate` with the same required artifact
variables as the desktop M5 QEMU gate, invoking the new module. The existing
backend fixes QEMU and DTB CPU count to four.

- [ ] **Step 5: Run the real QEMU gate**

Build Stage1 only:

```bash
make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
tools/riscv/debian/rootfs/build_stage1.sh \
  target/debian-riscv/debug-console/stage1/initramfs.cpio
make build_riscv_debian_browser_web_dev_overlay
DEBUG_ROOTFS="$PWD/target/dev-overlays/browser-web/rootfs"
make test_riscv_debian_debug_console_qemu_gate \
  DEBIAN_KERNEL="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  DEBIAN_UBOOT="$PWD/target/qemu-uboot/cache/u-boot-build/u-boot" \
  DEBIAN_DTB="$PWD/target/qemu-uboot/debian-root/qemu-virt.dtb" \
  DEBIAN_STAGE1_INITRAMFS="$PWD/target/debian-riscv/debug-console/stage1/initramfs.cpio" \
  DEBIAN_ROOT_IMAGE="$DEBUG_ROOTFS/debian-root.ext2" \
  DEBIAN_ROOT_MANIFEST="$DEBUG_ROOTFS/rootfs-manifest.json" \
  DEBIAN_PACKAGES_LOCK="$DEBUG_ROOTFS/packages.lock" \
  DEBIAN_PACKAGE_CHECKSUMS="$DEBUG_ROOTFS/source-metadata/package-checksums" \
  DEBIAN_DEBUG_CONSOLE_QEMU_GATE_OUTPUT="$PWD/target/debian-riscv/debug-console/qemu"
```

The earlier review run used
`/tmp/asterinas-xkb-contract-overlay-v4-20260907/rootfs`, and the final run used
`/tmp/asterinas-xkb-contract-overlay-v5-20260907/rootfs`. These historical paths
are evidence metadata, not the executable build guide. The generated development
overlay has manifest identity schema 7, profile `browser-web`; its overlay
manifest records the XKB-cache and `runuser` fixes tested on Megrez.

Expected: `result.json` reports pass, UID 0, PID 1 `systemd`, ext2 root, active
graphical and desktop services, and a non-empty rendered screenshot.

- [ ] **Step 6: Commit the QEMU gate**

```bash
git add Makefile tools/riscv/debian/rootfs/debug_console_qemu_gate.py \
  tools/riscv/tests/test_debian_debug_console.py
git commit -m "test(riscv): gate debug root console in QEMU"
```

### Task 5: Integrate the guarded Megrez serial path

**Files:**
- Modify: `tools/riscv/megrez_board_session.py:55-70,760-1038`
- Modify: `tools/riscv/tests/test_megrez_board_session.py`
- Modify: `tools/riscv/README.md`

- [ ] **Step 1: Write failing board-profile tests**

Add `debug-root-console` to `FINAL_MILESTONE_MARKERS` and require:

```python
self.assertEqual(
    board.FINAL_MILESTONE_MARKERS["debug-root-console"],
    "ASTERINAS_DEBUG_CONSOLE_READY uid=0",
)
```

Mock the debug protocol and prove it runs only after ordered `Enter riscv_boot`
and readiness milestones. Reject debug profiles whose bootargs lack either
`--root-init=systemd` or `--debug-console=root`. Prove timeout/failure returns
non-zero, closes the serial descriptor, and retains the transcript.

- [ ] **Step 2: Run focused tests and observe RED**

```bash
PYTHONPATH=tools/riscv python3 -m unittest -v \
  tools.riscv.tests.test_megrez_board_session
```

Expected: FAIL because the final profile and command phase are absent.

- [ ] **Step 3: Add the final profile and command phase**

Import `run_debug_console_phase` and `SerialConsole`. After the readiness
milestone, wrap the still-owned serial descriptor, execute the fixed read-only
protocol with `ready_seen=True` and a fresh host nonce, and append the complete
command exchange to the existing serial log before waiting for recovery. Print
one additional canonical JSON evidence line only for this profile; preserve
the existing milestone JSON and output behavior for every other profile.

Keep paced U-Boot writes, CRC checks, framebuffer DT patch, single serial
ownership, and `finally` cleanup. Add no reset command, `saveenv`, password
handling, boot retry, or caller-provided guest command.

- [ ] **Step 4: Run board-session regressions**

```bash
PYTHONPATH=tools/riscv python3 -m unittest -v \
  tools.riscv.tests.test_megrez_board_session \
  tools.riscv.tests.test_megrez_gmac_gate
```

Expected: all tests pass.

- [ ] **Step 5: Document bounded and handoff bootargs**

Document:

- bounded acceptance: `console=tty0 console=ttyS0 ... asterinas.reboot_after=120 -- --root-init=systemd --debug-console=root` plus `--require-recovery`;
- operator handoff after a passing gate: the same arguments without the
  recovery token, followed by `picocom` on the serial-by-id path.

State that this prompt is root under Asterinas, whereas `debian / debian` is
the unrelated RockOS recovery account.

- [ ] **Step 6: Commit the board integration**

```bash
git add tools/riscv/megrez_board_session.py \
  tools/riscv/tests/test_megrez_board_session.py tools/riscv/README.md
git commit -m "feat(riscv): drive Megrez debug root console"
```

### Task 6: Prove isolation and physical acceptance

**Files:**
- Modify: `tools/riscv/debian/rootfs/README.md`
- Create: `docs/porting/evidence/2026-09-07-megrez-debug-root-console.md`

- [ ] **Step 1: Run the complete focused host suite**

```bash
make test_riscv_debian_debug_console
PYTHONPATH=tools/riscv python3 -m unittest -v \
  tools.riscv.tests.test_megrez_board_session \
  tools.riscv.tests.test_megrez_gmac_gate
git diff --check
```

Expected: zero failures and zero whitespace errors.

- [ ] **Step 2: Prove the negative QEMU boot**

Run the graphical root without `--debug-console=root`. The existing desktop
gate must pass; its complete serial log must contain zero
`ASTERINAS_DEBUG_CONSOLE_READY` and zero `action=debug-console` markers.

- [ ] **Step 3: Stage only small boot artifacts through RockOS**

Copy the new Stage1 and current kernel to unique `/boot` filenames, call
`sync`, and verify SHA-256 plus U-Boot CRC32 on host and board. Do not replace
extlinux defaults or modify the installed `ASTER_BROWSERWEB` partition.

- [ ] **Step 4: Run one bounded physical gate**

Use the serial-by-id device, SMP=4 kernel, framebuffer patch, exact CRC map,
debug profile, and 120-second recovery. Require:

```text
DEBIAN_STAGE1_PROGRESS step=probe-complete result=match
DEBIAN_STAGE1_PROGRESS step=handoff-done action=debug-console
ASTERINAS_DEBUG_CONSOLE_READY uid=0
```

The framed probes must report PID 1 `systemd`, root `/dev/mmcblk0p2 ext2`, and
active graphical and desktop services. Require a fresh OpenSBI/U-Boot epoch,
then boot RockOS through its explicit extlinux entry. Verify SSH reports kernel
`6.6.87`; do not request a physical reset.

- [ ] **Step 5: Perform an operator handoff boot**

Only after the bounded gate passes, boot without an automatic recovery token.
Reopen the serial console and execute:

```bash
id
cat /proc/1/comm
systemctl is-active graphical.target asterinas-desktop-m5.service
```

Expected: UID 0, PID 1 `systemd`, both units active, and the HDMI desktop still
responsive. Leave the board at the root prompt unless RockOS recovery is
requested.

- [ ] **Step 6: Record exact evidence and limitations**

Write the evidence document with commit IDs, artifact SHA-256/CRC32, bootargs,
timestamps, command results, recovery state, serial-log SHA-256, and the
boundary that this proves physical-serial root access rather than remote
authentication.

- [ ] **Step 7: Run completion verification and commit evidence**

```bash
make test_riscv_debian_debug_console
PYTHONPATH=tools/riscv python3 -m unittest -v \
  tools.riscv.tests.test_megrez_board_session \
  tools.riscv.tests.test_megrez_gmac_gate
git diff --check
git status --short
```

Inspect every reported path and stage only debug-console work and evidence:

```bash
git add tools/riscv/debian/rootfs/README.md \
  docs/porting/evidence/2026-09-07-megrez-debug-root-console.md
git commit -m "docs(riscv): record Megrez debug root console"
```
