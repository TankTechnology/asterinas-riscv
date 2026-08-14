// SPDX-License-Identifier: MPL-2.0
// Type=simple test service: stay in the foreground (the service is "running"
// as long as this process is alive) and drop a startup marker so the smoke
// test can prove ExecStart ran.
#include <fcntl.h>
#include <stdio.h>
#include <unistd.h>

int main(void) {
    int fd = open("/run/simpletest.started", O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd >= 0) {
        dprintf(fd, "simpletest started pid=%d\n", getpid());
        close(fd);
    }
    for (;;) {
        printf("simpletest alive pid=%d\n", getpid());
        fflush(stdout);
        sleep(1);
    }
}
