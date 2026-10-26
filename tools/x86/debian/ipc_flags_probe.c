// SPDX-License-Identifier: MPL-2.0
//
// Minimal reproducer for the Firefox IPC EPERM: exercises SCM_RIGHTS (regular
// fd and socket fd), SCM_CREDENTIALS (own pid, pid=0, foreign pid),
// MSG_DONTWAIT, MSG_CMSG_CLOEXEC, MSG_NOSIGNAL and pipe2(O_CLOEXEC).
//
// Build: gcc -O2 -o ipc_flags_probe ipc_flags_probe.c
// Run as root and as an unprivileged user; each line is self-describing.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

static int failures;

static void report(const char *tag, int rc, int err) {
    printf("ipc_case %-34s rc=%d errno=%s\n", tag, rc, rc ? strerror(err) : "-");
    if (rc) failures++;
}

static void send_fd(int sock, int fd, int flags, const char *tag) {
    char buf[1] = {'x'};
    struct iovec iov = {.iov_base = buf, .iov_len = 1};
    char cbuf[CMSG_SPACE(sizeof(int))];
    memset(cbuf, 0, sizeof cbuf);
    struct msghdr msg = {0};
    msg.msg_iov = &iov;
    msg.msg_iovlen = 1;
    msg.msg_control = cbuf;
    msg.msg_controllen = sizeof cbuf;
    struct cmsghdr *c = CMSG_FIRSTHDR(&msg);
    c->cmsg_level = SOL_SOCKET;
    c->cmsg_type = SCM_RIGHTS;
    c->cmsg_len = CMSG_LEN(sizeof(int));
    memcpy(CMSG_DATA(c), &fd, sizeof fd);
    errno = 0;
    int rc = sendmsg(sock, &msg, flags);
    report(tag, rc < 0 ? rc : 0, errno);
}

static void send_cred(int sock, pid_t pid, const char *tag) {
    char buf[1] = {'y'};
    struct iovec iov = {.iov_base = buf, .iov_len = 1};
    char cbuf[CMSG_SPACE(sizeof(struct ucred))];
    memset(cbuf, 0, sizeof cbuf);
    struct msghdr msg = {0};
    msg.msg_iov = &iov;
    msg.msg_iovlen = 1;
    msg.msg_control = cbuf;
    msg.msg_controllen = sizeof cbuf;
    struct cmsghdr *c = CMSG_FIRSTHDR(&msg);
    c->cmsg_level = SOL_SOCKET;
    c->cmsg_type = SCM_CREDENTIALS;
    c->cmsg_len = CMSG_LEN(sizeof(struct ucred));
    struct ucred cr = {.pid = pid, .uid = getuid(), .gid = getgid()};
    memcpy(CMSG_DATA(c), &cr, sizeof cr);
    errno = 0;
    int rc = sendmsg(sock, &msg, 0);
    report(tag, rc < 0 ? rc : 0, errno);
}

static int recv_fd(int sock, int flags, int *fd_out) {
    char buf[64];
    struct iovec iov = {.iov_base = buf, .iov_len = sizeof buf};
    char cbuf[CMSG_SPACE(sizeof(int))];
    struct msghdr msg = {0};
    msg.msg_iov = &iov;
    msg.msg_iovlen = 1;
    msg.msg_control = cbuf;
    msg.msg_controllen = sizeof cbuf;
    int rc = recvmsg(sock, &msg, flags);
    if (rc < 0) return rc;
    for (struct cmsghdr *c = CMSG_FIRSTHDR(&msg); c; c = CMSG_NXTHDR(&msg, c)) {
        if (c->cmsg_level == SOL_SOCKET && c->cmsg_type == SCM_RIGHTS) {
            memcpy(fd_out, CMSG_DATA(c), sizeof(int));
            return 0;
        }
    }
    *fd_out = -1;
    return 0;
}

