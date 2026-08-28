# SPDX-License-Identifier: MPL-2.0

# Keep this base immutable: the Firefox builder must not silently change when
# the mutable 20260826 tag is republished.
FROM asterinas/debian-riscv-builder@sha256:d53fb72d643698efb412985650161fc5deff60eba30c2457e173dca9ce9f1acd

ARG DEBIAN_FRONTEND=noninteractive

ARG DEBIAN_ARCHIVE_KEYRING_VERSION=2025.1
ARG DEBIAN_ARCHIVE_KEYRING_SHA256=9ea7778e443144ca490668737a8ab22dd3e748bb99e805e22ec055abeb3c7fac

# build_rootfs.sh deliberately performs these operations at image-build time
# so a browser image never has to regenerate sysusers, journal, linker, or font
# caches during its first Asterinas boot.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg='7:6.1.1-3ubuntu5' \
        flex \
        fontconfig='2.15.0-1.1ubuntu2' \
        bison \
        python3-dev \
        python3-setuptools \
        libssl-dev \
        systemd='255.4-1ubuntu8.17' \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Ubuntu 24.04 ships a 2023 Debian keyring which cannot validate all Trixie
# signatures. Install Debian's architecture-independent Trixie keyring only
# after checking the package against Debian's published SHA-256 identity.
RUN keyring=/var/tmp/debian-archive-keyring.deb \
    && curl --fail --location --silent --show-error \
        "https://deb.debian.org/debian/pool/main/d/debian-archive-keyring/debian-archive-keyring_${DEBIAN_ARCHIVE_KEYRING_VERSION}_all.deb" \
        --output "$keyring" \
    && printf '%s  %s\n' "$DEBIAN_ARCHIVE_KEYRING_SHA256" "$keyring" \
        | sha256sum --check --strict - \
    && dpkg --install "$keyring" \
    && rm -f -- "$keyring"

WORKDIR /workspace
