# SPDX-License-Identifier: MPL-2.0
"""Board tools exposed to the model.

Everything goes through the same serial bridge the developer tools use, so
the harness does not depend on the board's network or on the portal session.
The tools are deliberately small: run a command, read the console, push a
file, and (opt-in) reboot.
"""

from __future__ import annotations

from pathlib import Path

from tools.riscv.board import megrez_console

REPO_ROOT = Path(__file__).resolve().parents[3]


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


class BoardTools:
    def __init__(
        self,
        log: str = "/tmp/board3.log",
        fifo: str = "/tmp/board3.cmd",
        *,
        allow_reboot: bool = False,
        command_timeout: float = 60.0,
        max_push_bytes: int = 256 * 1024,
    ) -> None:
        self.console = megrez_console.Console(log, fifo)
        self.log_path = Path(log)
        self.allow_reboot = allow_reboot
        self.command_timeout = command_timeout
        self.max_push_bytes = max_push_bytes

    def schemas(self) -> list[dict]:
        tools = [
            _tool(
                "board_run",
                "Run a shell command on the board through the serial console "
                "and return its output. The board is a Milk-V Megrez running "
                "Asterinas with a Debian root filesystem; the shell is root.",
                {
                    "command": {"type": "string", "description": "Shell command"},
                    "timeout_s": {
                        "type": "number",
                        "description": "Seconds to wait for the marker (default 60)",
                    },
                },
                ["command"],
            ),
            _tool(
                "board_read_log",
                "Read the most recent console output from the serial bridge log. "
                "Useful after a command that produced too much output or after "
                "a reboot.",
                {
                    "tail_chars": {
                        "type": "number",
                        "description": "How many characters to return (default 4000)",
                    }
                },
                [],
            ),
            _tool(
                "board_push",
                "Copy a host file to an absolute path on the board and verify "
                "its sha256 on the guest. Local files must be inside the "
                "repository or /tmp, and the file must be at most 256 KiB.",
                {
                    "local_path": {"type": "string"},
                    "remote_path": {
                        "type": "string",
                        "description": "Absolute path on the board, e.g. /tmp/x.sh",
                    },
                },
                ["local_path", "remote_path"],
            ),
        ]
        if self.allow_reboot:
            tools.append(
                _tool(
                    "board_reboot",
                    "Reboot the board through /proc/sysrq-trigger. Only use it "
                    "when the task explicitly requires a reboot.",
                    {},
                    [],
                )
            )
        return tools

    def call(self, name: str, arguments: dict) -> str:
        try:
            if name == "board_run":
                return self._run(
                    str(arguments.get("command", "")),
                    float(arguments.get("timeout_s", self.command_timeout)),
                )
            if name == "board_read_log":
                return self._read_log(int(arguments.get("tail_chars", 4000)))
            if name == "board_push":
                return self._push(
                    str(arguments.get("local_path", "")),
                    str(arguments.get("remote_path", "")),
                )
            if name == "board_reboot":
                return self._reboot()
            return f"error: unknown tool {name}"
        except Exception as error:  # surface tool errors to the model
            return f"error: {type(error).__name__}: {error}"

    def _run(self, command: str, timeout: float) -> str:
        if not command.strip():
            return "error: empty command"
        text, complete = self.console.run(command, timeout=timeout)
        cleaned = megrez_console.ANSI.sub("", text)
        # The bridge logs its own raw-mode toggles into the console log.
        cleaned = "\n".join(
            line for line in cleaned.splitlines() if not line.startswith("[bridge]")
        ).strip()
        if not complete:
            cleaned += "\n[the command did not produce its end marker within " \
                       f"{timeout:.0f}s; it may still be running]"
        limit = 6000
        if len(cleaned) > limit:
            cleaned = cleaned[-limit:]
            cleaned = "[output truncated to the last 6000 characters]\n" + cleaned
        return cleaned or "(no output)"

    def _read_log(self, tail_chars: int) -> str:
        tail_chars = max(200, min(tail_chars, 20000))
        data = self.log_path.read_bytes()[-tail_chars:]
        return megrez_console.ANSI.sub("", data.decode("utf-8", "replace"))

    def _push(self, local_path: str, remote_path: str) -> str:
        local = Path(local_path).expanduser().resolve()
        roots = (REPO_ROOT.resolve(), Path("/tmp").resolve())
        if not any(local.is_relative_to(root) for root in roots):
            return f"error: {local} is outside the repository and /tmp"
        if not local.is_file():
            return f"error: {local} is not a regular file"
        if not remote_path.startswith("/"):
            return "error: remote_path must be absolute"
        size = local.stat().st_size
        if size > self.max_push_bytes:
            return f"error: {size} bytes exceeds the {self.max_push_bytes}-byte limit"
        if not self.console.push_verified(str(local), remote_path):
            return "error: push failed or the guest's digest did not match"
        return f"pushed {size} bytes to {remote_path} (sha256 verified)"

    def _reboot(self) -> str:
        if not self.allow_reboot:
            return "error: rebooting is disabled; pass --allow-reboot"
        self.console.type_line("sync; echo b > /proc/sysrq-trigger")
        return "reboot requested; the board will come back in about a minute"
