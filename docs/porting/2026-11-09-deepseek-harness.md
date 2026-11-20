# dsh: a DeepSeek harness for the Megrez board, 2026-11-09

> **Naming correction (2026-11-18).** This document describes the local,
> minimal `tools/riscv/dsh` board agent. It is **not** the official DeepSeek
> Harness ([deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness),
> `@deepseek-ai/dsh`), which is a TypeScript / Cordis plugin runtime that runs
> on the board as `dsh web` and is documented in
> [2026-11-10-node24-jit-and-dsh.md](2026-11-10-node24-jit-and-dsh.md). The
> shared name is a local collision that led us to reason about the wrong
> project's sandbox and permission layer. Prefer "board agent" when referring
> to this tool.

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

- A host shell tool, arbitrary local file reads or network access for the
  model. Add a tool deliberately, with the same restriction pattern as
  `board_push`, if a task needs it.

## On-board variant: dsh inside the OS

`tools/riscv/deepseek/dsh_board.py` is a self-contained, standard-library
script that runs *on the board* with the system Python. It speaks the OpenAI
protocol (the campus gateway `https://zgc.apihy.com/v1`, a New API
deployment) and exposes one tool, `run_shell`, which runs commands on the
board itself — the harness lives inside the OS it operates.

Deploying it (all over the serial bridge; the key file is transferred with
the guest's terminal echo off so it never appears in the console log):

```bash
# host: push the harness
tools/riscv/board/megrez_console.py push tools/riscv/deepseek/dsh_board.py \
    /root/dsh_board.py

# host: write the key locally with mode 600, then on the console
#   stty -echo
#   cat > /tmp/dsh-key        (push the key file; payload is not echoed)
#   install -d -m 700 /root/.config/dsh
#   install -m 600 /tmp/dsh-key /root/.config/dsh/key
#   rm -f /tmp/dsh-key; stty echo
```

`dsh_board.py` reads the key from `DSH_API_KEY`, `DEEPSEEK_API_KEY` or
`ANTHROPIC_API_KEY` first, then from `~/.config/dsh/key`. It never prints the
key. Endpoint and model come from `DSH_BASE_URL`/`DSH_MODEL` or
`--base-url`/`--model`.

Verified on the board:

```
$ python3 /root/dsh_board.py --check
DSH_ONBOARD_OK model=deepseek-v4.1-flash
正常

$ python3 /root/dsh_board.py "在板上运行 uname -a 和 df -h /，然后用一句话汇报"
[tool] run_shell {"command": "uname -a"}
[tool] exit=0
Linux asterinas 5.13.0 #1 SMP ... riscv64 GNU/Linux
[tool] run_shell {"command": "df -h /"}
[tool] exit=0
/dev/mmcblk0p2  3.0G  2.7G  130M  96% /
一句话汇报：板子运行 RISC-V 64 位 Linux（内核 5.13.0），根分区仅剩 130M。
```

The board reaches the gateway through the campus portal; the clock must be
correct (systemd-timesyncd handles that) and the portal session current.
No API key is ever committed to the repository; the board keeps it only in
`/root/.config/dsh/key` with mode 0600.
