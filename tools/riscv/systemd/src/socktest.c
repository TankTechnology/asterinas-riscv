// SPDX-License-Identifier: MPL-2.0
// Socket-activated service (Type=simple). systemd listens on /run/socktest.sock
// (socktest.socket, ListenStream, Accept=no) and hands the already-listening
// fd to this process as fd 3, with LISTEN_FDS=1 / LISTEN_PID=<pid>. We accept
// one connection and reply, proving the fd was passed through.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>

#define SD_LISTEN_FDS_START 3

int main(void) {
    const char *fds = getenv("LISTEN_FDS");
    const char *pid = getenv("LISTEN_PID");
    fprintf(stderr, "socktest: LISTEN_PID=%s LISTEN_FDS=%s\n",
            pid ? pid : "(unset)", fds ? fds : "(unset)");

    int n = fds ? atoi(fds) : 0;
    if (n < 1) {
        fprintf(stderr, "socktest: no inherited socket, aborting\n");
        return 1;
    }

    int c = accept(SD_LISTEN_FDS_START, NULL, NULL);
    if (c < 0) {
        perror("accept");
        return 1;
    }
    const char *msg = "hello-from-socket-activated-service\n";
    (void)write(c, msg, strlen(msg));
    close(c);
    return 0;
}
