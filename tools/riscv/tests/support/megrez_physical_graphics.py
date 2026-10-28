#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the bounded Megrez physical graphics host gate."""

from __future__ import annotations

import hashlib
import importlib
import base64
import io
import json
import os
import shutil
from dataclasses import asdict
from pathlib import Path
import struct
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib


MODULE_PATH = Path(__file__).resolve().parents[2] / "megrez_physical_graphics.py"
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
MAKEFILE_PATH = REPOSITORY_ROOT / "Makefile"
README_PATH = Path(__file__).resolve().parents[2] / "README.md"


def png_payload(*, width: int = 1920, height: int = 1080, value: int = 0x35) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", checksum)
        )

    row = b"\0" + bytes((value, 0x77, 0xB5)) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


def load_gate(test: unittest.TestCase):
    test.assertTrue(MODULE_PATH.is_file(), "Megrez physical graphics gate is missing")
    return importlib.import_module("tools.riscv.megrez_physical_graphics")

NONCES = (
        "0123",
        "9876",
        "0011",
    )

__all__ = tuple(
    name
    for name in globals()
    if name not in {"annotations"} and not name.startswith("__")
)
