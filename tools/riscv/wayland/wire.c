// SPDX-License-Identifier: MPL-2.0

#include "wire.h"

#include <string.h>
#include <sys/socket.h>

static size_t align4(size_t n) {
    return (n + 3u) & ~3u;
}

void wl_writer_init(WlWriter *w) {
    w->len = WL_HEADER_SIZE; /* reserve room for the message header */
}

size_t wl_writer_len(const WlWriter *w) {
    return w->len;
}

const unsigned char *wl_writer_data(const WlWriter *w) {
    return w->data;
}

static void put_bytes(WlWriter *w, const void *src, size_t n) {
    memcpy(w->data + w->len, src, n);
    w->len += n;
}

void wl_put_u32(WlWriter *w, uint32_t v) {
    put_bytes(w, &v, sizeof(v));
}

void wl_put_int(WlWriter *w, int32_t v) {
    put_bytes(w, &v, sizeof(v));
}

void wl_put_str(WlWriter *w, const char *s) {
    size_t slen = strlen(s);
    uint32_t len = (uint32_t)(slen + 1); /* length includes the NUL terminator */
    wl_put_u32(w, len);
    put_bytes(w, s, slen + 1);
    size_t pad = align4(slen + 1) - (slen + 1);
    static const unsigned char zeros[4] = {0, 0, 0, 0};
    put_bytes(w, zeros, pad);
}

void wl_put_fd_placeholder(WlWriter *w) {
    wl_put_u32(w, 0); /* real fd goes in the control message */
}

void wl_put_header(WlWriter *w, uint32_t object_id, uint16_t opcode) {
    uint32_t head = object_id;
    /* Wire format: size in the high 16 bits, opcode in the low 16 bits. */
    uint32_t sz_op = (((uint32_t)w->len & 0xffffu) << 16u) | (uint32_t)opcode;
    memcpy(w->data, &head, sizeof(head));
    memcpy(w->data + 4, &sz_op, sizeof(sz_op));
}

void wl_reader_init(WlReader *r, const unsigned char *data, size_t len) {
    r->data = data;
    r->len = len;
    r->pos = 0;
}

uint32_t wl_get_u32(WlReader *r) {
    uint32_t v;
    memcpy(&v, r->data + r->pos, sizeof(v));
    r->pos += sizeof(v);
    return v;
}

int32_t wl_get_int(WlReader *r) {
    int32_t v;
    memcpy(&v, r->data + r->pos, sizeof(v));
    r->pos += sizeof(v);
    return v;
}

const char *wl_get_str(WlReader *r) {
    uint32_t len = wl_get_u32(r);
    const char *s = (const char *)(r->data + r->pos);
    r->pos += align4(len);
    return s;
}

int wl_reader_done(const WlReader *r) {
    return r->pos >= r->len;
}

int wl_send_msg(int fd, const unsigned char *data, size_t len, int send_fd) {
    struct iovec iov = {(void *)data, len};
    char cbuf[CMSG_SPACE(sizeof(int))];
    struct msghdr msg;
    memset(&msg, 0, sizeof(msg));
    msg.msg_iov = &iov;
    msg.msg_iovlen = 1;
    if (send_fd >= 0) {
        memset(cbuf, 0, sizeof(cbuf));
        msg.msg_control = cbuf;
        msg.msg_controllen = sizeof(cbuf);
        struct cmsghdr *c = CMSG_FIRSTHDR(&msg);
        c->cmsg_level = SOL_SOCKET;
        c->cmsg_type = SCM_RIGHTS;
        c->cmsg_len = CMSG_LEN(sizeof(int));
        *((int *)CMSG_DATA(c)) = send_fd;
    }
    return sendmsg(fd, &msg, 0) < 0 ? -1 : 0;
}

int wl_recv_msg(int fd, WlReader *r, int *rcv_fd) {
    unsigned char buf[WL_MSG_MAX];
    struct iovec iov = {buf, sizeof(buf)};
    char cbuf[CMSG_SPACE(sizeof(int))];
    struct msghdr msg;
    memset(&msg, 0, sizeof(msg));
    msg.msg_iov = &iov;
    msg.msg_iovlen = 1;
    msg.msg_control = cbuf;
    msg.msg_controllen = sizeof(cbuf);

    ssize_t n = recvmsg(fd, &msg, 0);
    if (n < 0) {
        return -1;
    }

    *rcv_fd = -1;
    for (struct cmsghdr *c = CMSG_FIRSTHDR(&msg); c; c = CMSG_NXTHDR(&msg, c)) {
        if (c->cmsg_level == SOL_SOCKET && c->cmsg_type == SCM_RIGHTS) {
            *rcv_fd = ((int *)CMSG_DATA(c))[0];
        }
    }

    wl_reader_init(r, buf, (size_t)n);
    return 0;
}
