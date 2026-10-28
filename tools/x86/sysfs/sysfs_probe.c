// SPDX-License-Identifier: MPL-2.0
//
// Minimal `/init` that proves the sysfs device model is in place: it mounts
// sysfs and prints the parts of `/sys` that the device model owns.
//
// Build and boot it the same way as `tools/x86/drm/flip_vblank_probe.c`:
//
//     gcc -static -O1 -o init sysfs_probe.c
//     mkdir -p root/sys && cp init root/init
//     (cd root && find . | cpio -o -H newc | gzip -1) > /tmp/sysprobe.cpio.gz
//     qemu-system-x86_64 -machine q35,kernel-irqchip=split -cpu Icelake-Server \
//         -smp 2 -m 4G -nographic -no-reboot \
//         -kernel target/osdk/aster-kernel/aster-kernel-osdk-bin.qemu_elf \
//         -initrd /tmp/sysprobe.cpio.gz \
//         -append "console=ttyS0 earlycon init=/init" \
//         -device virtio-gpu-pci -device virtio-keyboard-pci
//
// Every line starts with `PROBE_`, so `grep -a PROBE_` on the serial log is
// the whole result. The probe parks instead of exiting: the kernel has no
// reboot path here, and a panicking init would drown the output.
#define _GNU_SOURCE
#include <dirent.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

static void show(const char *p) {
    struct stat st;
    printf("PROBE_STAT %s %s\n", p, stat(p, &st) == 0 ? "ok" : "MISSING");
}

static void link_(const char *p) {
    char buf[256];
    ssize_t n = readlink(p, buf, sizeof(buf) - 1);
    if (n < 0) {
        printf("PROBE_LINK %s MISSING\n", p);
        return;
    }
    buf[n] = 0;
    printf("PROBE_LINK %s -> %s\n", p, buf);
}

static void read_(const char *p) {
    char b[256];
    int fd = open(p, O_RDONLY);
    if (fd < 0) {
        printf("PROBE_READ %s MISSING\n", p);
        return;
    }
    ssize_t n = read(fd, b, sizeof(b) - 1);
    close(fd);
    if (n < 0) n = 0;
    b[n] = 0;
    printf("PROBE_READ %s: %s", p, b);
    if (n > 0 && b[n - 1] != '\n') printf("\n");
}

static void list_(const char *p) {
    DIR *d = opendir(p);
    if (!d) {
        printf("PROBE_LIST %s MISSING\n", p);
        return;
    }
    printf("PROBE_LIST %s:", p);
    struct dirent *e;
    while ((e = readdir(d)) != NULL) printf(" %s", e->d_name);
    printf("\n");
    closedir(d);
}

int main(void) {
    mkdir("/sys", 0755);
    if (mount("sysfs", "/sys", "sysfs", 0, "") != 0) {
        printf("PROBE_MOUNT_FAILED\n");
    }
    list_("/sys");
    list_("/sys/dev");
    list_("/sys/dev/char");
    list_("/sys/class");
    list_("/sys/bus");
    list_("/sys/devices");
    show("/sys/dev/char/226:0");
    link_("/sys/dev/char/226:0/device/subsystem");
    read_("/sys/dev/char/226:0/device/uevent");
    read_("/sys/dev/char/226:0/uevent");
    list_("/sys/dev/char/226:0/device/drm");
    show("/sys/class/tty/tty0");
    read_("/sys/class/tty/tty0/active");
    show("/sys/class/graphics/fb0");
    link_("/sys/class/graphics/fb0/device/subsystem");
    printf("PROBE_DONE\n");
    fflush(stdout);
    for (;;) pause();
    return 0;
}
