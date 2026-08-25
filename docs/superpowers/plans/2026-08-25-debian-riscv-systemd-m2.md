# Debian RISC-V systemd M2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Boot a separately signed Debian Trixie systemd profile twice on Asterinas, prove normal userspace reboot and persistent-root state in QEMU/SMP=4, then repeat the gate on Megrez.

**Architecture:** Preserve the M1 schema and artifacts. Add an explicit schema-v2 `systemd-m2` profile, an exact Stage1 init selector, a deterministic evidence service, and a bounded systemd two-boot gate that reuses the existing descriptor-pinned artifact and process lifecycle primitives.

**Tech Stack:** Python 3 standard library, Bash, C11/Linux UAPI, Debian `gpgv`/APT/debootstrap, ext2/e2fsprogs, Asterinas RISC-V, U-Boot, QEMU, systemd.

---

### Task 1: Freeze the M2 profile and schema-v2 contract

**Files:**
- Create: `tools/riscv/debian/rootfs/profiles.py`
- Modify: `tools/riscv/debian/rootfs/contract.py`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`

- [x] **Step 1: Write the failing profile and compatibility tests**

Add `DebianSystemdM2ProfileTests` that requires:

```python
profile = get_profile("systemd-m2")
self.assertEqual(profile.root_label, "ASTER_DEBIANM2")
self.assertEqual(profile.root_uuid, "4a5d8b91-2189-44fa-a908-ae88dc76f2a1")
self.assertEqual(
    profile.requested_packages,
    ("bash", "ca-certificates", "coreutils", "dbus", "procps",
     "systemd-sysv", "util-linux"),
)
self.assertRaisesRegex(ValueError, "unknown rootfs profile", get_profile, "desktop")
```

Also require schema-v1 M1 manifests to round-trip unchanged and schema-v2
manifests to contain exact `profile: "systemd-m2"`, the M2 label/UUID, and
locked `systemd`, `systemd-sysv`, and `dbus` package identities.

- [x] **Step 2: Run the focused RED**

Run:

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_rootfs.DebianSystemdM2ProfileTests -v
```

Expected: fail because `tools.riscv.debian.rootfs.profiles` and schema v2 do
not exist.

- [x] **Step 3: Implement the immutable profile API**

Create a frozen `RootfsProfile` and exact registry:

```python
@dataclass(frozen=True)
class RootfsProfile:
    name: str
    schema_version: int
    root_label: str
    root_uuid: str
    requested_packages: tuple[str, ...]
    identity_packages: tuple[str, ...]

def get_profile(name: str) -> RootfsProfile:
    try:
        return _PROFILES[name]
    except KeyError as error:
        raise ValueError(f"unknown rootfs profile: {name}") from error
```

Teach `contract.py` to dispatch schema 1 to the existing M1 validator and
schema 2 to the M2 validator. Do not relax duplicate-key, same-open hash,
downloaded-package/full-lock equality, URL, release, architecture, ext2, size,
or block-size validation.

- [x] **Step 4: Run focused GREEN and compatibility checks**

Run the focused test plus the existing manifest/contract classes. Expected:
all pass, and the existing 83-row M1 artifact verifies quietly.

- [x] **Step 5: Commit**

```bash
git add tools/riscv/debian/rootfs/profiles.py \
  tools/riscv/debian/rootfs/contract.py tools/riscv/tests/test_debian_rootfs.py
git commit -m "build(riscv): define Debian systemd M2 profile"
```

### Task 2: Build the signed M2 root and evidence service

