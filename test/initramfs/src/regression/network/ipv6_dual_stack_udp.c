// SPDX-License-Identifier: MPL-2.0

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#define CHECK(condition, stage)                                                \
	do {                                                                   \
		if (!(condition)) {                                            \
			fprintf(stderr,                                        \
				"ASTERINAS_IPV6_DUAL_STACK_UDP_FAIL stage=%s " \
				"line=%d errno=%d\n",                          \
				stage, __LINE__, errno);                       \
			goto fail;                                             \
		}                                                              \
	} while (0)

static void timeout_handler(int signal_number)
{
	static const char timeout_message[] =
		"ASTERINAS_IPV6_DUAL_STACK_UDP_FAIL stage=timeout\n";
	ssize_t written;

	(void)signal_number;
	written = write(STDERR_FILENO, timeout_message,
			sizeof(timeout_message) - 1);
	(void)written;
	_exit(124);
}

int main(void)
{
	static const char request[] = "dual-stack-udp-request";
	static const char response[] = "dual-stack-udp-response";
	const struct in6_addr expected_peer = {
		.s6_addr = { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff, 127, 0,
			     0, 1 },
	};
	const struct timeval short_timeout = { .tv_sec = 0, .tv_usec = 100000 };
	struct sockaddr_in6 wildcard = {
		.sin6_family = AF_INET6,
		.sin6_addr = IN6ADDR_ANY_INIT,
	};
	struct sockaddr_in destination = {
		.sin_family = AF_INET,
		.sin_addr.s_addr = htonl(INADDR_LOOPBACK),
	};
	struct sockaddr_in ipv4_wildcard = {
		.sin_family = AF_INET,
		.sin_addr.s_addr = htonl(INADDR_ANY),
	};
	struct sockaddr_in strict_sender_address = {
		.sin_family = AF_INET,
		.sin_addr.s_addr = htonl(INADDR_LOOPBACK),
	};
	struct sockaddr_in sender_peer = { 0 };
	struct sockaddr_in6 peer = { 0 };
	socklen_t wildcard_len = sizeof(wildcard);
	socklen_t sender_peer_len = sizeof(sender_peer);
	socklen_t peer_len = sizeof(peer);
	char buffer[sizeof(response) > sizeof(request) ? sizeof(response) :
							 sizeof(request)];
	int listener = -1;
	int sender = -1;
	int conflict = -1;
	int strict_listener = -1;
	int ipv4_owner = -1;
	int v6only = 0;

	signal(SIGALRM, timeout_handler);
	alarm(10);

	listener = socket(AF_INET6, SOCK_DGRAM, 0);
	CHECK(listener >= 0, "dual-socket");
	CHECK(setsockopt(listener, IPPROTO_IPV6, IPV6_V6ONLY, &v6only,
			 sizeof(v6only)) == 0,
	      "dual-v6only-off");
	CHECK(bind(listener, (struct sockaddr *)&wildcard, sizeof(wildcard)) ==
		      0,
	      "dual-bind");
	CHECK(getsockname(listener, (struct sockaddr *)&wildcard,
			  &wildcard_len) == 0 &&
		      wildcard.sin6_port != 0,
	      "dual-name");
	fprintf(stderr, "ASTERINAS_IPV6_DUAL_STACK_UDP_STAGE dual-port=%u\n",
		ntohs(wildcard.sin6_port));

	/* A dual wildcard owns the corresponding IPv4 wildcard port. */
	conflict = socket(AF_INET, SOCK_DGRAM, 0);
	CHECK(conflict >= 0, "conflict-socket");
	ipv4_wildcard.sin_port = wildcard.sin6_port;
	errno = 0;
	CHECK(bind(conflict, (struct sockaddr *)&ipv4_wildcard,
		   sizeof(ipv4_wildcard)) == -1 &&
		      errno == EADDRINUSE,
	      "dual-port-reservation");
	close(conflict);
	conflict = -1;
	fprintf(stderr, "ASTERINAS_IPV6_DUAL_STACK_UDP_STAGE port-reserved\n");

	sender = socket(AF_INET, SOCK_DGRAM, 0);
	CHECK(sender >= 0, "sender-socket");
	destination.sin_port = wildcard.sin6_port;
	CHECK(sendto(sender, request, sizeof(request), 0,
		     (struct sockaddr *)&destination,
		     sizeof(destination)) == (ssize_t)sizeof(request),
	      "mapped-send");
	fprintf(stderr, "ASTERINAS_IPV6_DUAL_STACK_UDP_STAGE mapped-sent\n");
	CHECK(recvfrom(listener, buffer, sizeof(buffer), 0,
		       (struct sockaddr *)&peer,
		       &peer_len) == (ssize_t)sizeof(request),
	      "mapped-receive");
	fprintf(stderr,
		"ASTERINAS_IPV6_DUAL_STACK_UDP_STAGE mapped-received\n");
	CHECK(peer.sin6_family == AF_INET6 &&
		      memcmp(&peer.sin6_addr, &expected_peer,
			     sizeof(expected_peer)) == 0 &&
		      memcmp(buffer, request, sizeof(request)) == 0,
	      "mapped-peer");
	CHECK(sendto(listener, response, sizeof(response), 0,
		     (struct sockaddr *)&peer,
		     peer_len) == (ssize_t)sizeof(response),
	      "mapped-reply-send");
	fprintf(stderr, "ASTERINAS_IPV6_DUAL_STACK_UDP_STAGE reply-sent\n");
	CHECK(recvfrom(sender, buffer, sizeof(buffer), 0,
		       (struct sockaddr *)&sender_peer,
		       &sender_peer_len) == (ssize_t)sizeof(response) &&
		      sender_peer.sin_family == AF_INET &&
		      memcmp(buffer, response, sizeof(response)) == 0,
	      "mapped-reply-receive");
	fprintf(stderr, "ASTERINAS_IPV6_DUAL_STACK_UDP_STAGE reply-received\n");
	close(sender);
	sender = -1;

	/* IPV6_V6ONLY leaves IPv4 ownership and delivery independent. */
	strict_listener = socket(AF_INET6, SOCK_DGRAM, 0);
	CHECK(strict_listener >= 0, "strict-socket");
	v6only = 1;
	CHECK(setsockopt(strict_listener, IPPROTO_IPV6, IPV6_V6ONLY, &v6only,
			 sizeof(v6only)) == 0,
	      "strict-v6only-on");
	wildcard.sin6_port = 0;
	wildcard_len = sizeof(wildcard);
	CHECK(bind(strict_listener, (struct sockaddr *)&wildcard,
		   sizeof(wildcard)) == 0,
	      "strict-bind");
	CHECK(getsockname(strict_listener, (struct sockaddr *)&wildcard,
			  &wildcard_len) == 0 &&
		      wildcard.sin6_port != 0,
	      "strict-name");
	CHECK(setsockopt(strict_listener, SOL_SOCKET, SO_RCVTIMEO,
			 &short_timeout, sizeof(short_timeout)) == 0,
	      "strict-timeout");

	ipv4_owner = socket(AF_INET, SOCK_DGRAM, 0);
	CHECK(ipv4_owner >= 0, "ipv4-owner-socket");
	ipv4_wildcard.sin_port = wildcard.sin6_port;
	CHECK(bind(ipv4_owner, (struct sockaddr *)&ipv4_wildcard,
		   sizeof(ipv4_wildcard)) == 0,
	      "v6only-port-isolation");
	fprintf(stderr,
		"ASTERINAS_IPV6_DUAL_STACK_UDP_STAGE v6only-isolated\n");
	sender = socket(AF_INET, SOCK_DGRAM, 0);
	CHECK(sender >= 0, "strict-sender-socket");
	strict_sender_address.sin_port =
		htons(ntohs(wildcard.sin6_port) == UINT16_MAX ?
			      UINT16_MAX - 1 :
			      ntohs(wildcard.sin6_port) + 1);
	CHECK(bind(sender, (struct sockaddr *)&strict_sender_address,
		   sizeof(strict_sender_address)) == 0,
	      "strict-sender-bind");
	destination.sin_port = wildcard.sin6_port;
	CHECK(sendto(sender, request, sizeof(request), 0,
		     (struct sockaddr *)&destination,
		     sizeof(destination)) == (ssize_t)sizeof(request),
	      "strict-ipv4-send");
	fprintf(stderr,
		"ASTERINAS_IPV6_DUAL_STACK_UDP_STAGE strict-ipv4-sent\n");
	CHECK(recvfrom(ipv4_owner, buffer, sizeof(buffer), 0, NULL, NULL) ==
		      (ssize_t)sizeof(request),
	      "strict-ipv4-receive");
	fprintf(stderr,
		"ASTERINAS_IPV6_DUAL_STACK_UDP_STAGE ipv4-owner-received\n");
	errno = 0;
	CHECK(recvfrom(strict_listener, buffer, sizeof(buffer), 0, NULL,
		       NULL) == -1 &&
		      (errno == EAGAIN || errno == EWOULDBLOCK),
	      "strict-no-ipv4-delivery");

	alarm(0);
	close(ipv4_owner);
	close(strict_listener);
	close(sender);
	close(listener);
	puts("ASTERINAS_IPV6_DUAL_STACK_UDP_OK peer=::ffff:127.0.0.1 "
	     "dual-port-reserved=1 v6only-isolated=1");
	return EXIT_SUCCESS;

fail:
	if (ipv4_owner >= 0)
		close(ipv4_owner);
	if (strict_listener >= 0)
		close(strict_listener);
	if (conflict >= 0)
		close(conflict);
	if (sender >= 0)
		close(sender);
	if (listener >= 0)
		close(listener);
	return EXIT_FAILURE;
}
