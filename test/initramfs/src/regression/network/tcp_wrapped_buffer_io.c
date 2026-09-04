// SPDX-License-Identifier: MPL-2.0

#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

#include "../common/test.h"

#define LISTEN_PORT 9999
#define LISTEN_BACKLOG 4
#define RECEIVE_BUFFER_FILL_DRAIN_ROUNDS 16
#define TCP_SETTLE_USEC 100000

static ssize_t recv_exact(int fd, void *buf, size_t len)
{
	size_t received = 0;

	while (received < len) {
		ssize_t result =
			recv(fd, (char *)buf + received, len - received, 0);

		if (result <= 0)
			return result < 0 ? result : (ssize_t)received;
		received += result;
	}

	return (ssize_t)received;
}

/*
 * Tokio's PollEvented clears readiness after a positive short read on
 * epoll/kqueue targets because such a read is treated as evidence that the
 * socket buffer has been drained. If a TCP stream read stops at the tail
 * fragment of a wrapped receive buffer while bytes remain after the wrap, a
 * reactor can miss the remaining data and leave the task waiting.
 *
 * The same applies to a positive short write, which is not yet covered by
 * this test.
 *
 * See:
 * https://github.com/tokio-rs/tokio/blob/905c146aeda741ea2202f942a7c3a606dda13da5/tokio/src/io/poll_evented.rs#L182-L213
 * https://github.com/tokio-rs/tokio/blob/905c146aeda741ea2202f942a7c3a606dda13da5/tokio/src/io/poll_evented.rs#L240-L267
 *
 * This test focuses on Asterinas's simple, ring-buffered TCP behavior. On
 * Linux, there are many other complexities related to SKB management and
 * receive window advertisement, so this test is likely to fail.
 *
 * See:
 * https://github.com/asterinas/asterinas/pull/3146#issuecomment-4351171818
 */
FN_TEST(tcp_read_wrap_receive_buffer_tail)
{
	struct sockaddr_in listen_addr = {
		.sin_family = AF_INET,
		.sin_addr.s_addr = htonl(INADDR_LOOPBACK),
		.sin_port = htons(LISTEN_PORT),
	};
	int listen_fd = TEST_SUCC(socket(AF_INET, SOCK_STREAM, 0));
	int client_fd = TEST_SUCC(socket(AF_INET, SOCK_STREAM, 0));
	int server_fd;
	int tcp_recv_buf_len = 0;
	socklen_t optlen = sizeof(tcp_recv_buf_len);
	size_t one_third_buf_len;
	size_t two_thirds_buf_len;
	char *buf;

	TEST_SUCC(bind(listen_fd, (struct sockaddr *)&listen_addr,
		       sizeof(listen_addr)));
	TEST_SUCC(listen(listen_fd, LISTEN_BACKLOG));
	TEST_SUCC(connect(client_fd, (struct sockaddr *)&listen_addr,
			  sizeof(listen_addr)));
	server_fd = TEST_SUCC(accept(listen_fd, NULL, NULL));

	TEST_RES(getsockopt(client_fd, SOL_SOCKET, SO_RCVBUF, &tcp_recv_buf_len,
			    &optlen),
		 _ret == 0 && tcp_recv_buf_len >= 3);
	one_third_buf_len = tcp_recv_buf_len / 3;
	two_thirds_buf_len = one_third_buf_len * 2;

	buf = TEST_RES(malloc(two_thirds_buf_len), _ret != NULL);
	memset(buf, 'a', two_thirds_buf_len);

	TEST_RES(send(server_fd, buf, two_thirds_buf_len, 0),
		 _ret == (ssize_t)two_thirds_buf_len);
	usleep(TCP_SETTLE_USEC);
	TEST_RES(recv(client_fd, buf, one_third_buf_len, 0),
		 _ret == (ssize_t)one_third_buf_len);
	usleep(TCP_SETTLE_USEC);

	for (int i = 0; i < RECEIVE_BUFFER_FILL_DRAIN_ROUNDS; i++) {
		TEST_RES(send(server_fd, buf, two_thirds_buf_len, 0),
			 _ret == (ssize_t)two_thirds_buf_len);
		usleep(TCP_SETTLE_USEC);
		TEST_RES(recv(client_fd, buf, two_thirds_buf_len, 0),
			 _ret == (ssize_t)two_thirds_buf_len);
		usleep(TCP_SETTLE_USEC);
	}

	TEST_RES(recv(client_fd, buf, two_thirds_buf_len, 0),
		 _ret == (ssize_t)one_third_buf_len);

	free(buf);
	TEST_SUCC(close(client_fd));
	TEST_SUCC(close(server_fd));
	TEST_SUCC(close(listen_fd));
}
END_TEST()

