// SPDX-License-Identifier: MPL-2.0
//
// DRM-M3 launcher: starts Xorg with the modesetting driver on /dev/dri/card0,
// then spawns the draw client that fills the root window with a gradient.
// Mirrors the fbdev launcher's detach trick so Xorg's own setsid() succeeds.

#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

static void tty_log(const char *s) {
    int fd = open("/dev/ttyS0", O_WRONLY);
    if (fd >= 0) {
        write(fd, s, strlen(s));
        write(fd, "\n", 1);
        close(fd);
    }
}

static pid_t launch_xorg(void) {
    char *argv[] = {
        "/usr/bin/Xorg",
        "-config", "/etc/xorg.conf",
        "-modulepath", "/usr/lib/xorg/modules",
        "-xkbdir", "/usr/share/X11/xkb",
        "-logfile", "/dev/ttyS0",
        NULL,
    };

    pid_t pid = fork();
    if (pid == 0) {
        setsid();
        execv(argv[0], argv);
        tty_log("xorg: exec Xorg failed");
        _exit(1);
    }
    return pid;
}

int main(void) {
    tty_log(">>> DRM-M3: launching Xorg (modesetting) <<<");

    pid_t xorg = launch_xorg();

    /* The draw client retries its own XOpenDisplay, but also respawn if it ever
     * exits (e.g. the display never came up and it gave up). */
    pid_t client = fork();
    if (client == 0) {
        setenv("DISPLAY", ":0", 1);
        for (;;) {
            pid_t p = fork();
            if (p == 0) {
                execl("/usr/bin/xfill", "xfill", NULL);
                tty_log("xorg: exec xfill failed");
                _exit(1);
            }
            int status;
            waitpid(p, &status, 0);
            sleep(1);
        }
    }

    for (;;) {
        int status;
        waitpid(xorg, &status, 0);
        tty_log("xorg: Xorg exited, restarting");
        xorg = launch_xorg();
    }
    return 0;
}
