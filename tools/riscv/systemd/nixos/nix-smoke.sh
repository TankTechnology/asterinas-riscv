#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# NIXOS-STAGE2-M1 smoke — run nix-profile-installed binaries *by bare name*
# inside a systemd service and emit markers the boot driver greps for. The
# binaries resolve through PATH (which the activation wired to the profile),
# proving the "nix profile -> systemd environment" link works.

echo "___NIX_SMOKE_BEGIN___"
echo "PATH=$PATH"

for b in hello nixos-info fortune; do
    echo "___NIX_RUN_${b}___"
    "$b" 2>&1 || echo "___NIX_${b}_FAILED___"
done

echo "___NIX_RUN_jq___"
jq --version 2>&1 || echo "___NIX_jq_FAILED___"

echo "___NIX_RUN_curl___"
curl --version 2>&1 | head -n 1 || echo "___NIX_curl_FAILED___"

echo "___NIX_SMOKE_END___"
