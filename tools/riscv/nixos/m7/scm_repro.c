// SPDX-License-Identifier: MPL-2.0
//
// M7 minimal repro: UNIX-domain SCM_RIGHTS file-descriptor passing + SO_PEERCRED.
//
// This is the exact kernel surface nix-daemon depends on for its multi-user
// protocol: clients connect over an AF_UNIX socket, the daemon reads the
// client's credentials with SO_PEERCRED, and the two sides pass file
// descriptors (log files, store dirs) back and forth with SCM_RIGHTS.
//
// The repro runs three checks and prints a fixed marker for each so the QEMU
// driver can assert the result unambiguously:
//   1. socketpair() creates a connected AF_UNIX pair.
//   2. SO_PEERCRED on the child end reports the parent's (pid, uid, gid).
//   3. the child opens a file, writes a known payload, and sends the fd over
//      the socket with sendmsg(SCM_RIGHTS); the parent recvmsg()s it and reads
//      the payload back through the received fd to prove the fd is live.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static void say(const char *s) {
    (void)write(1, s, strlen(s));
}

static void fail(const char *why) {
    printf("__M7_%s_FAIL__ errno=%d (%s)\n", why, errno, strerror(errno));
    exit(1);
}

int main(void) {
    say(">>> M7 init: SCM_RIGHTS + SO_PEERCRED repro <<<\n");

    int sv[2];
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, sv) < 0)
        fail("SOCKETPAIR");
    printf("__M7_SOCKETPAIR_OK__ sv={%d,%d}\n", sv[0], sv[1]);

    /* 2. SO_PEERCRED: the peer of each end is the process itself before fork. */
    struct ucred cred;
    socklen_t cred_len = sizeof(cred);
    if (getsockopt(sv[0], SOL_SOCKET, SO_PEERCRED, &cred, &cred_len) < 0)
        fail("PEERCRED");
    printf("__M7_PEERCRED_OK__ pid=%d uid=%d gid=%d\n",
           (int)cred.pid, (int)cred.uid, (int)cred.gid);

    pid_t pid = fork();
    if (pid < 0)
        fail("FORK");

    if (pid == 0) {
        /* Child: sender. Open a file, write a known payload, pass its fd. */
        close(sv[0]);

        int fd = open("/tmp/scm_payload", O_RDWR | O_CREAT | O_TRUNC, 0644);
        if (fd < 0)
            fail("CHILD_OPEN");
        const char payload[] = "scm-rights-ok";
        if (write(fd, payload, sizeof(payload) - 1) != (ssize_t)(sizeof(payload) - 1))
            fail("CHILD_WRITE");
        if (lseek(fd, 0, SEEK_SET) < 0)
            fail("CHILD_SEEK");

        char byte = 'x';
        struct iovec iov = { .iov_base = &byte, .iov_len = 1 };
        char cbuf[CMSG_SPACE(sizeof(int))];
        memset(cbuf, 0, sizeof(cbuf));
        struct msghdr msg;
        memset(&msg, 0, sizeof(msg));
        msg.msg_iov = &iov;
        msg.msg_iovlen = 1;
        msg.msg_control = cbuf;
        msg.msg_controllen = sizeof(cbuf);

        struct cmsghdr *cmsg = CMSG_FIRSTHDR(&msg);
        cmsg->cmsg_level = SOL_SOCKET;
        cmsg->cmsg_type = SCM_RIGHTS;
        cmsg->cmsg_len = CMSG_LEN(sizeof(int));
        memcpy(CMSG_DATA(cmsg), &fd, sizeof(int));

        if (sendmsg(sv[1], &msg, 0) < 0)
            fail("CHILD_SENDMSG");
        printf("__M7_CHILD_SEND_OK__\n");
        _exit(0);
    }

    /* Parent: receiver. recvmsg the fd, then read the payload through it. */
    close(sv[1]);

    char byte = 0;
    struct iovec iov = { .iov_base = &byte, .iov_len = 1 };
    char cbuf[CMSG_SPACE(sizeof(int))];
    memset(cbuf, 0, sizeof(cbuf));
    struct msghdr msg;
    memset(&msg, 0, sizeof(msg));
    msg.msg_iov = &iov;
    msg.msg_iovlen = 1;
    msg.msg_control = cbuf;
    msg.msg_controllen = sizeof(cbuf);

    ssize_t n = recvmsg(sv[0], &msg, 0);
    if (n < 0)
        fail("RECVMSG");
    if (byte != 'x')
        fail("RECV_DATA");

    int rfd = -1;
    for (struct cmsghdr *cmsg = CMSG_FIRSTHDR(&msg); cmsg; cmsg = CMSG_NXTHDR(&msg, cmsg)) {
        if (cmsg->cmsg_level == SOL_SOCKET && cmsg->cmsg_type == SCM_RIGHTS) {
            memcpy(&rfd, CMSG_DATA(cmsg), sizeof(int));
        }
    }
    if (rfd < 0)
        fail("NO_SCM_RIGHTS");

    char buf[32] = {0};
    ssize_t rd = read(rfd, buf, sizeof(buf) - 1);
    if (rd < 0)
        fail("READ_FD");

    int status = 0;
    waitpid(pid, &status, 0);

    if (strcmp(buf, "scm-rights-ok") != 0)
        fail("FD_CONTENT");
    printf("__M7_SCM_RIGHTS_OK__ read_back=[%s]\n", buf);

    say(">>> M7 repro done <<<\n");
    return 0;
}
