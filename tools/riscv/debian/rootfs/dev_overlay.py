# SPDX-License-Identifier: MPL-2.0

"""Compatibility alias for `tools.debian.rootfs.dev_overlay`.

The module moved to the architecture-neutral `tools.debian` package. Aliasing
the module object, rather than re-exporting its names, keeps
`tools.riscv.debian.rootfs.dev_overlay` importable and keeps attribute patching through
either path effective.
"""

import sys

from tools.debian.rootfs import dev_overlay as _impl

sys.modules[__name__] = _impl
