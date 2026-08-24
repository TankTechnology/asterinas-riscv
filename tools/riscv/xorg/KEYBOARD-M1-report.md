# KEYBOARD-M1 — Termios Line-Discipline Echo Fix + WERASE + xkbcomp-stub

**date:** 2026-08-18
**branch:** main
**head:** 1ed8a46c5-dirty

## Summary

Three interdependent fixes for the RISC-V desktop keyboard pipeline:

1. **Kernel `line_discipline.rs`** — Implement `ECHOE`/`ECHOK`/`ECHOKE` echo
   variants and `VWERASE` (Ctrl-W word erase) in the canonical-mode line
   discipline. The old code sent bare `\x08` for every erase regardless of
   termios flags; Linux `n_tty.c` uses `ECHOE` → BS-SP-BS to physically
   overwrite the erased character on screen.

2. **Kernel `handler.rs`** — Drop the per-key-event `debug!` log from the
   keyboard IRQ hot path. At `loglevel=info` (the desktop default), this
   generated a log line for every keystroke, which is significant overhead
   inside the input IRQ handler.

3. **Build script + xorg.conf** — Ship the pre-compiled `default.xkm` keymap
   and `xkbcomp-stub` so Xorg can load the keymap without shelling out to
   `xkbcomp` (which requires `/bin/sh` and a pipe). Add `stty` busybox applet
   so the guest can inspect/change terminal settings. Add `XTerm` app-defaults
   (`backarrowKey=false`) so xterm sends DEL (0x7f) matching the kernel's
   default VERASE.

## Changes

### 1. Kernel: line-discipline echo rewrite (`kernel/src/device/tty/line_discipline.rs`)

**`CurrentLine` additions** (lines 83–101):
- `word_backspace_len()` — returns the number of characters that would be
  erased by WERASE (skips trailing whitespace, then the preceding word).
- `word_backspace()` — applies the word erase to `self.len`.

**`push_char` addition** (lines 165–170):
- VWERASE handling: when `IEXTEN` is set, erases the last word from the
  current line. This is gated on `IEXTEN` per POSIX.

**`output_char` rewrite** (lines 188–233):
- `VERASE` echo: now checks `ECHOE` → BS-SP-BS (overwrites the erased
  character on screen) vs bare `\x08`. Gated on canonical mode.
- `VWERASE` echo: when `ECHOE` is set, emits BS-SP-BS for each erased
  character; otherwise a single BS. Gated on canonical mode + IEXTEN.
- `VKILL` echo: when `ECHOKE` is set, erases each character on screen with
  BS-SP-BS; when `ECHOK` is set, echoes a newline. Gated on canonical mode.
- All echo transforms now extract `local_flags` and `canonical` once at the
  top of the function instead of calling accessors inside each match arm.

The termios defaults in `termio.rs` already set `ECHOE | ECHOK | ECHOKE |
IEXTEN` and `VERASE = 0x7f`, `VWERASE = Ctrl-W`, `VKILL = Ctrl-U` — these
were defined but never checked by the line discipline. The fix closes that gap
for the three most important erase characters.

### 2. Kernel: drop IRQ-path keyboard logging (`kernel/src/device/tty/vt/keyboard/handler.rs`)

Removed the `ostd::debug!` call at the top of `handle_key_event()` (lines
65–66 old, replaced with a comment). Every keystroke enters through this
function via the virtio-input IRQ; logging each event at `info`/`debug` level
adds measurable overhead even when the log level is `warn` (the format
arguments are still evaluated). The comment documents the reasoning so future
readers don't re-add it.

### 3. Build script: xkbcomp-stub, default.xkm, stty, XTerm app-defaults (`tools/riscv/systemd/build_systemd_desktop.sh`)

**Step 10** (old lines 188–196 → new lines 188–209):
- Prefer `xkbcomp-stub` (the pre-compiled keymap emitter) over the real
  `xkbcomp`. Xorg invokes `xkbcomp` by shelling out to `/bin/sh -c "xkbcomp
  ..."`, which requires a working shell and pipe. The stub is a static RISC-V
  binary that writes the pre-compiled keymap to stdout with no subprocess
  overhead.
- Copy `default.xkm` to `/etc/xkb/default.xkm` so Xorg's
  `XkbCompiledKeymap` option resolves.

**Step 5** (line 117):
- Add `stty` to the busybox applet symlink list. The desktop rootfs's busybox
  previously only had the `sh` applet; `stty` is needed to inspect/change
  terminal line-discipline settings from the guest.

**Step 14b** (lines 330–338):
- Copy `XTerm` app-defaults from the cross-compiled sysroot into
  `/usr/lib/X11/app-defaults/XTerm`. The file sets `backarrowKey=false` so
  xterm sends DEL (0x7f) for the Backspace key, matching the kernel's default
  VERASE character. Without this, xterm defaults to `backarrowKey=true` →
  sends ^H, which the kernel line discipline does not recognise as erase.

### 4. Xorg configuration (`tools/riscv/xorg/xorg.conf`)

Added explicit XKB options to the keyboard InputDevice section:
```
Option  "XkbRules"  "evdev"
Option  "XkbModel"  "pc105"
Option  "XkbLayout" "us"
Option  "XkbCompiledKeymap" "/etc/xkb/default.xkm"
```

