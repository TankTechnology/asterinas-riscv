# Debian application matrix, 2026-11-09

The desktop has been validated with Firefox, a terminal and a file manager.
The question now is which *other* Debian riscv64 packages work, and what they
expose in the kernel. This page records the matrix tool and the first run.

## Tool

```bash
rt apps --list                    # print the declared applications
rt apps --board                   # run the matrix on the booted board
rt apps --board --install jq      # apt-get install first, then run
rt apps --board --timeout 280     # default collection window
```

- `tools/riscv/apps/manifest.py` declares each application: package, kind
  (`cli`/`gui`), command, expected output or window name.
- `tools/riscv/apps/guest/probe.sh` runs inside the guest and prints
  `APP <name> PASS|FAIL|SKIP <detail>`.
- `tools/riscv/apps/runner.py` plans, optionally installs, pushes the probe
  and manifest over the serial bridge, and collects the result lines.

CLI entries must exit 0 and print `expect`. GUI entries must open a **new**
window (absent from the pre-launch window list) whose name matches `window`
within six seconds; a stale window cannot pass a run.

## First matrix

| Application | Kind | Package | Result |
| --- | --- | --- | --- |
| git | cli | git | PASS |
| python3 | cli | python3 | PASS |
| tree | cli | tree | PASS |
| jq | cli | jq | PASS |
| file | cli | file | PASS (`file -L`) |
| htop | cli | htop | PASS |
| less | cli | less | PASS |
| rsync | cli | rsync | PASS |
| strace | cli | strace | PASS (ptrace works) |
| lsof | cli | lsof | PASS (partial `/proc/<pid>/cwd` readlink) |
| zip/unzip | cli | zip unzip | PASS |
| xlogo | gui | x11-apps | PASS |
| mousepad | gui | mousepad | PASS |
| galculator | gui | galculator | PASS |
| feh | gui | feh | PASS |

`APPS total=15 pass=15 fail=0 skip=0 complete=True` on the tabfix kernel.
`lsof` runs but cannot resolve `cwd`/`rtd` through `/proc/<pid>/{cwd,root}`
readlink; the FD listing itself works.

Package installs come from the existing trixie sources (Tsinghua mirror +
security). `/var/cache/apt` grows by ~88 MB per batch; `rt apps --install`
runs `apt-get clean` afterwards. After installing this batch, cleaning the
cache leaves 237 MB free on p2.

## Kernel gaps this exposed

1. **Canonical-mode line discipline dropped TAB and control bytes.** The
   ldisc only stored `is_printable_char(ch)` (0x20-0x7e and >= 0x80), so a
   typed tab vanished and `a\tb` reached a program as `ab`. Linux's n_tty
   stores every byte that is not an editing/terminator character. Fixed in
   `kernel/src/device/tty/line_discipline.rs`: only VERASE/VKILL/VWERASE and
   the line terminators are interpreted; everything else is data. TAB is
   echoed as a tab (not `^I`). A ktest covers `a\tb\x01c\n`.
2. **`wait4` rejects `__WALL` with a warning.** bash job control uses
   `wait4(pid, __WALL)` when reaping killed jobs; Asterinas logs
   `unsupported wait options are found: WALL` per call. The wait still
   succeeds, so it is noise rather than failure; accepting (and ignoring) the
   flag is a follow-up.

The first gap was also the reason the matrix tool's own manifest transfer
failed: it used a tab-separated manifest. The push path now uses `cat` +
canonical EOF rather than a shell heredoc, and the manifest uses 0x1f (unit
separator) because TAB is IFS whitespace and `read` collapses empty fields
(in the first run this left the GUI window pattern empty, so every GUI case
matched a stale window and passed). Field separators must not be IFS
whitespace.

## Push protocol (v3)

`Console.push()` now types `cat > <remote>`, streams the payload, and sends
`^D` (an empty line in canonical mode) to finish. A payload that does not end
with a newline gets a second `^D`. This keeps bytes verbatim: a shell heredoc
reads its body through bash's line handling, which eats tabs and other control
characters. The command is terminated with a single LF because the bridge
only flushes its FIFO buffer on LF.

`push_verified()` hashes the local file, asks the guest for `sha256sum`, and
retries at 2, 6 and 12 ms/byte until the hashes match. The digest marker must
be matched alone on its line: the echoed command contains the same marker.

## Next

- Add a PDF viewer (`mupdf` or `zathura`) once disk budget allows; it is the
  next heaviest dependency tree (poppler/Qt or GTK).
- Add a media player (`mpv`) to exercise audio/video ioctls; HDA audio was
  already brought up separately.
- Feed `apps` into the subsystem selftest as the `apps` subsystem (terminal,
  editor, files, archive are already declared in the catalog).
