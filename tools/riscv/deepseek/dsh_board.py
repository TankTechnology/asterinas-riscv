# SPDX-License-Identifier: MPL-2.0
"""A local on-board DeepSeek board agent ("dsh --on-board").

Naming note: this is NOT the official DeepSeek Harness
(https://github.com/deepseek-ai/deepseek-harness, `@deepseek-ai/dsh`), which is
a TypeScript/Cordis plugin runtime that runs on the board as `dsh web` (see
docs/porting/2026-11-10-node24-jit-and-dsh.md). The shared name is a local
collision; this file is a minimal single-tool agent with no sandbox or
permission layer.

This file is deliberately self-contained: it is pushed to the board and run
there with the system Python, using only the standard library. The model can
run shell commands on the board itself (that is the point: the harness lives
inside the OS it operates).

Configuration, in order of precedence:
  --base-url / DSH_BASE_URL        default https://zgc.apihy.com/v1
  --model    / DSH_MODEL           default deepseek-flash
  --key-file / DSH_API_KEY / DEEPSEEK_API_KEY / ANTHROPIC_API_KEY
                                   default key file ~/.config/dsh/key

The API key is never printed and never written by this program.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://zgc.apihy.com/v1"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_KEY_FILE = Path.home() / ".config" / "dsh" / "key"

SYSTEM_PROMPT = """\
You are dsh, a harness running inside a Milk-V Megrez RISC-V development
board. The board runs Asterinas with a Debian root filesystem; you are root.
Use the run_shell tool to inspect and test the system.

Rules:
- Prefer read-only commands. Explain any command that changes files, services
  or the boot state before running it.
- Never print credentials, API keys or private keys.
- Keep commands short and bounded; the tool has a timeout.
- This board is the machine you are running on; rebooting it ends your session.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a shell command on this board and return its "
            "combined output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_s": {
                        "type": "number",
                        "description": "Timeout in seconds (default 30)",
                    },
                },
                "required": ["command"],
            },
        },
    }
]


class ApiError(RuntimeError):
    pass


def read_key(key_file: Path | None = None) -> str:
    for name in ("DSH_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
        value = os.environ.get(name)
        if value:
            return value.strip()
    path = key_file or DEFAULT_KEY_FILE
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ApiError(f"no API key (env or {path}): {error}") from error


class ChatClient:
    def __init__(
        self, api_key: str, base_url: str, model: str, timeout: float = 120.0
    ) -> None:
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def complete(
        self, messages: list[dict], tools: list[dict], max_tokens: int = 2048
    ) -> dict:
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self._api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:400]
            raise ApiError(f"HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ApiError(f"transport error: {error}") from error


def run_shell(command: str, timeout_s: float = 30.0) -> str:
    if not command.strip():
        return "error: empty command"
    try:
        completed = subprocess.run(
            ["/bin/sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {timeout_s:.0f}s"
    output = (completed.stdout or "") + (completed.stderr or "")
    if len(output) > 6000:
        output = "[output truncated to the last 6000 characters]\n" + output[-6000:]
    if not output:
        output = "(no output)"
    return f"exit={completed.returncode}\n{output}"


def run_agent(
    client: ChatClient,
    prompt: str,
    *,
    max_turns: int = 8,
    trace=None,
) -> str:
    say = trace or (lambda _message: None)
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    for _turn in range(max_turns):
        response = client.complete(messages, TOOLS)
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        messages.append(message)

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return (message.get("content") or "").strip() or "(no reply)"
        for call in tool_calls:
            function = call.get("function") or {}
            name = function.get("name", "")
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            say(f"[tool] {name} {json.dumps(arguments, ensure_ascii=False)[:300]}")
            if name == "run_shell":
                output = run_shell(
                    str(arguments.get("command", "")),
                    float(arguments.get("timeout_s", 30.0)),
                )
            else:
                output = f"error: unknown tool {name}"
            say(f"[tool] {output[:600]}")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": output,
                }
            )
    raise ApiError(f"no final answer within {max_turns} turns")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dsh_board", description="On-board DeepSeek harness"
    )
    parser.add_argument("prompt", nargs="*")
    parser.add_argument("--base-url", default=os.environ.get("DSH_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=os.environ.get("DSH_MODEL", DEFAULT_MODEL))
    parser.add_argument("--key-file", default=None)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--check", action="store_true", help="one completion, no tools")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    prompt = " ".join(args.prompt).strip()
    if not prompt and not sys.stdin.isatty() and not args.check:
        prompt = sys.stdin.read().strip()

    try:
        key = read_key(Path(args.key_file) if args.key_file else None)
        client = ChatClient(key, args.base_url, args.model)
        if args.check:
            response = client.complete(
                [{"role": "user", "content": "只回复两个字：正常"}], [], max_tokens=64
            )
            message = response["choices"][0]["message"]
            print("DSH_ONBOARD_OK model=" + str(response.get("model")))
            print((message.get("content") or "").strip())
            return 0
        if not prompt:
            parser.error("no prompt")
        answer = run_agent(
            client,
            prompt,
            max_turns=args.max_turns,
            trace=(lambda _m: None) if args.quiet else (
                lambda message: print(message, file=sys.stderr, flush=True)
            ),
        )
    except ApiError as error:
        print(f"dsh_board: {error}", file=sys.stderr)
        return 1
    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
