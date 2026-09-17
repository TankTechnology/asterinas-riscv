# DRM-M7 — persistent storage lands: virtio-blk device-order fix + reboot persistence

Date: 2026-08-15
Branch: `track/drm`
Status: **DONE** — the long-standing `mount -t ext2 /dev/vdb → EINVAL` bug is
root-caused (it was never an ext2 bug; it was a **virtio-mmio device-numbering
swap**) and fixed; a second virtio-blk ext2 disk now mounts in the boot chain
and a file written before a reboot is read back after it.

---

## 0. TL;DR

| item | result |
|---|---|
| `mount -t ext2 /dev/vdb` before | `EINVAL` (errno 22) |
| root cause | virtio-mmio devices enumerated in DT order (descending addr), QEMU assigns slots in ascending addr → `vda`/`vdb` swapped |
| fix | sort `virtio,mmio` nodes by MMIO address before registering |
| `mount -t ext2 /dev/vdb` after | **SUCCESS** |
| reboot persistence | `boot1 → __M7_PERSIST__=WROTE`, `boot2 → __M7_PERSIST__=SURVIVED` — **PASS** |

---

## 1. The bug — reproduce → locate → fix → verify

### 1.1 Symptom

The M9 lightweight-NixOS work (§7 of `asterinas-riscv-nixos/…/m9/M9-report.md`)
attempted to persist `/nix/store` on a second virtio-blk ext2 disk. Its `/init`
does `mount("/dev/vdb", "/nix", "ext2", …)` and logged:

```
__M9_INIT__ mount /dev/vdb ext2 failed: errno=22 (Invalid argument)
```

