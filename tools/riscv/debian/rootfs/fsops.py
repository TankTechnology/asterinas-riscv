# SPDX-License-Identifier: MPL-2.0

"""Compatibility alias for `tools.debian.rootfs.fsops`.

The module moved to the architecture-neutral `tools.debian` package. Aliasing
the module object, rather than re-exporting its names, keeps
`tools.riscv.debian.rootfs.fsops` importable and keeps attribute patching through
either path effective.
"""

import sys

from tools.debian.rootfs import fsops as _impl

sys.modules[__name__] = _impl