int main(void) {
    printf("ipc_env uid=%d euid=%d gid=%d\n", getuid(), geteuid(), getgid());
    int sv[2], rc, err;
    int fds[2];
    int tmpfd = open("/tmp/ipc-probe-file", O_CREAT | O_RDWR, 0600);

    /* 1. SCM_RIGHTS with a regular file fd. */
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, sv) == 0) {
        send_fd(sv[0], tmpfd, 0, "SCM_RIGHTS:regular_fd");
        int got = -1;
        rc = recv_fd(sv[1], 0, &got);
        report("SCM_RIGHTS:regular_fd:recv", rc, errno);
        if (got >= 0) close(got);
        close(sv[0]); close(sv[1]);
    }

    /* 2. SCM_RIGHTS carrying a *socket* fd (Firefox passes channel ends). */
    if (socketpair(AF_UNIX, SOCK_SEQPACKET, 0, fds) == 0) {
        int sp[2];
        if (socketpair(AF_UNIX, SOCK_SEQPACKET, 0, sp) == 0) {
            send_fd(fds[0], sp[0], 0, "SCM_RIGHTS:socket_fd");
            close(sp[0]); close(sp[1]);
        }
        close(fds[0]); close(fds[1]);
    }

    /* 3. Same, with MSG_DONTWAIT. */
    if (socketpair(AF_UNIX, SOCK_SEQPACKET, 0, fds) == 0) {
        int sp[2];
        if (socketpair(AF_UNIX, SOCK_SEQPACKET, 0, sp) == 0) {
            send_fd(fds[0], sp[0], MSG_DONTWAIT, "SCM_RIGHTS:socket_fd_DONTWAIT");
            close(sp[0]); close(sp[1]);
        }
        close(fds[0]); close(fds[1]);
    }

    /* 4. MSG_CMSG_CLOEXEC on recv. */
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, sv) == 0) {
        send_fd(sv[0], tmpfd, 0, "SCM_RIGHTS:for_CLOEXEC");
        int got = -1;
        rc = recv_fd(sv[1], MSG_CMSG_CLOEXEC, &got);
        report("recvmsg:MSG_CMSG_CLOEXEC", rc, errno);
        if (got >= 0) {
            int fl = fcntl(got, F_GETFD);
            report("recvmsg:CLOEXEC_set", (fl & FD_CLOEXEC) ? 0 : -1, errno);
            close(got);
        }
        close(sv[0]); close(sv[1]);
    }

    /* 5. MSG_NOSIGNAL. */
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, sv) == 0) {
        char b = 'z';
        errno = 0; rc = (int)send(sv[0], &b, 1, MSG_NOSIGNAL);
        report("send:MSG_NOSIGNAL", rc < 0 ? rc : 0, errno);
        close(sv[0]); close(sv[1]);
    }

    /* 6. pipe2(O_CLOEXEC) + write/read. */
    {
        int p[2];
        errno = 0; rc = pipe2(p, O_CLOEXEC);
        report("pipe2:O_CLOEXEC", rc, errno);
        if (rc == 0) {
            char b = 'q';
            errno = 0; rc = (int)write(p[1], &b, 1);
            report("pipe2:write", rc < 0 ? rc : 0, errno);
            errno = 0; rc = (int)read(p[0], &b, 1);
            report("pipe2:read", rc < 0 ? rc : 0, errno);
            close(p[0]); close(p[1]);
        }
    }

    /* 7. Non-blocking pipe write when full -> EAGAIN (never EPERM). */
    {
        int p[2];
        if (pipe2(p, O_NONBLOCK | O_CLOEXEC) == 0) {
            static char buf[65536];
            while (write(p[1], buf, sizeof buf) > 0) {}
            errno = 0; rc = (int)write(p[1], buf, 1);
            report("pipe:nonblock_full", rc < 0 ? rc : 0, errno);
            close(p[0]); close(p[1]);
        }
    }

    /* 8. SCM_CREDENTIALS with our own pid / pid 0 / a foreign pid. */
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, sv) == 0) {
        int one = 1;
        setsockopt(sv[1], SOL_SOCKET, SO_PASSCRED, &one, sizeof one);
        send_cred(sv[0], getpid(), "SCM_CREDENTIALS:own_pid");
        send_cred(sv[0], 0, "SCM_CREDENTIALS:pid_zero");
        send_cred(sv[0], getpid() + 1000, "SCM_CREDENTIALS:foreign_pid");
        close(sv[0]); close(sv[1]);
    }

    /* 9. Peer closed -> EPIPE (with MSG_NOSIGNAL), never EPERM. */
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, sv) == 0) {
        close(sv[1]);
        char b = 'w';
        errno = 0; rc = (int)send(sv[0], &b, 1, MSG_NOSIGNAL);
        report("send:peer_closed", rc < 0 ? rc : 0, errno);
        close(sv[0]);
    }

    /* 10. Firefox-style: socketpair(SEQPACKET) + SCM_RIGHTS of a socket end. */
    if (socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, fds) == 0) {
        int sp[2];
        if (socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC | SOCK_NONBLOCK, 0, sp) == 0) {
            send_fd(fds[0], sp[0], MSG_NOSIGNAL, "ff_style:seqpacket_socket_fd");
            close(sp[0]); close(sp[1]);
        }
        close(fds[0]); close(fds[1]);
    }

    printf("ipc_done failures=%d\n", failures);
    return failures ? 1 : 0;
}
