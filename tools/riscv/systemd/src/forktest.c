// SPDX-License-Identifier: MPL-2.0
// Type=forking test service: the ExecStart process forks, the parent exits 0
// (so systemd sees "started"), and the child daemonizes and writes its pid.
#include <fcntl.h>
#include <stdio.h>
#include <unistd.h>

int main(void) {
    pid_t pid = fork();
    if (pid < 0) {
        perror("fork");
        return 1;
    }
    if (pid > 0) {
        _exit(0); // parent: signal "daemonized" to systemd
    }

    // child: the actual daemon
    int fd = open("/run/forktest.pid", O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd >= 0) {
        dprintf(fd, "%d\n", getpid());
        close(fd);
    }
    for (;;) {
        sleep(1);
    }
}
