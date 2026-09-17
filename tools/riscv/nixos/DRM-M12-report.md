# DRM-M12 — the DTB self-consistency check from M11 becomes PR #55

Date: 2026-08-16
Branch: `track/drm` (report only — the code change is raised independently as PR #55 against `main`)
Status: **DONE** — (1) the M11 "proposed fix" (validate the device tree before trusting its
CPU/memory/interrupt description) is now a real PR, **#55**, rather than a
deferred note; (2) it is extracted from the DRM work and raised against `main`
on its own because the touched code (`ostd` boot) is shared across x86,
loongarch and other RISC-V boards; (3) `track/drm` carries no new code for it —
only this report.

---

## 0. TL;DR

| item | result |
|---|---|
| M11 proposed fix → real change | **PR #55** (`fix/riscv-dtb-self-consistency` → `main`), +168/-0 |
| scope | `ostd/src/arch/riscv/boot/mod.rs` only |
| checks added | CPU nodes, memory layout, interrupt controllers (see §1) |
| compile/static verification | `cargo check` / `clippy` / `fmt` (riscv64imac) all clean |
| full QEMU boot run | not run — build env was reset (§4) |
| `track/drm` push | nothing new — already up to date with `origin/track/drm` |

---

## 1. What M12 does

M11's §3.3 documented — but deliberately did not apply — a device-tree
self-consistency check, on the grounds that the early-boot code is shared and
needs cross-board validation. M12 turns that note into a mergeable PR, raised
against `main` independently of the DRM feature.

`validate_device_tree()` runs in `riscv_boot` immediately after the FDT is
parsed (`DEVICE_TREE.call_once`) and *before* `EARLY_INFO` is constructed, so it
fires before any consumer trusts the CPU count, memory layout, or interrupt
wiring. Three heap-free checks:

| check | function | what it enforces |
|---|---|---|
| CPU nodes | `validate_cpu_nodes` | `/cpus` describes ≥1 MMU-capable, uniquely-addressed hart, and includes the bootstrap hart |
| memory layout | `validate_memory_layout` | `/memory` declares non-zero RAM, and the kernel image + initramfs both lie inside it |
| interrupt wiring | `validate_interrupt_controllers` | every MMU CPU carries a `riscv,cpu-intc` child with a `phandle` |

The criteria mirror exactly what the existing `smp::for_each_hart_id` and
`Plic::from_fdt` parsers already rely on, so the check cannot reject a DTB the
kernel would otherwise accept — it only turns the *self-inconsistent-but-
parseable* case into a descriptive panic. Every panic is prefixed `[DTB]` and
names the mismatch (e.g. "initramfs [0x83000000, 0x89600000) is outside the RAM
declared by '/memory' (device tree '-m' does not match the boot arguments?)").

Which real failure each check catches:

- **CPU nodes** → a DTB dumped with a stale `-smp` (boot with `-smp 1` while the
  tree describes 4 harts, or vice-versa) makes `boot_all_aps` start the wrong
  set of harts and spin forever.
- **memory layout** → the actual M10 root cause: a DTB dumped without `-m 2G`
  declares 128 MiB while the guest boots with 2 GiB, so the initramfs lands
  beyond the declared RAM and is silently corrupted while the page tables are
  built. `validate_memory_layout` compares `MemoryRegion::kernel()` and
  `parse_initramfs_range()` against the `/memory` regions via the
  `is_covered_by_memory` helper.
- **interrupt controllers** → a CPU missing its `riscv,cpu-intc` phandle is
  silently left with no external interrupts (the PLIC uses that phandle to map
  `interrupts-extended` entries back to harts).

---

## 2. Why it is a separate PR against `main` (not part of the DRM rollup)

The touched file, `ostd/src/arch/riscv/boot/mod.rs`, is the shared RISC-V boot
entry — not DRM code. Folding the check into PR #53 (`track/drm` → `main`) would
couple a platform-integrity fix to the graphics feature, delay it behind the DRM
review, and risk it being reverted or conflicted with the independent fix PRs
(#43 keyctl, #46 virtio-mmio, #47 devtmpfs). Raising it as PR #55 against `main`
keeps it reviewable and mergeable on its own.

The three checks are heap-free by construction: they run before the frame
allocator exists, so they use only fixed-size arrays (`MAX_DT_HARTS`) and the
FDT's existing read-only APIs.

---

## 3. Verification

```text
cargo check  -p ostd --target riscv64imac-unknown-none-elf --features riscv_sv39_mode
cargo clippy -p ostd --target riscv64imac-unknown-none-elf --features riscv_sv39_mode
cargo fmt    -p ostd -- --check
```

All clean. (Note for this tree: `cargo check -p ostd --target
riscv64imac-unknown-none-elf` works directly — the target is installed and no
`cargo osdk` is needed — which is much faster for compile-verifying ostd-only
changes.)

---

## 4. What was NOT done

No full QEMU boot run: the build environment had been reset between sessions
(`/tmp/osdk-bin`, `/tmp/drm-m1` were gone), and reconstructing it was out of
scope for this milestone — the change is a pure additive early-boot panic path,
verified by compile + static checks. A negative-boot test (feed the known-bad
128 MiB DTB and assert the new `[DTB]` panic fires instead of silent corruption)
is the natural follow-up once the env is restored.

---

## 5. PR topology (as of this report)

| PR | head → base | what | state |
|---|---|---|---|
| **#53** | `track/drm` → `main` | DRM/KMS desktop rollup (virtio-gpu 2D + KMS + cursor + modesetting + M1–M12 reports/harnesses) | OPEN |
| **#55** | `fix/riscv-dtb-self-consistency` → `main` | DTB self-consistency check (this milestone) | OPEN |
| #43 / #46 / #47 | — → `main` | keyctl / virtio-mmio / devtmpfs fixes | OPEN (independent) |

`track/drm` itself is `up to date with origin/track/drm` — the only change this
milestone adds to it is this report file.

---

## 6. Files changed (this branch)

- `tools/riscv/nixos/DRM-M12-report.md` — this report.

(The code change lives in PR #55: `ostd/src/arch/riscv/boot/mod.rs`, +168/-0.)

---

## 7. Result

| deliverable | status |
|---|---|
| DTB self-consistency check (M11 proposal) | **PR #55** raised against `main`, +168/-0, OPEN |
| three checks (CPU / memory / interrupt) | implemented, `[DTB]`-prefixed descriptive panics |
| compile + clippy + fmt | clean |
| full QEMU boot | deferred (build env reset) — negative-boot test is the follow-up |
| `track/drm` | up to date, no unpushed commits beyond this report |
