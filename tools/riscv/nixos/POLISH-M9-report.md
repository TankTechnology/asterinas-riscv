# POLISH-M9 — cgroup file handles: `name_to_handle_at` / `open_by_handle_at` (264/265)

Date: 2026-08-16
Branch: `track/nixos` (+ focused cherry-pick PR to `main`)
Status: **Fixed and verified.** systemd's `Failed to get cgroup ID of cgroup …,
ignoring: Bad file descriptor` is gone (was 8×/boot), and a dedicated
`name_to_handle_at` → `open_by_handle_at` round-trip probe on a cgroup
directory re-opens the **same inode** and reads `cgroup.events` through the
recovered fd.

---

## 1. Root cause — the EBADF was self-inflicted, not a missing cgroup feature

The prior pass (POLISH-M8) triaged the boot log's
`Unimplemented syscall number: 264` as a missing `sync_file_range2` and
registered a handler for it at slot **264**. That was a misread of the RISC-V
syscall table:

| Slot | RISC-V (asm-generic) name | What M8 registered |
|---|---|---|
| **264** | `name_to_handle_at` | `sync_file_range2` ❌ |
| **265** | `open_by_handle_at` | *(unimplemented)* |
| 84 | `sync_file_range` (`sync_file_range2` only `#ifdef __ARCH_WANT_SYNC_FILE_RANGE2`, which RISC-V does **not** define) | *(unimplemented)* |

Verified against the installed RISC-V headers
(`/usr/riscv64-linux-gnu/include/asm-generic/unistd.h`):
`__NR_name_to_handle_at 264`, `__NR_open_by_handle_at 265`, and
`__NR_sync_file_range2 84` exists only under `__ARCH_WANT_SYNC_FILE_RANGE2`
(gated off for RISC-V). So 264 was always `name_to_handle_at`.

The consequence: every systemd cgroup-ID lookup —
`name_to_handle_at(AT_FDCWD, /sys/fs/cgroup/…, &fh, &mount_id, 0)` — was routed
to the `sync_file_range2` handler, which interpreted `fd = AT_FDCWD (-100)`
and `fdatasync`'d it → **`EBADF`**. systemd surfaced it as
`Failed to get cgroup ID … Bad file descriptor` (8×/boot in the pre-fix log,
`target/nixos/systemd/systemd-m8-serial.log.smp1`). This is the gap POLISH-M9
was asked to close.

## 2. The fix

Two coupled changes, committed as one kernel commit:

1. **Correct the dispatch.** Remove the phantom `sync_file_range2 = 264` and
   register the real syscalls: `SYS_NAME_TO_HANDLE_AT = 264`,
   `SYS_OPEN_BY_HANDLE_AT = 265` (in `syscall/arch/generic.rs`).

2. **Implement both syscalls** (`syscall/name_to_handle_at.rs`) with a minimal
   ino-based file handle, mirroring Linux's `encode_fh` / `fh_to_dentry`:

   - `Inode::encode_file_handle()` (default) emits the 64-bit inode number,
     8 bytes little-endian.
   - `FileSystem::fh_to_inode()` (default `EOPNOTSUPP`) decodes it back.
     `CgroupFs` overrides it: cgroupfs inode numbers are `node_id << 8`, so it
     shifts the ID back out and walks the cgroup `SysTree` (small, monotonic
     node ids) to recover the node, then rebuilds the `CgroupInode`.
   - `name_to_handle_at` writes `struct file_handle { handle_bytes, handle_type,
     f_handle[] }` (with the `EOVERFLOW` sizing path) plus the mount id
     (`mount_node().id()`); `open_by_handle_at` resolves `mount_fd` to its
     mount, asks that filesystem to recover the inode, and opens it
     (`Path::from_inode_and_mount` → `path.open`).

The `handle_type` (`0x81`) and payload are opaque to user space; they only need
to round-trip through `open_by_handle_at`, which they do.

## 3. Verification

### 3.1 systemd boot (integration)

Rebuilt the kernel and booted the systemd rootfs
(`boot_systemd_smoke.py --smp 1`). The new serial log
(`/tmp/m9-serial.log.smp1`) reaches `Reached target Multi-User System` →
`Login Prompts`, with:

```
Failed to get cgroup ID              → 0   (was 8)
Unimplemented syscall number: 264    → 0
Unimplemented syscall number: 265    → 0
panic / Oops                         → 0
```

Remaining `Unimplemented syscall` noise is the known-harmless out-of-scope set
only: `170` (set_mempolicy), `258` (riscv_hwprobe), `280` (bpf),
`285` (copy_file_range).

### 3.2 Round-trip probe (focused)

`tools/riscv/nixos/fh_repro.c` (static `/init`) mounts cgroup2, creates a child
cgroup, and does the full round-trip. Booted via
`tools/riscv/nixos/boot_fh_repro.py`:

```
MOUNT_OK
INO_BEFORE=1536
[264 SYS_NAME_TO_HANDLE_AT]  NAME_TO_HANDLE_OK bytes=8 type=129 mnt_id=11
[265 SYS_OPEN_BY_HANDLE_AT]  OPEN_BY_HANDLE_OK fd=4
INO_AFTER=1536
ROUNDTRIP_INO_MATCH
EVENTS_READ=populated 0 frozen 0
FH_REPRO_ALL_OK
```

The recovered fd refers to the same inode (1536) and can read `cgroup.events`
— the exact capability systemd's cgroup-ID bookkeeping depends on.

## 4. Limitations (documented, non-blocking)

- **Attribute files don't round-trip.** `fh_to_inode` decodes the handle as a
  branch node (`node_id = ino >> 8`). A handle for a cgroup *attribute* file
  (ino = `dir_ino + attr_id`) would therefore resolve to the attribute's parent
  directory. systemd only ever takes handles for cgroup **directories**, so this
  is not hit in practice; a follow-up could decode the `attr_id` low byte.
- **`sync_file_range` (84) is still unimplemented.** It was never the real gap
  (nothing in the logs calls it), so it is deliberately left out rather than
  re-introducing a misregistered slot.

## 5. Commits / PR

`track/nixos` commits:

- `f6a01a996` `feat(syscall): implement name_to_handle_at/open_by_handle_at (264/265)`
- `60fdb6734` `test(riscv): cgroup file-handle round-trip probe (fh_repro)`

The kernel commit cherry-picks onto `origin/main` as a focused PR (branch
`name-to-handle-at`). Note: `origin/main` never had the phantom
`sync_file_range2` slot, so the cherry-pick reduces to a pure addition of the
two syscalls — no removal needed there.
