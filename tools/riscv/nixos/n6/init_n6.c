// SPDX-License-Identifier: MPL-2.0
//
// NIXOS-N6 initramfs /init: just runs the namespace probe.

#define _GNU_SOURCE
#include <fcntl.h>
#include <string.h>
#include <unistd.h>

static void say(const char *s) {
    (void)write(1, s, strlen(s));
}

int main(void) {
    int fd = open("/dev/console", O_RDWR);
    if (fd < 0)
        fd = open("/dev/ttyS0", O_RDWR);
    if (fd >= 0) {
        (void)dup2(fd, 0);
        (void)dup2(fd, 1);
        (void)dup2(fd, 2);
        if (fd > 2)
            (void)close(fd);
    }

    say(">>> N6 init: running nsprobe <<<\n");
    char *const argv[] = { "/bin/nsprobe", NULL };
    (void)execv("/bin/nsprobe", argv);

    say("init: exec nsprobe failed\n");
    for (;;)
        (void)pause();
    return 0;
}
