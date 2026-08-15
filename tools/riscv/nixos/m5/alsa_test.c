// SPDX-License-Identifier: MPL-2.0
//
// DRM-M5 ALSA smoke test (systemd oneshot). Forks + execs the Alpine musl
// `aplay` against `/dev/snd/pcmC0D0p` via the ALSA `hw:0,0` device, then prints
// `__ALSA_EXIT=<code>__` and a final `__ALSA_DONE__` +
// `__ALSA_PASS__`/`__ALSA_FAIL__` marker. The host verifies the PCM actually
// left the guest by decoding QEMU's `wav` backend output.

#include <errno.h>
#include <stdio.h>
#include <unistd.h>
#include <sys/wait.h>

int main(void) {
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
