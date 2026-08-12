/* SPDX-License-Identifier: MPL-2.0
 *
 * Minimal Wayland wire-format codec.
 *
 * Wayland messages on an AF_UNIX socket are a stream of 8-byte-aligned
 * frames: object_id (u32), size (u16), opcode (u16), followed by arguments.
 * File descriptors ride alongside the message via SCM_RIGHTS; the argument
 * slot for an fd holds a placeholder u32. This header provides the small
 * encode/decode helpers the demo compositor and client need.
 */
#ifndef WAYLAND_WIRE_H
#define WAYLAND_WIRE_H

#include <stddef.h>
#include <stdint.h>

#define WL_HEADER_SIZE 8u

/* Fixed upper bound for a single message (plenty for this demo). */
#define WL_MSG_MAX 4096u

typedef struct {
    unsigned char data[WL_MSG_MAX];
    size_t len; /* bytes written */
} WlWriter;

void wl_writer_init(WlWriter *w);
size_t wl_writer_len(const WlWriter *w);
const unsigned char *wl_writer_data(const WlWriter *w);

void wl_put_u32(WlWriter *w, uint32_t v);
void wl_put_int(WlWriter *w, int32_t v);
void wl_put_str(WlWriter *w, const char *s);
void wl_put_fd_placeholder(WlWriter *w); /* 4-byte slot; fd sent via SCM_RIGHTS */

/* Rewrites the message header (object id, size, opcode) at the front. */
void wl_put_header(WlWriter *w, uint32_t object_id, uint16_t opcode);

typedef struct {
    const unsigned char *data;
    size_t len;
    size_t pos;
} WlReader;

void wl_reader_init(WlReader *r, const unsigned char *data, size_t len);
uint32_t wl_get_u32(WlReader *r);
int32_t wl_get_int(WlReader *r);
const char *wl_get_str(WlReader *r); /* points into the message buffer */
int wl_reader_done(const WlReader *r);

/* Socket transport: send/receive a message, optionally passing an fd via
 * SCM_RIGHTS (send_fd >= 0). Returns 0 on success, -1 on error. */
int wl_send_msg(int fd, const unsigned char *data, size_t len, int send_fd);
int wl_recv_msg(int fd, WlReader *r, int *rcv_fd);

#endif /* WAYLAND_WIRE_H */