**Files:**
- Create: `tools/riscv/debian/rootfs/systemd_m2_evidence.sh`
- Modify: `tools/riscv/debian/rootfs/build_rootfs.sh`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`

- [x] **Step 1: Write failing builder-profile tests**

Require `build_rootfs.sh --profile systemd-m2 --print-packages` to print the
profile's exact sorted package set; reject unknown/duplicate profiles before
network or output mutation; keep no-option behavior identical to M1. Test a
fake staged root and assert the M2 unit, enablement symlink, evidence script,
label, UUID, and output directory are exact.

- [x] **Step 2: Write failing evidence-script state tests**

Run the script against a temporary fake root with injected command paths. The
first run must write `1`, fsync through `sync`, emit
`DEBIAN_SYSTEMD_M2_READY boot=1`, and invoke reboot. The second run must write
`2` and emit `DEBIAN_SYSTEMD_M2_PASS`. Invalid/missing identity and counters
other than zero/one must emit a stable FAIL reason and never reboot.

- [x] **Step 3: Run RED**

Expected: profile CLI and evidence script are absent.

- [x] **Step 4: Implement profile-driven assembly**

Add `--profile` without changing the default. Resolve package/label/UUID/output
identity through `profiles.py`; retain plain `gpgv`, retained-Packages digest
binding, full lock/checksum equality, descriptor-pinned cache writes, ext2
inspection, and rollback publication. Install the evidence service as:

```ini
[Unit]
Description=Asterinas Debian M2 evidence
After=local-fs.target
Before=multi-user.target

[Service]
Type=oneshot
ExecStart=/usr/lib/asterinas/systemd-m2-evidence
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

The script writes only under `/var/lib/asterinas-debian-m2` and `/dev/console`.

- [x] **Step 5: Run focused GREEN and commit**

Run the builder/profile/evidence tests, `bash -n` on both scripts, Python static
checks, and diff check.

```bash
git add tools/riscv/debian/rootfs/build_rootfs.sh \
  tools/riscv/debian/rootfs/systemd_m2_evidence.sh \
  tools/riscv/tests/test_debian_rootfs.py
git commit -m "build(riscv): assemble Debian systemd M2 root"
```

### Task 3: Add the exact Stage1 root-init selector

**Files:**
- Modify: `tools/riscv/debian/rootfs/stage1_init.c`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`

- [x] **Step 1: Write failing native self-tests**

Cover default/explicit interactive selection, explicit systemd selection,
duplicate/unknown/control-character rejection before discovery, systemd's
reduced mount sequence, and exact `execv("/sbin/init", {"/sbin/init", NULL})`.
Retain all existing discovery-deadline, console CLOEXEC, lifecycle, and handoff
cases.

- [x] **Step 2: Run RED**

Run only `DebianStage1Tests`; expect new cases to fail because the selector is
not parsed.

- [x] **Step 3: Implement the selector and mode-specific handoff**

Parse only:

```text
--root-init=interactive
--root-init=systemd
```

Interactive mode retains the current proc/sysfs mounts and Bash rcfile exec.
Systemd mode binds `/dev`, mounts `/run` and `/tmp`, creates `/proc`, `/sys`, and
`/sys/fs/cgroup`, then lets PID 1 mount its own API filesystems.

- [x] **Step 4: Run GREEN and rebuild deterministic Stage1**

Run native tests and static C warnings-as-errors. Build the RISC-V archive once
in the pinned container and verify exact `.`/`init` entries, static ELF, modes,
and deterministic SHA-256.

- [x] **Step 5: Commit**

```bash
git add tools/riscv/debian/rootfs/stage1_init.c \
  tools/riscv/tests/test_debian_rootfs.py
git commit -m "feat(riscv): select Debian root init"
```

### Task 4: Implement the bounded systemd two-boot protocol

**Files:**
- Create: `tools/riscv/debian/rootfs/systemd_m2_gate.py`
- Modify: `tools/riscv/debian/rootfs/gate_protocol.py`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`
- Modify: `Makefile`

- [x] **Step 1: Write failing classifier/orchestrator tests**

Freeze ordered markers for boot 1, firmware restart, boot 2, and PASS. Reject
FAIL, panic/oops, early exit, reverse/duplicate markers, transcript overflow,
timeout, a third boot, and PASS without a normal reboot request. Verify every
failure invalidates stale `passed: true` evidence and still tears down the full
process group.

