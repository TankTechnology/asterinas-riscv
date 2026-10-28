"""Unit tests for the guarded Megrez board session contract."""

import contextlib
import io
import os
import socket
import tempfile
import threading
import time
import tty
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.riscv import megrez_board_session as board


def _required_args() -> list[str]:
    return ["serial", "--booti", "kernel", "--initrd", "initrd", "--dtb", "board.dtb"]


def _parse_fails(args: list[str]) -> None:
    with (
        contextlib.redirect_stderr(io.StringIO()),
        unittest.TestCase().assertRaises(SystemExit),
    ):
        board.parse_args(args)


def _make_session() -> board.BoardSession:
    session = board.BoardSession.__new__(board.BoardSession)
    session.fd = -1
    session.confirm = False
    session.milestones = {}
    session.milestone_transcript = ""
    session._milestone_tail = ""
    session._next_milestone = 0
    session._markers = dict(board.MILESTONES)
    session._log = mock.Mock()
    return session

__all__ = tuple(
    name
    for name in globals()
    if name not in {"annotations"} and not name.startswith("__")
)
