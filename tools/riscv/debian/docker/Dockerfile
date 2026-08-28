# SPDX-License-Identifier: MPL-2.0

# Keep this base immutable: the Firefox builder must not silently change when
# the mutable 20260826 tag is republished.
FROM asterinas/debian-riscv-builder@sha256:d53fb72d643698efb412985650161fc5deff60eba30c2457e173dca9ce9f1acd

ARG DEBIAN_FRONTEND=noninteractive

# build_rootfs.sh deliberately performs these operations at image-build time
# so a browser image never has to regenerate sysusers, journal, linker, or font
# caches during its first Asterinas boot.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg='7:6.1.1-3ubuntu5' \
        fontconfig='2.15.0-1.1ubuntu2' \
        systemd='255.4-1ubuntu8.17' \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
