#!/bin/busybox sh
# Mount the large Mesa test root from a block device so the kernel only needs
# to unpack a tiny bootstrap initramfs under TCG.

/bin/busybox mkdir -p /newroot
if ! /bin/busybox mount -t ext2 /dev/vdb /newroot; then
    echo "MINI_DISK_FAIL mount /dev/vdb"
    exec /bin/busybox sh
fi

/bin/busybox mkdir -p /newroot/dev /newroot/proc /newroot/sys
/bin/busybox mount -o bind /dev /newroot/dev
/bin/busybox mount -t proc proc /newroot/proc
/bin/busybox mount -t sysfs sysfs /newroot/sys
echo MINI_DISK_ROOT_READY
exec /bin/busybox chroot /newroot /init
