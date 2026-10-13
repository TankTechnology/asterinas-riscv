# SPDX-License-Identifier: MPL-2.0

"""Compatibility alias for `tools.debian.rootfs.contract`.

The module moved to the architecture-neutral `tools.debian` package. Aliasing
the module object, rather than re-exporting its names, keeps
`tools.riscv.debian.rootfs.contract` importable and keeps attribute patching through
either path effective.
"""

import sys

from tools.debian.rootfs import contract as _impl

sys.modules[__name__] = _impl
