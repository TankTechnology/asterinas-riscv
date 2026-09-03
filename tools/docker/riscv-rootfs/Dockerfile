# SPDX-License-Identifier: MPL-2.0

ARG BASE_IMAGE=asterinas/asterinas:0.18.0-20260702
FROM ${BASE_IMAGE}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG DEBIAN_FRONTEND=noninteractive
ARG DEBIAN_ARCHIVE_KEYRING_VERSION=2025.1
ARG DEBIAN_ARCHIVE_KEYRING_SHA256=9ea7778e443144ca490668737a8ab22dd3e748bb99e805e22ec055abeb3c7fac

# Keep the general Asterinas image small and stable. These dependencies are
# specific to building a Debian riscv64 rootfs from an x86_64 host.
RUN apt-get update && \
    apt-get install --yes --no-install-recommends \
        ca-certificates \
        cpio \
        curl \
        debootstrap \
        device-tree-compiler \
        e2fsprogs \
        file \
        gcc-riscv64-linux-gnu \
        gpgv \
        jq \
        libc6-dev-riscv64-cross \
        linux-libc-dev-riscv64-cross \
        proot \
        qemu-system-misc \
        qemu-user-static \
        util-linux && \
    keyring_deb="$(mktemp)" && \
    curl --fail --location --proto '=https' --tlsv1.2 \
        --output "$keyring_deb" \
        "https://deb.debian.org/debian/pool/main/d/debian-archive-keyring/debian-archive-keyring_${DEBIAN_ARCHIVE_KEYRING_VERSION}_all.deb" && \
    printf '%s  %s\n' "$DEBIAN_ARCHIVE_KEYRING_SHA256" "$keyring_deb" | \
        sha256sum --check --strict - && \
    dpkg --install "$keyring_deb" && \
    test "$(dpkg-query --show --showformat='${Version}' debian-archive-keyring)" = \
        "$DEBIAN_ARCHIVE_KEYRING_VERSION" && \
    rm -f "$keyring_deb" && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN install --directory --mode=0755 \
        /var/cache/asterinas/debian \
        /var/cache/asterinas/debootstrap

COPY tools/docker/riscv-rootfs/entrypoint.sh /usr/local/bin/asterinas-riscv-rootfs-entrypoint
RUN chmod 0755 /usr/local/bin/asterinas-riscv-rootfs-entrypoint

ENTRYPOINT ["/usr/local/bin/asterinas-riscv-rootfs-entrypoint"]
CMD ["/bin/bash"]
