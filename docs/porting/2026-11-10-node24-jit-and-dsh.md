# Node 24's riscv64 JIT fault and running dsh on the board, 2026-11-10

This note records why Node 24 crashes on the Megrez board, which is not an
Asterinas defect, and what the official DeepSeek harness (`dsh`) needs before
it can boot inside the board's OS.

## Node 24: not a kernel bug

Node 24.22.2 (unofficial-builds `linux-riscv64`) crashes on any JavaScript that
enables JIT:

```
page fault handler failed: ... address: 0x20000001f, required_perms: READ,
    err: EACCES, msg: "no VM mappings contain the page fault address"
```

`--jitless` runs; Node 22.23.4 and Node 26.0.0 (same tarball source) run with
JIT. The fault is inside the node binary itself, not JIT-generated code:

| Item | Value |
| --- | --- |
| Faulting instruction | `0x105f67a` in the text segment |
| Function | `v8::internal::Runtime_InstallBaselineCode` |
| Instruction | `addi a5,a5,31; ld s7,0(a5)` with `a5 = 0x200000000` |
| Meaning | Sparkplug's runtime receives `0x200000000` where `args[0]` should be a JSFunction |

The tier matrix isolates Sparkplug: `--no-sparkplug --no-maglev` (Turbofan only)
works, while any configuration with Sparkplug faults, changing the reported
address (`0x40000001f` vs `0x20000001f`) with the tier set.

### Falsified hypotheses

- **Pointer-compression cage / V8 sandbox.** The binary embeds
  `v8_enable_pointer_compression: 0`, `v8_enable_sandbox: 0`,
  `v8_enable_external_code_space: 0`.
- **Code range far from `.text`.** Asterinas ignores a non-`MAP_FIXED` hint when
  the exact range is occupied (V8 asks for a hint inside node's own text
  segment). Forcing the 256 MiB code range into V8's preferred region at
  `0x2af0000000` still faults at the same address.
- **Signal delivery.** A user SIGSEGV handler at the same address catches the
  fault and recovers, so the kernel's fault-signal path is correct.
- **Asterinas itself.** The same Node 24 binary crashes under
  `qemu-riscv64` user-mode emulation with a Debian glibc sysroot (real Linux
  syscall semantics, no Asterinas). Node 22 and Node 26 work both under qemu
  and on the board.

Conclusion: use Node 22 or Node 26 on the board; Node 24.22.2's riscv64
Sparkplug path is broken in the published binary.

## dsh on riscv64: platform gaps and shims

The official `@deepseek-ai/dsh` (0.2.2-rc.1) hard-depends on native addons
that publish only darwin/x64/arm64/win32 prebuilds:

| Package | Missing piece | Workaround used on the board |
| --- | --- | --- |
| `node-addon-require-builtin` | `napi-v9-linux-riscv64-gnu` binding | replace `lib/index.js` with a shim that forwards `requireBuiltin(id)` to Node's own `require` under `node --expose-internals` |
| `@deepseek-ai/node-addon-system` (`flock`) | `linux-riscv64` binding | no-op `tryLockExclusive` (single harness instance on the board) |
| `@deepseek-ai/node-addon-system` (`landlock-run`) | riscv64 launcher | not needed for `DSH_PERMISSION_MODE=danger-full-access`; the sandbox probe reports `unusable` and the bash executor bypasses it |

The require-builtin shim is exercised on the board: dsh boots past profile
resolution and plugin loading and reaches the next failure, so the internal
`require` path is sufficient on Node 26.

## Kernel fix: JBD2 transactions with multiple descriptor blocks

`fsync` on the journaled `/data` (ext3 layout, 114 GiB) returned `E2BIG`.
`ext2/journal.rs` encoded a whole transaction into a single JBD2 descriptor
block, which tags at most `(BLOCK_SIZE - header - uuid) / tag = (4096 - 12 -
16) / 8 = 508` blocks. A filesystem-wide metadata sync on a 917-group volume
exceeds that.

Fixed in this tree:

- `JournalTransaction::encode` splits the tag list into as many descriptor
  blocks as needed, each followed by its own payloads and terminated by the
  single commit block.
- `Ext2::recover_journal` scans a transaction across descriptor blocks and
  revoke blocks until the commit block, instead of assuming exactly one
  descriptor block.
- `splits_oversized_transactions_across_descriptors` covers the encoder; the
  existing encode tests still pass.

Validated with the RISC-V QEMU ktest suite (`cargo osdk test`, `--scheme
riscv`), which needed three pre-existing ktest-only import fixes:

- `fs_impls/ext2/fs.rs`: import `RawBlockGroup` in the ktest module.
- `fs/vfs/path/mount_namespace.rs`: import `FileSystem` in the ktest module.
- `fs_impls/procfs/sys/kernel/pid_max.rs`: add `validate_pid_max`, shared by
  the procfs write path and its ktest.

## Operational notes

- `/data` is p3 (LBA 0x008fa022..0x0ee2afde) mounted by `p3-data.service`. The
  MMC write gate only arms writes inside that exact window with
  `asterinas.mmc_write_partition3` on the command line.
- Do not run `umount /data` from a shell whose cwd is `/data`, and expect a
  manual unmount to block if any process still holds a file or cwd on the
  filesystem: the board then has no remote control path (no sshd, VNC or
  Marionette listener; serial SysRq is not implemented) and needs a physical
  reset.
- Node 26 must be launched with `--expose-internals` for the shim; the
  `dsh-web.service` unit does this and reads the API key from
  `/root/.config/dsh/key` into the process environment only.
