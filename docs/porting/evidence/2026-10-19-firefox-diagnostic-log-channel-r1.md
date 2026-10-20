# Firefox diagnostic log channel r1

## Scope

This bounded host-side implementation check changes only the opt-in
`mmap`/`epoll`/`futex` diagnostic records. They now use the kernel logger
(`ostd::info!`) instead of direct `early_println!` writes, so the logger's
console record lock covers each complete marker. Marker names and fields are
unchanged. No QEMU guest or real hardware was started in this check.

## Verification

```text
make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
Finished `dev` profile ... aster-kernel-osdk-bin
python3 -m unittest tools.riscv.tests.test_firefox_drm_scene_gate
Ran 22 tests ... OK
python3 -m py_compile tools/riscv/debian/rootfs/firefox_drm_scene_gate.py
git diff --check
```

The generated kernel image is `target/osdk/aster-kernel-osdk-bin.Image` with
SHA-256
`7188a7d83a648738e2fe8304c32a882dc4cc2e9a75ba9c80bf94ee8b91a1b454`.

This closes only the implementation/compile check for serialized diagnostic
records. The next short Firefox probe must still prove at runtime that the
records are no longer interleaved; no full scene gate is implied.
