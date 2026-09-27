#!/bin/sh
# SPDX-License-Identifier: MPL-2.0

# systemd may classify Asterinas as a container and skip its own final sync.
# Run after services stop, before the kernel reboot syscall.
exec /usr/bin/sync
