// SPDX-License-Identifier: MPL-2.0

// A passive init for the audio gate.
//
// The HDA driver runs its playback self-test during controller bring-up; this
// probe only mounts devtmpfs, announces readiness and keeps the kernel alive
// while the host captures the guest's audio output.

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
            printf("AUDIOPROBE_FAIL mount-devtmpfs errno=%d\n", errno);
            return 1;
        }
    }

    printf("AUDIOPROBE_READY\n");
    sleep(3600);
    return 0;
}
