#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""The dsh harness: tool loop, tool schemas and environment handling."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tools.riscv.deepseek import client, runner
from tools.riscv.deepseek import dsh_board
from tools.riscv.deepseek.board_tools import BoardTools


class FakeClient:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def create(self, **kwargs) -> dict:
        self.requests.append(kwargs)
        return self.responses.pop(0)


class FakeTools:
    def __init__(self, outputs: list[str] | None = None) -> None:
        self.outputs = list(outputs or [])
        self.calls: list[tuple[str, dict]] = []

    def schemas(self) -> list[dict]:
        return [{"name": "board_run", "description": "", "input_schema": {}}]

    def call(self, name: str, arguments: dict) -> str:
        self.calls.append((name, arguments))
        return self.outputs.pop(0)


def tool_use_response(command: str) -> dict:
    return {
        "content": [
            {"type": "text", "text": "checking"},
            {
                "type": "tool_use",
                "id": "call-1",
                "name": "board_run",
                "input": {"command": command},
            },
        ]
    }


class AgentLoopTests(unittest.TestCase):
    def test_runs_a_tool_and_returns_the_final_text(self) -> None:
        fake_client = FakeClient(
            [
                tool_use_response("uname -a"),
                {"content": [{"type": "text", "text": "board says 5.13.0"}]},
            ]
        )
        fake_tools = FakeTools(["Linux asterinas 5.13.0"])

        answer = runner.run_agent(fake_client, fake_tools, "report the kernel")

        self.assertEqual(answer, "board says 5.13.0")
        self.assertEqual(fake_tools.calls, [("board_run", {"command": "uname -a"})])
        # The tool result is sent back as a user message with a tool_result block.
        # (The recorded request shares the live message list, so index it.)
        result_message = fake_client.requests[1]["messages"][2]
        self.assertEqual(result_message["role"], "user")
        self.assertEqual(result_message["content"][0]["type"], "tool_result")
        self.assertEqual(result_message["content"][0]["tool_use_id"], "call-1")

    def test_stops_after_max_turns(self) -> None:
        fake_client = FakeClient([tool_use_response("true") for _ in range(3)])
        fake_tools = FakeTools(["ok", "ok", "ok"])
        with self.assertRaises(runner.AgentError):
            runner.run_agent(fake_client, fake_tools, "loop", max_turns=3)
        self.assertEqual(len(fake_tools.calls), 3)


class EnvironmentTests(unittest.TestCase):
    def test_model_suffix_is_stripped(self) -> None:
        with mock.patch.dict(os.environ, {"DSH_MODEL": "deepseek-flash[1m]"}):
            self.assertEqual(client.model_from_environment(), "deepseek-flash")

    def test_missing_key_is_reported(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"DSH_API_KEY": "", "DEEPSEEK_API_KEY": "", "ANTHROPIC_API_KEY": ""},
        ):
            with self.assertRaisesRegex(client.ApiError, "no API key"):
                client.key_from_environment()

    def test_dry_run_prints_the_endpoint_without_a_key(self) -> None:
        with mock.patch.dict(os.environ, {"DSH_API_KEY": ""}, clear=False):
            with mock.patch.dict(
                os.environ,
                {"DEEPSEEK_API_KEY": "", "ANTHROPIC_API_KEY": ""},
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(runner.main(["--dry-run"]), 0)
        self.assertIn("endpoint:", output.getvalue())
        self.assertIn("board_run", output.getvalue())


class BoardToolTests(unittest.TestCase):
    def test_reboot_tool_is_hidden_until_enabled(self) -> None:
        tools = BoardTools("/tmp/x.log", "/tmp/x.cmd")
        names = [tool["name"] for tool in tools.schemas()]
        self.assertIn("board_run", names)
        self.assertNotIn("board_reboot", names)
        self.assertIn("disabled", tools.call("board_reboot", {}))

        enabled = BoardTools("/tmp/x.log", "/tmp/x.cmd", allow_reboot=True)
        self.assertIn(
            "board_reboot", [tool["name"] for tool in enabled.schemas()]
        )

    def test_push_rejects_files_outside_the_allowed_roots(self) -> None:
        tools = BoardTools("/tmp/x.log", "/tmp/x.cmd")
        result = tools.call(
            "board_push", {"local_path": "/etc/passwd", "remote_path": "/tmp/x"}
        )
        self.assertIn("outside the repository and /tmp", result)

    def test_unknown_tool_is_an_error_string(self) -> None:
        tools = BoardTools("/tmp/x.log", "/tmp/x.cmd")
        self.assertIn("unknown tool", tools.call("no_such_tool", {}))


class OnBoardHarnessTests(unittest.TestCase):
    def test_agent_runs_a_shell_tool_and_returns_text(self) -> None:
        class FakeChat:
            def __init__(self) -> None:
                self.calls: list[list[dict]] = []
                self.responses = [
                    {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "c1",
                                            "type": "function",
                                            "function": {
                                                "name": "run_shell",
                                                "arguments": '{"command": "echo hi"}',
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                    {
                        "choices": [
                            {"message": {"role": "assistant", "content": "it said hi"}}
                        ]
                    },
                ]

            def complete(self, messages, tools, max_tokens=2048):
                self.calls.append(list(messages))
                return self.responses.pop(0)

        fake = FakeChat()
        answer = dsh_board.run_agent(fake, "say hi")
        self.assertEqual(answer, "it said hi")
        tool_message = fake.calls[1][-1]
        self.assertEqual(tool_message["role"], "tool")
        self.assertEqual(tool_message["tool_call_id"], "c1")
        self.assertIn("hi", tool_message["content"])

    def test_run_shell_reports_exit_code(self) -> None:
        result = dsh_board.run_shell("echo hello")
        self.assertIn("exit=0", result)
        self.assertIn("hello", result)

    def test_key_can_come_from_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "key"
            key_path.write_text("sk-test\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"DSH_API_KEY": "", "DEEPSEEK_API_KEY": "", "ANTHROPIC_API_KEY": ""},
            ):
                self.assertEqual(dsh_board.read_key(key_path), "sk-test")


if __name__ == "__main__":
    unittest.main()
