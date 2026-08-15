// DRM-M8 `/init` — devtmpfs auto-create regression test.
//
// The initramfs this is packed into deliberately contains **no `/dev`
// directory**. Before the fix, the kernel panicked in
// `device::init_in_first_process` ("path resolution did not reach the final
// target") because it looked up `/dev` to mount devtmpfs and did not create it
// when absent. After the fix the kernel creates `/dev` itself, mounts devtmpfs,
// and registers `/dev/console` — so this init (PID 1) can prove:
//
//   __M8_DEV__=DIR          /dev exists and is a directory
//   __M8_CONSOLE__=PRESENT  /dev/console was registered
//   __M8_OPEN_CONSOLE__=OK  /dev/console opens R/W
//
// Static glibc, run as PID 1. fd 0/1/2 are already wired to /dev/console by
// fs::init_in_first_process before the first process is spawned.
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static void say(const char *s) { (void)write(1, s, strlen(s)); }

int main(void) {
    say(">>> M8 nodev init: no /dev was in the initramfs <<<\n");

    struct stat st;
    if (stat("/dev", &st) == 0 && S_ISDIR(st.st_mode))
        say("__M8_DEV__=DIR\n");
    else
        say("__M8_DEV__=MISSING\n");

    if (stat("/dev/console", &st) == 0)
        say("__M8_CONSOLE__=PRESENT\n");
    else
        say("__M8_CONSOLE__=MISSING\n");

    int fd = open("/dev/console", O_RDWR);
    say(fd >= 0 ? "__M8_OPEN_CONSOLE__=OK\n" : "__M8_OPEN_CONSOLE__=FAIL\n");
    if (fd >= 0)
        (void)close(fd);

    say(">>> M8 nodev init done <<<\n");
    return 0;
}
