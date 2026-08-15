// SPDX-License-Identifier: MPL-2.0
//
// ALSA smoke test: mount proc/sys (best-effort), then fork + exec the Alpine
// prebuilt `aplay` against `/dev/snd/pcmC0D0p` via the ALSA `hw:0,0` device.
// Prints `__ALSA_EXIT=<code>__` and a final `__ALSA_DONE__` +
// `__ALSA_PASS__`/`__ALSA_FAIL__` marker. The host verifies the PCM actually
// left the guest by decoding QEMU's `wav` backend output.

#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <sys/mount.h>
#include <sys/wait.h>

int main(void) {
    // devtmpfs is mounted by the kernel; proc/sys are best-effort (aplay does
    // not need them, but musl/libasound may probe).
    if (mount("proc", "/proc", "proc", 0, NULL) != 0)
        fprintf(stderr, "mount /proc: %s\n", strerror(errno));
    if (mount("sysfs", "/sys", "sysfs", 0, NULL) != 0)
        fprintf(stderr, "mount /sys: %s\n", strerror(errno));

    pid_t pid = fork();
    if (pid < 0) {
        printf("__ALSA_FORK_FAIL__\n");
        printf("__ALSA_DONE__ __ALSA_FAIL__\n");
        return 1;
    }

    if (pid == 0) {
        char *argv[] = {"aplay", "-D", "hw:0,0", "/sine.wav", NULL};
        execv("/usr/bin/aplay", argv);
        printf("__ALSA_EXEC_FAIL__ errno=%d\n", errno);
        _exit(127);
    }

    int status = 0;
    waitpid(pid, &status, 0);
    int code = WIFEXITED(status) ? WEXITSTATUS(status) : -1;
    printf("__ALSA_EXIT=%d__\n", code);
    printf("__ALSA_DONE__ %s\n", code == 0 ? "__ALSA_PASS__" : "__ALSA_FAIL__");
    return code == 0 ? 0 : 1;
}
