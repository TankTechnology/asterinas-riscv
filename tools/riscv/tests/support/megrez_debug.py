"""Fast contracts for the simulation-first Megrez debug workflow."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import zlib
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.riscv import megrez_debug_board as board_module
from tools.riscv.debian.rootfs.desktop_network_gate import (
    DESKTOP_NETWORK_MEGREZ_MILESTONES,
)
from tools.riscv.megrez_debug_contract import (
    DEBIAN_BROWSER_ARTIFACT_ORDER,
    DEBIAN_BROWSER_MARKERS,
    MAX_ARTIFACT_BYTES,
    METADATA_ARTIFACT_NAMES,
    BROWSER_ROOT_IMAGE_BYTES, ROOT_IMAGE_BYTES,
    ArtifactIdentity,
    DebugContractError,
    DebugPlan,
    StageResult,
)
from tools.riscv.megrez_contract import (
    ROOTFS_ARTIFACT_NAMES,
    ROOTFS_METADATA_ARTIFACT_NAMES,
)
from tools.riscv.megrez_debian_shell_contract import SHELL_ARTIFACT_ORDER
from tools.riscv.debian.rootfs.desktop_network_gate import NetworkMode
from tools.riscv.megrez_gmac_gate import GateTarget, physical_bootargs
from tools.riscv.megrez_debug_simulation import SimulationError, simulate_fast
from tools.riscv.megrez_debug_board import (
    BoardRunConfig,
    BoardRunFailure,
    BoardTermination,
    BoardTransport,
    BoardTransportError,
    KERNEL_COMPRESSED_ADDRESS,
    RealBoardOperations,
    ensure_board_artifacts,
    run_board,
)
from tools.riscv.megrez_board_session import MEGREZ_USB_HOST_COMMAND
from tools.riscv.megrez_preboard import RecoveryEvidence

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]

__all__ = tuple(
    name
    for name in globals()
    if name not in {"annotations"} and not name.startswith("__")
)
