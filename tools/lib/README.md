# `tools/lib/shell.sh`

Shared helpers for the repository's shell scripts. Source it; never execute it.

```bash
# shellcheck source=lib/shell.sh
_shell_self_dir=${BASH_SOURCE[0]%/*}
if [[ ${_shell_self_dir} == "${BASH_SOURCE[0]}" ]]; then _shell_self_dir=.; fi
source "${_shell_self_dir}/lib/shell.sh"
unset _shell_self_dir
REPO_ROOT="$(repo_root)"
```

## API

| Function | Behavior |
| --- | --- |
| `repo_root` | Prints the repository root. Resolved from the library's own location (`tools/lib`), so a script at any depth gets the same answer. |
| `die "message"` | Prints `error: <message>` to stderr and exits 1. |
| `require_cmd cmd...` | Fails with `die` naming the first command missing from `PATH`. |
| `log_step "message"` | Prints `==> <message>` to stderr. |

## Rules

- The prologue uses only shell builtins (`${BASH_SOURCE[0]%/*}`, `cd`, `pwd`).
  Some scripts are tested with an empty `PATH` (for example
  `prepare_qemu_uboot_booti.sh --check-tools`), where an external `dirname`
  would fail before the script can report a missing tool.
- The library itself must stay dependency-free: no `grep`, `sed`, `dirname`
  or other external commands.
- A script that still needs its own directory for other paths keeps its
  `SCRIPT_DIR` assignment; only the repository-root computation moves here.
