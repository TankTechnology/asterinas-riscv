# SPDX-License-Identifier: MPL-2.0

"""A minimal DeepSeek-driven board agent (locally named "dsh").

This package is NOT the official DeepSeek Harness
(https://github.com/deepseek-ai/deepseek-harness, `@deepseek-ai/dsh`). The
official harness is a TypeScript/Cordis plugin runtime that runs on the board
as `dsh web` and is documented in
docs/porting/2026-11-10-node24-jit-and-dsh.md. The shared name is a local
collision; this package is a small harness with four board tools and no
sandbox or permission layer.
"""