/*
 * TCP copies run while the network stack protects its ring buffers.  A valid
 * anonymous mapping may still be demand-paged, so both send and receive must
 * resolve that fault without panicking or losing stream data.
 *
 * See: https://github.com/TankTechnology/asterinas-riscv/issues/132
 */
FN_TEST(tcp_io_resolves_demand_paged_user_buffers)
{
	static const char message[] = "demand-paged TCP receive";
	struct sockaddr_in listen_addr = {
		.sin_family = AF_INET,
		.sin_addr.s_addr = htonl(INADDR_LOOPBACK),
		.sin_port = 0,
	};
	socklen_t listen_addr_len = sizeof(listen_addr);
	int listen_fd = CHECK(socket(AF_INET, SOCK_STREAM, 0));
	int client_fd = CHECK(socket(AF_INET, SOCK_STREAM, 0));
	int server_fd;
	size_t page_size = CHECK(sysconf(_SC_PAGESIZE));
	char receive_buf[32];
	char *demand_paged_buf;

	CHECK(bind(listen_fd, (struct sockaddr *)&listen_addr,
		   sizeof(listen_addr)));
	CHECK(getsockname(listen_fd, (struct sockaddr *)&listen_addr,
			  &listen_addr_len));
	CHECK(listen(listen_fd, LISTEN_BACKLOG));
	CHECK(connect(client_fd, (struct sockaddr *)&listen_addr,
		      sizeof(listen_addr)));
	server_fd = CHECK(accept(listen_fd, NULL, NULL));

	demand_paged_buf = CHECK_WITH(mmap(NULL, page_size, PROT_WRITE,
					   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0),
				      _ret != MAP_FAILED);
	CHECK_WITH(send(server_fd, message, sizeof(message), 0),
		   _ret == (ssize_t)sizeof(message));
	TEST_RES(recv_exact(client_fd, demand_paged_buf, sizeof(message)),
		 _ret == (ssize_t)sizeof(message));
	CHECK(mprotect(demand_paged_buf, page_size, PROT_READ));
	TEST_RES(memcmp(demand_paged_buf, message, sizeof(message)), _ret == 0);
	CHECK(munmap(demand_paged_buf, page_size));

	demand_paged_buf =
		CHECK_WITH(mmap(NULL, page_size, PROT_READ | PROT_WRITE,
				MAP_PRIVATE | MAP_ANONYMOUS, -1, 0),
			   _ret != MAP_FAILED);
	CHECK_WITH(send(client_fd, demand_paged_buf, sizeof(receive_buf), 0),
		   _ret == (ssize_t)sizeof(receive_buf));
	TEST_RES(recv_exact(server_fd, receive_buf, sizeof(receive_buf)),
		 _ret == (ssize_t)sizeof(receive_buf));
	TEST_RES(memcmp(receive_buf, demand_paged_buf, sizeof(receive_buf)),
		 _ret == 0);
	CHECK(munmap(demand_paged_buf, page_size));

	CHECK(close(client_fd));
	CHECK(close(server_fd));
	CHECK(close(listen_fd));
}
END_TEST()
