// SPDX-License-Identifier: MPL-2.0

// A passive init for the USB hotplug gate.
//
// The xHCI driver reports controller bring-up and port changes in the kernel
// log; this probe only mounts devtmpfs, announces readiness and keeps the
// guest alive while the host hot-adds and removes a USB stick.

#define _GNU_SOURCE

#include <errno.h>
#include <stdio.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);

    if (getpid() == 1) {
        mkdir("/dev", 0755);
        if (mount("devtmpfs", "/dev", "devtmpfs", 0, NULL) != 0 && errno != EBUSY) {
            printf("USBPROBE_FAIL mount-devtmpfs errno=%d\n", errno);
            return 1;
        }
    }

    printf("USBPROBE_READY\n");
    sleep(3600);
    return 0;
}
