#!/bin/busybox sh
# DRM-M19 virgl EGL verification init (Debian riscv64 rootfs on initramfs).
/bin/busybox echo M19_INIT_START
/bin/busybox mount -t devtmpfs devtmpfs /dev 2>/dev/null
/bin/busybox mount -t proc proc /proc 2>/dev/null
# Linux reference boots (boot_linux_ref.py) need the virtio-gpu module chain.
# On Asterinas these insmods fail silently.
if [ -d /root/ko ]; then
    /bin/busybox mkdir -p /sys
    /bin/busybox mount -t sysfs sysfs /sys 2>/dev/null
    for m in virtio_mmio drm drm_kms_helper drm_shmem_helper virtio_dma_buf virtio-gpu; do
        /bin/busybox insmod /root/ko/$m.ko 2>/dev/null
    done
fi
/bin/busybox echo M19_DEV_NODES
/bin/busybox ls -l /dev/dri 2>&1
export EGL_LOG_LEVEL=debug
/bin/busybox echo M20_PRIME_BEGIN
/root/primetest
/bin/busybox echo "M20_PRIME_RC=$?"
/bin/busybox echo M20_SYNCOBJ_BEGIN
/root/syncobjtest
/bin/busybox echo "M20_SYNCOBJ_RC=$?"
/bin/busybox echo M19_VIRGL_RAW_BEGIN
/root/virgltest
/bin/busybox echo "M19_VIRGL_RAW_RC=$?"
/bin/busybox echo M19_EGLRUN_BEGIN
LD_PRELOAD=/root/ioctltrace.so /root/eglrender2 2>&1
/bin/busybox echo "M19_EGLRUN_RC=$?"
/bin/busybox echo M19_KMSCUBE_BEGIN
LD_PRELOAD=/root/ioctltrace.so /bin/busybox timeout 60 /usr/bin/kmscube -D /dev/dri/card0 2>&1
/bin/busybox echo "M19_KMSCUBE_RC=$?"
/bin/busybox echo M19_PPM_BASE64_BEGIN
/bin/busybox base64 /m19_frame.ppm 2>/dev/null
/bin/busybox echo M19_PPM_BASE64_END
/bin/busybox echo M19_VERIFY_DONE
exec /bin/busybox sh
