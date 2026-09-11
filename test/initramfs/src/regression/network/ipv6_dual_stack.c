// SPDX-License-Identifier: MPL-2.0

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#define CHECK(condition, stage)                                                \
	do {                                                                   \
		if (!(condition)) {                                            \
			fprintf(stderr,                                        \
				"ASTERINAS_IPV6_DUAL_STACK_TCP_FAIL stage=%s " \
				"line=%d errno=%d\n",                          \
				stage, __LINE__, errno);                       \
			goto fail;                                             \
		}                                                              \
	} while (0)

static void timeout_handler(int signal_number)
{
	static const char timeout_message[] =
		"ASTERINAS_IPV6_DUAL_STACK_TCP_FAIL stage=timeout\n";
	ssize_t written;

	(void)signal_number;
	written = write(STDERR_FILENO, timeout_message,
			sizeof(timeout_message) - 1);
	(void)written;
	_exit(124);
}

int main(void)
{
	static const char request[] = "dual-stack-tcp-request";
	static const char response[] = "dual-stack-tcp-response";
	const struct in6_addr expected_peer = {
		.s6_addr = { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff, 127, 0,
			     0, 1 },
	};
	struct sockaddr_in6 wildcard = {
		.sin6_family = AF_INET6,
		.sin6_addr = IN6ADDR_ANY_INIT,
	};
	struct sockaddr_in ipv4_wildcard = {
		.sin_family = AF_INET,
		.sin_addr.s_addr = htonl(INADDR_ANY),
	};
	struct sockaddr_in client_address = {
		.sin_family = AF_INET,
		.sin_addr.s_addr = htonl(INADDR_LOOPBACK),
	};
	struct sockaddr_in client_local = { 0 };
	struct sockaddr_in6 peer = { 0 };
	socklen_t wildcard_len = sizeof(wildcard);
	socklen_t client_local_len = sizeof(client_local);
	socklen_t peer_len = sizeof(peer);
	char buffer[sizeof(response) > sizeof(request) ? sizeof(response) :
							 sizeof(request)];
	int listener = -1;
	int client = -1;
	int conflict = -1;
	int accepted = -1;
	int strict_listener = -1;
	int ipv4_owner = -1;
	int strict_client = -1;
	int ipv4_accepted = -1;
	int flags;
	int v6only = 0;

	signal(SIGALRM, timeout_handler);
	alarm(10);

	listener = socket(AF_INET6, SOCK_STREAM, 0);
	CHECK(listener >= 0, "dual-socket");
	CHECK(setsockopt(listener, IPPROTO_IPV6, IPV6_V6ONLY, &v6only,
			 sizeof(v6only)) == 0,
	      "dual-v6only-off");
	CHECK(bind(listener, (struct sockaddr *)&wildcard, sizeof(wildcard)) ==
		      0,
	      "dual-bind");
	CHECK(listen(listener, 1) == 0, "dual-listen");
	CHECK(getsockname(listener, (struct sockaddr *)&wildcard,
			  &wildcard_len) == 0 &&
		      wildcard.sin6_port != 0,
	      "dual-name");
	fprintf(stderr, "ASTERINAS_IPV6_DUAL_STACK_TCP_STAGE dual-port=%u\n",
		ntohs(wildcard.sin6_port));

	conflict = socket(AF_INET, SOCK_STREAM, 0);
	CHECK(conflict >= 0, "conflict-socket");
	ipv4_wildcard.sin_port = wildcard.sin6_port;
	errno = 0;
	CHECK(bind(conflict, (struct sockaddr *)&ipv4_wildcard,
		   sizeof(ipv4_wildcard)) == -1 &&
		      errno == EADDRINUSE,
	      "dual-port-reservation");
	close(conflict);
	conflict = -1;
	fprintf(stderr, "ASTERINAS_IPV6_DUAL_STACK_TCP_STAGE port-reserved\n");

	client = socket(AF_INET, SOCK_STREAM, 0);
	CHECK(client >= 0, "client-socket");
	client_address.sin_port = wildcard.sin6_port;
	CHECK(connect(client, (struct sockaddr *)&client_address,
		      sizeof(client_address)) == 0,
	      "mapped-connect");
	CHECK(getsockname(client, (struct sockaddr *)&client_local,
			  &client_local_len) == 0,
	      "client-name");
	fprintf(stderr,
		"ASTERINAS_IPV6_DUAL_STACK_TCP_STAGE client-port=%u "
		"listener-port=%u\n",
		ntohs(client_local.sin_port), ntohs(wildcard.sin6_port));
	CHECK(client_local.sin_port != wildcard.sin6_port,
	      "client-port-reserved");
	accepted = accept(listener, (struct sockaddr *)&peer, &peer_len);
	CHECK(accepted >= 0, "mapped-accept");
	CHECK(peer.sin6_family == AF_INET6 &&
		      memcmp(&peer.sin6_addr, &expected_peer,
			     sizeof(expected_peer)) == 0,
	      "mapped-peer");
	CHECK(send(client, request, sizeof(request), 0) ==
		      (ssize_t)sizeof(request),
	      "mapped-request-send");
	CHECK(recv(accepted, buffer, sizeof(request), 0) ==
			      (ssize_t)sizeof(request) &&
		      memcmp(buffer, request, sizeof(request)) == 0,
	      "mapped-request-receive");
	CHECK(send(accepted, response, sizeof(response), 0) ==
		      (ssize_t)sizeof(response),
	      "mapped-response-send");
	CHECK(recv(client, buffer, sizeof(response), 0) ==
			      (ssize_t)sizeof(response) &&
		      memcmp(buffer, response, sizeof(response)) == 0,
	      "mapped-response-receive");
	fprintf(stderr,
		"ASTERINAS_IPV6_DUAL_STACK_TCP_STAGE mapped-exchange-complete\n");
	close(accepted);
	accepted = -1;
	close(client);
	client = -1;
	close(listener);
	listener = -1;

	strict_listener = socket(AF_INET6, SOCK_STREAM, 0);
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
	CHECK(listen(strict_listener, 1) == 0, "strict-listen");
	CHECK(getsockname(strict_listener, (struct sockaddr *)&wildcard,
			  &wildcard_len) == 0 &&
		      wildcard.sin6_port != 0,
	      "strict-name");

	ipv4_owner = socket(AF_INET, SOCK_STREAM, 0);
	CHECK(ipv4_owner >= 0, "ipv4-owner-socket");
	ipv4_wildcard.sin_port = wildcard.sin6_port;
	CHECK(bind(ipv4_owner, (struct sockaddr *)&ipv4_wildcard,
		   sizeof(ipv4_wildcard)) == 0,
	      "v6only-port-isolation");
	CHECK(listen(ipv4_owner, 1) == 0, "ipv4-owner-listen");
	fprintf(stderr,
		"ASTERINAS_IPV6_DUAL_STACK_TCP_STAGE v6only-isolated\n");

	strict_client = socket(AF_INET, SOCK_STREAM, 0);
	CHECK(strict_client >= 0, "strict-client-socket");
	client_address.sin_port = wildcard.sin6_port;
	CHECK(connect(strict_client, (struct sockaddr *)&client_address,
		      sizeof(client_address)) == 0,
	      "strict-ipv4-connect");
	ipv4_accepted = accept(ipv4_owner, NULL, NULL);
	CHECK(ipv4_accepted >= 0, "ipv4-owner-accept");
	flags = fcntl(strict_listener, F_GETFL);
	CHECK(flags >= 0, "strict-getfl");
	CHECK(fcntl(strict_listener, F_SETFL, flags | O_NONBLOCK) == 0,
	      "strict-nonblock");
	errno = 0;
	CHECK(accept(strict_listener, NULL, NULL) == -1 &&
		      (errno == EAGAIN || errno == EWOULDBLOCK),
	      "strict-no-ipv4-accept");

	alarm(0);
	close(ipv4_accepted);
	close(strict_client);
	close(ipv4_owner);
	close(strict_listener);
	puts("ASTERINAS_IPV6_DUAL_STACK_TCP_OK peer=::ffff:127.0.0.1 "
	     "client-port-reserved=1 v6only-isolated=1");
	return EXIT_SUCCESS;

fail:
	if (ipv4_accepted >= 0)
		close(ipv4_accepted);
	if (strict_client >= 0)
		close(strict_client);
	if (ipv4_owner >= 0)
		close(ipv4_owner);
	if (strict_listener >= 0)
		close(strict_listener);
	if (accepted >= 0)
		close(accepted);
	if (conflict >= 0)
		close(conflict);
	if (client >= 0)
		close(client);
	if (listener >= 0)
		close(listener);
	return EXIT_FAILURE;
}
