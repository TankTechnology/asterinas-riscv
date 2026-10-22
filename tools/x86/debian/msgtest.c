// SPDX-License-Identifier: MPL-2.0
//
// Unit checks for the two socket/wait behaviours fcitx5 depends on:
// send(..., MSG_NOSIGNAL) on a broken peer must return EPIPE without
// killing the process, and waitid(P_PID, ..., WEXITED) must report the
// child's exit status.

#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

int main(void) {
    int sv[2];
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, sv) != 0) {
        printf("MSGTEST_FAIL socketpair errno=%d\n", errno);
        return 1;
    }
    close(sv[1]);
    errno = 0;
    ssize_t sent = send(sv[0], "x", 1, MSG_NOSIGNAL);
    printf("MSGTEST_SEND ret=%zd errno=%d\n", sent, errno);
    int send_ok = (sent < 0 && errno == EPIPE);
    close(sv[0]);

    pid_t pid = fork();
    if (pid == 0) {
        _exit(7);
    }
    siginfo_t info;
    memset(&info, 0, sizeof(info));
    errno = 0;
    int rc = waitid(P_PID, pid, &info, WEXITED);
    printf("MSGTEST_WAIT rc=%d errno=%d code=%d status=%d\n",
           rc, errno, info.si_code, info.si_status);
    int wait_ok = (rc == 0 && info.si_code == CLD_EXITED && info.si_status == 7);

    if (send_ok && wait_ok) {
        printf("MSGTEST_PASS\n");
        return 0;
    }
    printf("MSGTEST_FAIL send_ok=%d wait_ok=%d\n", send_ok, wait_ok);
    return 1;
}
