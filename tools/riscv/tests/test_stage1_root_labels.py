# SPDX-License-Identifier: MPL-2.0

"""Stage1 must recognise the root label of every profile that can be booted.

The probe finds its root by ext2 volume label, so a profile whose label is
absent from Stage1 does not fail loudly: the probe reports no match on a root
it was handed, retries until the discovery deadline, and the boot ends in
root-device-timeout. That happened to the desktop-drm profile, whose label
existed only in a separately built Stage1 binary; nothing in the tree connected
the profile to the list, so the two could drift apart silently.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from tools.riscv.debian.rootfs.profiles import get_profile

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
STAGE1_SOURCE = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/stage1_init.c"

_STRING_LABEL_RE = re.compile(
    r"static const unsigned char (\w+)\[EXT2_LABEL_LENGTH\]\s*=\s*\"([A-Z0-9_]+)\""
)
_CHAR_LABEL_RE = re.compile(
    r"static const unsigned char (\w+)\[EXT2_LABEL_LENGTH\]\s*=\s*\{(.*?)\}",
    re.DOTALL,
)
_CHARACTER_RE = re.compile(r"'([A-Za-z0-9_])'")
_MODE_FUNCTION_RE = re.compile(
    r"ext2_superblock_matches_mode\(.*?\n\{(.*?)\n\}", re.DOTALL
)

# One label per bootable profile. Adding a profile without teaching Stage1 its
# label is the failure this test exists to prevent, so the list is explicit
# rather than derived from whatever the source happens to contain.
PROFILE_NAMES = (
    "minimal-m1",
    "systemd-m2",
    "desktop-m3",
    "desktop-m4",
    "desktop-m5-network",
    "desktop-drm",
    "browser-m5",
    "desktop-m9-software",
    "browser-web",
)


def stage1_label_constants() -> dict[str, str]:
    """Return Stage1's declared root-label constants, as name -> label."""

    source = STAGE1_SOURCE.read_text(encoding="utf-8")
    constants = dict(_STRING_LABEL_RE.findall(source))
    # The interactive label is spelled as a character list: it fills all
    # sixteen bytes, so it has no NUL terminator for a string literal to use.
    for name, body in _CHAR_LABEL_RE.findall(source):
        constants[name] = "".join(_CHARACTER_RE.findall(body))
    return constants


def stage1_matched_labels() -> set[str]:
    """Return the labels the mode dispatcher actually compares against.

    Declaring a label is not matching it. A constant that no branch names is
    dead code that reads like support, which is precisely how the desktop-drm
    label came to be missing from the shipped binary while its profile existed
    in the tree.
    """

    source = STAGE1_SOURCE.read_text(encoding="utf-8")
    match = _MODE_FUNCTION_RE.search(source)
    if match is None:
        return set()
    body = match.group(1)
    constants = stage1_label_constants()
    return {
        label for name, label in constants.items() if name in body
    }


class Stage1RootLabelTests(unittest.TestCase):
    def test_stage1_source_is_where_this_expects_it(self) -> None:
        self.assertTrue(STAGE1_SOURCE.is_file(), "Stage1 source is missing")

    def test_the_dispatcher_was_actually_parsed(self) -> None:
        # Without this, a refactor that renames the function would empty the
        # set below and turn the real assertion into one that cannot fail.
        self.assertIn(
            "ASTER_DEBIANROOT",
            stage1_matched_labels(),
            "the mode dispatcher was not parsed; the invariant below is vacuous",
        )

    def test_every_profile_label_is_matched_by_stage1(self) -> None:
        matched = stage1_matched_labels()
        unmatched = {
            name: get_profile(name).root_label
            for name in PROFILE_NAMES
            if get_profile(name).root_label not in matched
        }
        self.assertEqual(
            unmatched,
            {},
            "Stage1 does not match these profile root labels, so booting those "
            "profiles ends in root-device-timeout",
        )
