# SPDX-License-Identifier: MPL-2.0

"""The Megrez board toolchain.

Three tools work on a board that is already running: `console` owns the
serial line, `flash` replaces the root partition through it, `portal` logs
the board's address into the campus portal, and `prepare` puts a kernel
inside a freshly built image before that image is ever shipped.

Each tool is also a standalone script; the package exists so the four live
together and can be reached through one entry point:

    python3 tools/riscv/board console bridge /dev/ttyUSB0
    python3 tools/riscv/board prepare --image ... --kernel ...
    python3 tools/riscv/board flash --image ... --url ...
    python3 tools/riscv/board portal --expect-ip 58.206.232.122
"""