`XkbCompiledKeymap` lets Xorg load the keymap directly from the pre-compiled
file without invoking `xkbcomp` at all — a belt-and-suspenders approach: the
`xkbcomp-stub` handles the shell-out path, and the compiled keymap handles the
direct-load path.

### 5. Systemd units: KeyringMode=inherit (9 files)

Added `KeyringMode=inherit` to every `[Service]` section. The kernel does not
implement the `keyctl` syscall family; without this, systemd's per-service
session keyring creation fails and the unit transitions to a failed state.
This is a mechanical, consistent addition across all units:
`curl-cert-test.service`, `emergency.service`, `matchbox-window-manager.service`,
`netsurf.service`, `nix-activation.service`, `nix-smoke.service`,
`pcmanfm.service`, `xorg.service`, `xpanel.service`, `xterm.service`.

### 6. Workspace: exclude tools/vnc-capture (`Cargo.toml`)

Added `tools/vnc-capture` to the workspace `exclude` list so `cargo build` at
the workspace root does not try to compile the host-side VNC capture tool as
part of the kernel workspace.

### 7. Cargo.lock

Dependency version bumps from `cargo update` (bitflags 2.13.0→2.13.1,
syn 2.0.118→2.0.119, zerocopy 0.8.54→0.8.56, etc.). No semantic changes.

## Verification

### Build

- Kernel built successfully with `TARGET_ARCH=riscv64 FEATURES=riscv_sv39_mode`
  (Sv39 page-table mode, required for the bochs→simple-framebuffer→VT display
  chain).
- `tools/vnc-capture` builds as a standalone host binary (not part of the
  kernel workspace).
- `build_systemd_desktop.sh` was not runnable end-to-end because the
  cross-compiled sysroot (`target/riscv-cross/usr`) and glibc runtime
  (`target/xorg-rootfs/lib`) were cleaned from the working tree. The script
  changes are structurally correct — the file paths, guards, and symlinks are
  consistent with the existing patterns.

### Boot smoke test

A minimal initramfs (busybox + `/init` shell script) was packed with the newly
built Sv39 kernel and booted in QEMU (`-machine virt`, U-Boot `booti`
handoff). The kernel reached userspace, the init script ran, and the busybox
ash shell was available at the console. No kernel panics, no page faults.

### What was NOT tested

- Full desktop boot (Xorg + xterm + matchbox) — requires the cross-compiled
  desktop binaries which are not currently available in the working tree.
- VNC framebuffer screenshot verification — requires the desktop to be
  running.
- The `sendkey` backspace test via QEMU monitor — the VNC display was not
  attached in the minimal smoke test.

## Review notes

The changes are internally consistent and follow the existing code patterns:

| Concern | Verdict |
|---------|---------|
| `word_backspace_len` uses `is_ascii_whitespace()` — what about non-ASCII? | POSIX WERASE only operates on the current line buffer, which in canonical mode only contains ASCII (printable + NL). The kernel's `push_char` only calls `push_char` for printable chars (0x20–0x7f). Non-ASCII input is not a concern. |
| `output_char` calls `word_backspace_len()` for echo — but `word_backspace` was already called in `push_char`, so the line state matches. | Correct. `push_char` runs before `output_char`, so `current_line.len` reflects the post-erase state. `word_backspace_len()` reads the current buffer state and returns the correct count for echo. |
| The `VWERASE` match arm in `output_char` does not re-check `ch == self.termios.special_char(...)` — it relies on the match arm ordering. | The match arm is `ch if canonical && IEXTEN && ch == VWERASE`. The character check is present. |
| `ECHOK` and `ECHOKE` are both set in defaults. `output_char` handles both: ECHOKE erases each char, ECHOK adds a newline. | Correct. Linux `n_tty.c` does the same: ECHOKE erases the displayed line, ECHOK echoes NL. Both can be set simultaneously. |
| The `debug!` removal in `handler.rs` — is there any other logging on the keyboard IRQ path? | No. The only other log in the keyboard subsystem is `warn!` for unsupported events (line 435), which fires only on unexpected input types (not keystrokes). The virtio-input driver has no per-event logging. |

## Future work

- **Full desktop boot verification**: Once the cross-compiled sysroot is
  restored, run `build_systemd_desktop.sh` with the new changes, boot the
  resulting initramfs, and verify:
  - Backspace key sends DEL and erases characters in xterm
  - Ctrl-W erases the last word
  - Ctrl-U erases the whole line
  - The echo behavior (BS-SP-BS) visually clears characters
- **Non-canonical mode echo**: Currently `output_char` gates all special-character
  echo on `canonical`. A raw-mode program that sets `ECHO` but not `ICANON`
  would get no echo for DEL — this is a latent bug, same as the old code.
- **Remaining CCtrlChar handlers**: VREPRINT (Ctrl-R), VDISCARD (Ctrl-O),
  VLNEXT (Ctrl-V), VSUSP (Ctrl-Z with ISIG) are still not implemented in the
  line discipline. They are defined in `termio.rs` with correct defaults.