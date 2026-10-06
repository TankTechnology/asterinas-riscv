#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -eu

if [ "$(cat /tmp/asterinas_requested_fs_type 2>/dev/null || true)" != ext4 ]; then
    echo 'ASTERINAS_DEBIAN_APT_ERROR ext4_root_not_mounted'
    exit 1
fi

# Asterinas assigns Virtio-Net 10.0.2.15/24 with the 10.0.2.2 gateway during
# device initialization. Reuse that kernel configuration; only the resolver
# file is part of the Debian userspace setup performed by this gate.
rm -f /ext2/etc/resolv.conf
printf 'nameserver 10.0.2.3\n' > /ext2/etc/resolv.conf

mount --bind /dev /ext2/dev
mount --bind /proc /ext2/proc
mount --bind /sys /ext2/sys
mount --bind /tmp /ext2/tmp

chroot /ext2 /bin/sh -c '
    set -eu
    test -r /proc/self/status
    work=/var/tmp/asterinas-debian-smoke
    rm -rf "$work"
    mkdir -p "$work"
    printf "process-filesystem-syscall\n" > "$work/source"
    cp "$work/source" "$work/copy"
    mv "$work/copy" "$work/renamed"
    test "$(cat "$work/renamed")" = process-filesystem-syscall
    sleep 0
    rm -rf "$work"
'

chroot /ext2 /usr/bin/env DEBIAN_FRONTEND=noninteractive \
    /usr/bin/apt-get update
chroot /ext2 /usr/bin/env DEBIAN_FRONTEND=noninteractive \
    /usr/bin/apt-get install -y --no-install-recommends hello

test -x /ext2/usr/bin/hello
test "$(chroot /ext2 /usr/bin/hello)" = 'Hello, world!'
chroot /ext2 /usr/bin/dpkg-query -W -f='${Status}' hello \
    | grep -Fxq 'install ok installed'
# apt and dpkg locks are intentionally ephemeral. Remove their directory
# entries before the final sync so the persistence check does not mistake an
# ordinary closed lock for leaked filesystem metadata.
rm -f /ext2/var/cache/apt/archives/lock \
    /ext2/var/lib/apt/lists/lock \
    /ext2/var/lib/dpkg/lock \
    /ext2/var/lib/dpkg/lock-frontend
sync
echo 'ASTERINAS_DEBIAN_APT_OK boot=1 shell=1 process=1 filesystem=1 syscall=1 apt_update=1 package=hello dpkg=1 network=1 ext4=1'
