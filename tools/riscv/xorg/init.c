// SPDX-License-Identifier: MPL-2.0
//
// Xorg launcher for the Asterinas RISC-V framebuffer chain.
//
// Runs as /init: puts the VT console into graphics mode, then forks a child
// that execs Xorg (fbdev driver + evdev input). The parent waits forever so
// pid 1 stays alive.

#define _GNU_SOURCE
#include <fcntl.h>
#include <linux/kd.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
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

static void vt_graphics_mode(void) {
    int fd = open("/dev/tty0", O_RDWR);
    if (fd < 0)
        fd = open("/dev/console", O_RDWR);
    if (fd >= 0) {
        ioctl(fd, KDSETMODE, KD_GRAPHICS);
        close(fd);
    }
}

int main(void) {
    int marker_fd = open("/dev/ttyS0", O_WRONLY);
    if (marker_fd >= 0) {
        const char *marker = ">>> Hello from RISC-V userspace on Asterinas! <<<\n";
        write(marker_fd, marker, strlen(marker));
        close(marker_fd);
    }

    vt_graphics_mode();
    tty_log("xorg: launching Xorg");

    char *xorg_argv[] = {
        "/usr/bin/Xorg",
        "-config", "/etc/xorg.conf",
        "-modulepath", "/usr/lib/xorg/modules",
        "-novtswitch",
        "-logfile", "/dev/ttyS0",
        NULL,
    };

    pid_t pid = fork();
    if (pid == 0) {
        /* Detach from the init session so Xorg's own setsid() succeeds. */
        setsid();
        execv(xorg_argv[0], xorg_argv);
        tty_log("xorg: exec Xorg failed");
        _exit(1);
    }

    for (;;) {
        int status;
        waitpid(pid, &status, 0);
        tty_log("xorg: Xorg exited, restarting");
        pid = fork();
        if (pid == 0) {
            setsid();
            execv(xorg_argv[0], xorg_argv);
            _exit(1);
        }
    }
    return 0;
}