- [x] **Step 2: Run RED**

Expected: `systemd_m2_gate` import fails.

- [x] **Step 3: Implement by composition**

Reuse `gate_runtime` for PTY/HMP/deadlines/process groups and reuse
`rootfs_gate` snapshot/DTB/ext4 helpers. Do not copy either lifecycle. Bootargs
must include Stage1's exact `--root-init=systemd`, SMP=4, generic Sv39, 2 GiB,
`-nic none`, and `-display none`.

- [x] **Step 4: Add the Make target and run focused GREEN**

Add `test_riscv_debian_systemd_m2_gate` with explicit environment inputs and a
networkless runtime contract. Run the new focused tests and existing runtime
tests.

- [x] **Step 5: Commit**

```bash
git add Makefile tools/riscv/debian/rootfs/systemd_m2_gate.py \
  tools/riscv/debian/rootfs/gate_protocol.py \
  tools/riscv/tests/test_debian_rootfs.py
git commit -m "test(riscv): automate Debian systemd two-boot gate"
```

### Task 5: Produce and verify the signed M2 artifact

**Files:**
- Modify: `tools/riscv/debian/rootfs/README.md`
- Create: `docs/porting/evidence/2026-08-25-debian-systemd-m2-build.md`

- [x] **Step 1: Run one signed build**

Use the pinned container, explicit Clash proxy `127.0.0.1:17892`, and TUNA.
Use a stable named container, preflight keyring/binfmt/tooling, and run one M2
builder invocation. Retry only after an identified builder defect and a focused
RED/GREEN fix.

- [x] **Step 2: Verify provenance and filesystem**

Run the public contract verifier; re-hash every downloaded package; verify
plain `gpgv`, full lock/checksum equality, M2 label/UUID, ext2 metadata, absence
of qemu-static, systemd ELF/interpreter, unit enablement, and public modes.

- [x] **Step 3: Record identities and commit docs**

Record container digest, mirror, release, package versions, and SHA-256/size for
all five published files. Do not commit the 1 GiB image.

### Task 6: Pass QEMU Sv39/SMP=4 and the host gate

**Files:**
- Modify: `docs/porting/evidence/2026-08-25-debian-systemd-m2-build.md`

- [x] **Step 1: Run one complete host/static gate**

Run the full Debian rootfs unit target once, then py_compile, Ruff, C warnings,
shell syntax, and diff checks.

- [x] **Step 2: Run the QEMU M2 gate**

Use four harts, generic Sv39, 2 GiB, no network, and no display. Require the
ordered two-boot markers and a passing result file; inspect both complete serial
logs for fatal markers.

- [x] **Step 3: Commit QEMU evidence**

Record durations, argv, artifact hashes, systemd version, warnings, boot-count,
and the exact non-claims.

### Task 7: Install and boot M2 on Megrez

**Files:**
- Create: `docs/porting/evidence/2026-08-25-megrez-debian-systemd-m2.md`
- Modify: `docs/porting/evidence/megrez-history-index.md`

- [x] **Step 1: Stage immutable boot artifacts only**

Use RockOS solely to place hashed Image/Stage1 files on `/boot`; do not write
the root partition from Linux. Verify hashes before leaving Linux.

- [x] **Step 2: Install through Asterinas**

Use the exact partition-2 capability and expected M2 image SHA-256. Resume by
32 MiB chunk, read back every written chunk, then hash the complete image.

- [x] **Step 3: Run the real two-boot systemd gate**

Require compiled-Sv39, four harts, MMC, Stage1 handoff, systemd version,
boot-1 marker, Debian `/sbin/reboot -f`, a new OpenSBI/U-Boot epoch, boot-2
marker, and `DEBIAN_SYSTEMD_M2_PASS`.

- [x] **Step 4: Record and commit evidence**

Hash the local serial log, document all artifact identities and warnings, add
the append-only history row, and keep the worktree clean. Do not claim network,
USB, display, or desktop support.
