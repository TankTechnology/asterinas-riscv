# SPDX-License-Identifier: MPL-2.0
"""dsh: a DeepSeek-driven harness that operates the Megrez board.

The harness keeps the model on the host and gives it four board tools over
the serial bridge: run a command, read the console, push a verified file, and
(opt-in) reboot. The API key comes from the environment and is never printed.
"""

from __future__ import annotations

import argparse
import json
import sys

from tools.riscv.deepseek import client as api
from tools.riscv.deepseek.board_tools import BoardTools

SYSTEM_PROMPT = """\
You are dsh, a harness that operates a Milk-V Megrez RISC-V development board.
The board runs Asterinas (a Rust OS) with a Debian root filesystem; the serial
console gives you a root shell. Use the board tools to inspect and test the
system. The serial bridge is the only transport: commands are typed at the
console and output is read back from its log.

Rules:
- Prefer read-only commands. Explain any command that changes files, services
  or the boot state before running it.
- Never print credentials, API keys or the contents of private keys.
- A reboot takes about a minute and may interrupt the session; only reboot
  when the task explicitly requires it.
- The board's clock and network are managed by systemd-timesyncd and the
  campus portal; do not reconfigure networking unless asked.
- Keep commands short. If output is cut off, read the console log.
"""


class AgentError(RuntimeError):
    pass


def run_agent(
    messages_client,
    tools: BoardTools,
    prompt: str,
    *,
    max_turns: int = 12,
    trace=None,
) -> str:
    """Run the tool-use loop and return the model's final text."""

    say = trace or (lambda _message: None)
    messages: list[dict] = [{"role": "user", "content": prompt}]

    for _turn in range(max_turns):
        response = messages_client.create(
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=tools.schemas(),
        )
        content = response.get("content") or []
        messages.append({"role": "assistant", "content": content})

        text_parts = [
            block.get("text", "")
            for block in content
            if block.get("type") == "text" and block.get("text")
        ]
        tool_uses = [block for block in content if block.get("type") == "tool_use"]
        if not tool_uses:
            return "\n".join(text_parts).strip() or "(the model returned no text)"
        # Commentary that accompanies a tool call is progress, not the answer.
        for part in text_parts:
            say(part)

        results = []
        for use in tool_uses:
            name = use.get("name", "")
            arguments = use.get("input") or {}
            say(f"[tool] {name} {json.dumps(arguments, ensure_ascii=False)[:400]}")
            output = tools.call(name, arguments)
            say(f"[tool] {output[:800]}")
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": use.get("id", ""),
                    "content": output,
                }
            )
        messages.append({"role": "user", "content": results})

    raise AgentError(f"the model did not finish within {max_turns} turns")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dsh",
        description="Run a DeepSeek model against the Megrez board over the "
        "serial bridge.",
    )
    parser.add_argument("prompt", nargs="*", help="task for the model (stdin if empty)")
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--log", default="/tmp/board3.log")
    parser.add_argument("--fifo", default="/tmp/board3.cmd")
    parser.add_argument("--command-timeout", type=float, default=60.0)
    parser.add_argument(
        "--allow-reboot",
        action="store_true",
        help="expose the board_reboot tool",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the tools and the resolved endpoint without calling the API",
    )
    parser.add_argument("--quiet", action="store_true", help="no tool trace on stderr")
    args = parser.parse_args(argv)

    prompt = " ".join(args.prompt).strip()
    if not prompt and not args.dry_run:
        prompt = sys.stdin.read().strip()
    if not prompt and not args.dry_run:
        parser.error("no prompt")

    tools = BoardTools(
        args.log,
        args.fifo,
        allow_reboot=args.allow_reboot,
        command_timeout=args.command_timeout,
    )

    try:
        key = api.key_from_environment()
    except api.ApiError as error:
        if not args.dry_run:
            print(f"dsh: {error}", file=sys.stderr)
            return 2
        key = ""
    messages_client = api.MessagesClient(
        key,
        base_url=args.base_url,
        model=args.model,
    )

    if args.dry_run:
        print(f"endpoint: {messages_client.base_url}")
        print(f"model:    {messages_client.model}")
        for tool in tools.schemas():
            print(f"tool:     {tool['name']}")
        return 0

    trace = (lambda _message: None) if args.quiet else (
        lambda message: print(message, file=sys.stderr, flush=True)
    )
    try:
        answer = run_agent(
            messages_client,
            tools,
            prompt,
            max_turns=args.max_turns,
            trace=trace,
        )
    except api.ApiError as error:
        print(f"dsh: API error: {error}", file=sys.stderr)
        return 1
    except AgentError as error:
        print(f"dsh: {error}", file=sys.stderr)
        return 1

    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
