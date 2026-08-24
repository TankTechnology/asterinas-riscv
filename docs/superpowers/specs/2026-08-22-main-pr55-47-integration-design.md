# Focused Main PR Integration Design

## Goal

Admit PRs #55, #46, and #47 to current `main` as three independently
reviewable changes, then use that baseline to reconcile the much larger DRM
rollup in PR #53.

## Admission strategy

The three small PRs are replayed instead of merging their stale branch
histories. Each change keeps its original scope and receives current-main
compile, lint, and focused regression verification before the next change is
started.

- #55 validates the RISC-V device-tree contract during early boot. Admission
  also hardens region arithmetic against overflow and adds focused kernel tests
  for the range predicate. Its claim is deliberately limited to properties the
  DTB can prove; it does not claim to discover every externally mismatched QEMU
  `-smp` configuration.
- #46 sorts FDT-discovered virtio-mmio slots by ascending MMIO address before
  registration. The ordering operation is isolated and tested independently
  from device probing.
- #47 creates `/dev` when a minimal initramfs omits it, then mounts the existing
  devtmpfs implementation. The change remains limited to first-process device
  initialization and is verified with the no-`/dev` boot contract when the
  existing QEMU assets are available.

## DRM follow-up boundary

PR #53 is not merged as a 41-commit rollup. After #55/#46/#47 land, its history
is classified into three focused destinations:

1. virtio-gpu 2D plus `/dev/dri/card0` KMS and dumb-buffer mmap;
2. reusable QEMU/Xorg/Weston verification assets and reports;
3. GEM/render-node/virgl and 3D ioctl work, admitted only after the 2D/KMS
   baseline passes on current `main`.

Already-admitted keyctl, audio, device-order, devtmpfs, and DTB changes are
removed from the DRM replay rather than duplicated.

## Failure handling

Every replay stops on conflicts. Each local verification command must exit
zero before the change is committed or pushed. A missing optional QEMU asset is
reported as an environment gap and cannot be represented as runtime PASS.
Remote CI is not monitored; all acceptance evidence is local.

## Success criteria

- #55, #46, and #47 are represented by focused current-main commits.
- Rust formatting and RISC-V compile/lint checks pass for the touched crates.
- Focused tests cover DTB range overflow and virtio-mmio ordering.
- The LTP tooling and NixOS track audit suites remain green.
- PR #53 has a current-main decomposition and no longer depends on duplicating
  the three admitted fixes.