FOUNDATION-M2 (`tools/riscv/nixos/FOUNDATION-M2-report.md`, issue #2) recorded
the same block for the LTP gate. The ext2 driver is registered and correct; the
on-disk superblock magic (`0xef53`) was readable through `/dev/vdb`, so the
symptom was attributed to "the block-device read path for a non-boot virtio-blk
disk (or the ext2 superblock validation against it)".

### 1.2 Reproduction (minimal probe)

A minimal static `/init` (`m7/probe`/`persist_init.c` pattern) boots the
DRM-tree kernel with **two** virtio-blk disks — `/tmp/drm-m7/boot.ext4` (the
128 MiB ext4 boot disk) and `/tmp/drm-m7/nix-store.ext2` (a 256 MiB ext2 image
made with `mkfs.ext2 -b 4096`) — and reads the superblock of both device nodes.

**Before the fix**, `/dev/vdb` returned the *boot* disk's superblock:

```
__PROBE__ /dev/vda: BLKGETSIZE64=268435456 bytes   (nix-store.ext2 — 256 MiB)
__PROBE__ /dev/vda: inodes=65536 blocks=65536 log_block_size=2 magic=0xef53
__PROBE__ /dev/vdb: BLKGETSIZE64=134217728 bytes   (boot.ext4    — 128 MiB)
__PROBE__ /dev/vdb: inodes=32768 blocks=131072 log_block_size=0 magic=0xef53
__PROBE__ mount /dev/vdb ext2 FAILED: errno=22 (Invalid argument)
__PROBE__ mount /dev/vda ext2 SUCCESS
```

The magic was right (`0xef53`, shared by ext2/ext3/ext4) but `log_block_size` was
`0` — the boot disk is ext4 with 1024-byte blocks, and the driver rejects
anything but `log_block_size == 2` (4096-byte blocks). So the earlier "correct
magic at 1080, wrong log_block_size at 1048" observation was simply the **boot
disk's superblock**, not a torn read.

### 1.3 Root cause

The devices were **named in reverse order**:

- QEMU assigns `-device virtio-blk-device` to the RISC-V `virt` machine's fixed
  virtio-mmio slots in **ascending** MMIO address order (the first `-device`
  gets `0x10001000`, the second `0x10002000`, …).
- QEMU emits the `virtio,mmio` nodes in the device tree in **descending**
  address order (`0x10008000` first … `0x10001000` last).
- The kernel's RISC-V MMIO probe (`kernel/comps/virtio/src/transport/mmio/bus/arch/riscv.rs`)
  iterated `fdt.all_nodes()` in tree order, so it registered the **second**
  disk (`0x10002000`) first → index 0 → `vda`, and the **first** disk
  (`0x10001000`) second → index 1 → `vdb`.

Net effect: `/dev/vda` = data disk, `/dev/vdb` = boot disk. Every consumer that
followed the "second `-device` is `vdb`" convention (M9's `/nix` mount, LTP's
`LTP_DEV`) mounted the **boot** disk and hit `EINVAL`.

### 1.4 Fix

`kernel/comps/virtio/src/transport/mmio/bus/arch/riscv.rs` — collect the
`virtio,mmio` slots, **sort by MMIO address ascending**, then register:

```rust
let mut mmio_slots = Vec::new();
for node in mmio_nodes { … mmio_slots.push((mmio_start, mmio_end, irq)); }
mmio_slots.sort_by_key(|(mmio_start, _, _)| *mmio_start);
for (mmio_start, mmio_end, interrupt_source_in_fdt) in mmio_slots { … }
```

The kernel now enumerates virtio-mmio devices in the same order QEMU assigns
them, so device numbering matches the `-device` command-line order.

### 1.5 Verification (after the fix)

```
__PROBE__ /dev/vda: BLKGETSIZE64=134217728 bytes   (boot.ext4    — 128 MiB)
__PROBE__ /dev/vda: inodes=32768 blocks=131072 log_block_size=0 magic=0xef53
__PROBE__ /dev/vdb: BLKGETSIZE64=268435456 bytes   (nix-store.ext2 — 256 MiB)
__PROBE__ /dev/vdb: inodes=65536 blocks=65536 log_block_size=2 magic=0xef53
__PROBE__ mount /dev/vdb ext2 SUCCESS
__PROBE__ mount /dev/vda ext2 FAILED: errno=22 (Invalid argument)   # ext4, correct
```

`/dev/vdb` is now the second (ext2) disk and mounts cleanly; `/dev/vda` is the
boot disk and correctly refuses an `ext2` mount.

---

## 2. Persistent storage in the boot chain

The smoke harness (`tools/riscv/nixos/m7/`) mounts the second disk at `/home`
and proves a file written on one boot is readable on the next:

- `persist_init.c` — static `/init`: mount `/dev/vdb` on `/home`; if
  `/home/PERSISTED` exists report `SURVIVED`, else write it, `sync()`, report
  `WROTE`.
- `build_m7.sh` — cross-compile `/init`, create the 256 MiB ext2 data disk
  (`mkfs.ext2 -b 4096`), pack the raw-cpio initramfs, re-pack `/tmp/drm-m7/boot.ext4`
  with the DRM kernel + initramfs + DTB.
- `boot_m7.py` — boot **twice** with the same data disk attached and assert
  boot 1 wrote and boot 2 survived.

```
[boot1] wrote sentinel: True          __M7_MOUNT__=OK … __M7_PERSIST__=WROTE
[boot2] sentinel survived: True       __M7_MOUNT__=OK … __M7_PERSIST__=SURVIVED content=m7-persisted
=== DRM-M7 persistence: PASS ===
```

This is the same mechanism M9 intended for `/nix/store` (and equally applies to
`/home`): the second virtio-blk disk is mounted onto the target path in the boot
chain, and everything written there outlives the boot.

---

## 3. Upstream rollup — kernel fix PR

Following the M6 pattern, the one-line-root-cause kernel fix is extracted onto a
clean single-commit branch off `origin/main` and PR-ed. The pre-fix
`riscv.rs` on `track/drm` is byte-identical to `origin/main`'s copy, so the
cherry-pick is conflict-free.

- Branch: `fix/virtio-mmio-device-order`
- Base: `main`, head: `fix/virtio-mmio-device-order`, one commit.

---

## 4. Result

| deliverable | status |
|---|---|
| ext2 `EINVAL` root-caused | virtio-mmio device-numbering swap (not ext2, not a torn read) |
| kernel fix | sort `virtio,mmio` nodes by MMIO address (`aster-virtio`) |
| second-disk mount in boot chain | `persist_init.c` mounts `/dev/vdb` at `/home` |
| reboot persistence verified | `WROTE` → reboot → `SURVIVED` (PASS) |
| upstream PR | `fix/virtio-mmio-device-order` → `main` |

## 5. Files changed

- `kernel/comps/virtio/src/transport/mmio/bus/arch/riscv.rs` — sort virtio-mmio
  slots by address (the kernel fix).
- `tools/riscv/nixos/m7/{persist_init.c,build_m7.sh,boot_m7.py}` — the
  persistent-storage smoke harness.
- `tools/riscv/nixos/DRM-M7-report.md` — this report.
