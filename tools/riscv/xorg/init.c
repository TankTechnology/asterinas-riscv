// SPDX-License-Identifier: MPL-2.0
//
// Xorg launcher + desktop session for the Asterinas RISC-V framebuffer chain.
//
// Runs as /init: forks a child that execs Xorg (fbdev driver + evdev input),
// then spawns the desktop session clients (the X11 demo client, and the window
// manager once one is bundled). The parent waits forever so pid 1 stays alive.
//
// The Xorg child detaches from the init session so Xorg's own VT setup
// (setsid/VT_ACTIVATE/VT_SETMODE) succeeds. We deliberately do NOT set
// KD_GRAPHICS here: a VT left in KD_GRAPHICS + VT_AUTO cannot be switched away
// from (matching Linux change_console's guard), which would hang Xorg's
// VT_ACTIVATE.

#define _GNU_SOURCE
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

/* Fork a child that connects to the X server at :0 and runs `path`. The
 * clients retry their own XOpenDisplay, so they do not need Xorg to be fully
 * up before they are spawned. */
static void spawn_client(char *const argv[]) {
    pid_t pid = fork();
    if (pid == 0) {
        setenv("DISPLAY", ":0", 1);
        setenv("HOME", "/root", 1);
        setenv("FONTCONFIG_FILE", "/etc/fonts/fonts.conf", 1);
        execv(argv[0], argv);
        tty_log("xorg: exec client failed");
        _exit(1);
    }
}

/* matchbox-window-manager exits immediately if the X server isn't up yet
 * (unlike our own xwm/xclient/gtk-hello, which retry internally). Wrap it in
 * a respawn loop. */
static void spawn_wm(void) {
    pid_t pid = fork();
    if (pid == 0) {
        setenv("DISPLAY", ":0", 1);
        setenv("HOME", "/root", 1);
        setenv("FONTCONFIG_FILE", "/etc/fonts/fonts.conf", 1);
        for (;;) {
            pid_t p = fork();
            if (p == 0) {
                char *argv[] = { "/usr/bin/matchbox-window-manager", NULL };
                execv(argv[0], argv);
                tty_log("xorg: exec matchbox-window-manager failed");
                _exit(1);
            }
            int st;
            waitpid(p, &st, 0);
            tty_log("xorg: matchbox-window-manager exited, retrying");
            sleep(1);
        }
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
        /* Detach from the init session so Xorg's own setsid() succeeds. */
        setsid();
        execv(argv[0], argv);
        tty_log("xorg: exec Xorg failed");
        _exit(1);
    }
    return pid;
}

int main(void) {
    int marker_fd = open("/dev/ttyS0", O_WRONLY);
    if (marker_fd >= 0) {
        const char *marker = ">>> Hello from RISC-V userspace on Asterinas! <<<\n";
        write(marker_fd, marker, strlen(marker));
        close(marker_fd);
    }

    tty_log("xorg: launching Xorg");
    pid_t xorg = launch_xorg();

    tty_log("xorg: launching session clients");
    spawn_wm();
    {
        char *gtk_hello_argv[] = { "/usr/bin/gtk-hello", NULL };
        spawn_client(gtk_hello_argv);
    }

    for (;;) {
        int status;
        waitpid(xorg, &status, 0);
        tty_log("xorg: Xorg exited, restarting");
        xorg = launch_xorg();
    }
    return 0;
}
