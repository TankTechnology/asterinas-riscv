# dsh: a DeepSeek harness for the Megrez board, 2026-11-09

`dsh` is a small agent loop that lets a DeepSeek model operate the board. It
runs on the host, keeps the model on the host, and reaches the board through
the same serial bridge the developer tooling uses. The board needs no network
access, no extra package and no credentials.

## Endpoint and key

The harness speaks the Anthropic Messages protocol, which DeepSeek exposes at
`https://api.deepseek.com/anthropic` (the same endpoint and key Claude Code
uses for this workspace):

| Setting | Source | Default |
| --- | --- | --- |
| endpoint | `ANTHROPIC_BASE_URL` | `https://api.deepseek.com/anthropic` |
| model | `DSH_MODEL` or `ANTHROPIC_MODEL` (a `[1m]` suffix is stripped) | `deepseek-flash` |
| key | `DSH_API_KEY`, then `DEEPSEEK_API_KEY`, then `ANTHROPIC_API_KEY` | none |

The key is read at run time and is never written to disk, logs, request
traces or the board. The client uses only the Python standard library.

## Usage

```bash
# One task; the model decides which board tools to call.
tools/riscv/dsh "在板上运行 uname -a，并报告内核版本"

# Show the resolved endpoint, model and tool list without calling the API.
tools/riscv/dsh --dry-run

# Let the model reboot the board (opt-in: the tool is hidden otherwise).
tools/riscv/dsh --allow-reboot "重启开发板并确认控制台恢复"

# More room for a multi-step task.
tools/riscv/dsh --max-turns 20 "检查 / 的剩余空间、运行 4 个 CPU 密集进程并报告每个核的占用"
```

The serial bridge must be running (the harness only writes to its FIFO and
reads its log):

```bash
tools/riscv/board/megrez_console.py bridge /dev/ttyUSB0 \
    --log /tmp/board3.log --fifo /tmp/board3.cmd
```

`--log` / `--fifo` point the harness at a different bridge. `--quiet` drops
the tool trace, which otherwise goes to stderr while the final answer goes to
stdout.

## Tools

| Tool | What it does |
| --- | --- |
| `board_run` | Type a shell command at the console, wait for its end marker, return the output (bridge noise filtered, last 6,000 characters kept). |
| `board_read_log` | Return the most recent console text; used after a command that produced too much output or after a reboot. |
| `board_push` | Copy a host file to an absolute board path and require the guest's sha256 to match. Local files must be inside the repository or `/tmp`, and at most 256 KiB. |
| `board_reboot` | `sync; echo b > /proc/sysrq-trigger`; only exposed with `--allow-reboot`. |

The system prompt tells the model to prefer read-only commands, explain
destructive ones, never print credentials, and avoid rebooting unless the task
requires it. The harness itself exposes no host shell: the model can only
reach the board through these four tools.

## Verified end to end

```
$ tools/riscv/dsh --max-turns 6 "在开发板上运行 hostname 和 uname -a，然后用一句话汇报结果。"
[tool] board_run {"command": "hostname; uname -a"}
[tool] asterinas
       Linux asterinas 5.13.0 #1 SMP Thu Jan  1 00:00:00 UTC 1970 riscv64 GNU/Linux
开发板主机名为 `asterinas`，运行的是 Linux 5.13.0（SMP，riscv64）内核……
```

The model chose the tool, the command ran over the serial console, and the
result came back through the Messages API. The harness is unit-tested with a
fake client and fake tools (tool-use round trip, max-turn guard, environment
handling, reboot gating, push path restrictions); the tests do not need an API
key or a board.

## Not included

- Running the harness *on* the board. The board can reach `api.deepseek.com`
  (see `2026-11-04-remote-access-and-deepseek.md`), but that would put the API
  key on the guest; the host-side design avoids it.
- A host shell tool, arbitrary local file reads or network access for the
  model. Add a tool deliberately, with the same restriction pattern as
  `board_push`, if a task needs it.
