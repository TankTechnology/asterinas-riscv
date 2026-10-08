// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#define ARRAY_SIZE(array) (sizeof(array) / sizeof((array)[0]))
#define BATCHES 1024U
#define BATCH_SIZE 4U
#define TOTAL_MESSAGES (BATCHES * BATCH_SIZE)
#define QUEUE_CAPACITY 16U
#define MAX_PAYLOAD 521U

static const size_t payload_sizes[BATCH_SIZE] = { 92, 40, 121, 521 };

struct message {
	uint32_t sequence;
	uint32_t length;
	uint8_t payload[MAX_PAYLOAD];
};

struct handoff {
	pthread_mutex_t mutex;
	struct message queue[QUEUE_CAPACITY];
	size_t head;
	size_t count;
	int wake_pending;
	int failed;
	int server_fd;
	int main_wake[2];
	int service_control[2];
	int service_inert[2];
};

static void timeout_handler(int signal_number)
{
	static const char message[] =
		"tcp_event_handoff: timed out (possible lost event handoff)\n";
	ssize_t write_result;

	(void)signal_number;
	write_result = write(STDERR_FILENO, message, sizeof(message) - 1);
	(void)write_result;
	_exit(EXIT_FAILURE);
}

static void fail(const char *operation)
{
	perror(operation);
	exit(EXIT_FAILURE);
}

static int write_all(int fd, const void *buffer, size_t length)
{
	const uint8_t *bytes = buffer;
	size_t offset = 0;

	while (offset < length) {
		ssize_t written = write(fd, bytes + offset, length - offset);
		if (written < 0 && errno == EINTR)
			continue;
		if (written <= 0)
			return -1;
		offset += (size_t)written;
	}
	return 0;
}

static int read_all(int fd, void *buffer, size_t length)
{
	uint8_t *bytes = buffer;
	size_t offset = 0;

	while (offset < length) {
		ssize_t received = read(fd, bytes + offset, length - offset);
		if (received < 0 && errno == EINTR)
			continue;
		if (received <= 0)
			return -1;
		offset += (size_t)received;
	}
	return 0;
}

static uint8_t payload_byte(uint32_t sequence, size_t offset)
{
	return (uint8_t)(sequence * 31U + offset * 17U + 0x5aU);
}

static int enqueue_message(struct handoff *handoff, uint32_t sequence,
			   const uint8_t *payload, uint32_t length)
{
	int must_wake = 0;

	pthread_mutex_lock(&handoff->mutex);
	if (handoff->count == QUEUE_CAPACITY) {
		handoff->failed = 1;
		pthread_mutex_unlock(&handoff->mutex);
		errno = ENOSPC;
		return -1;
	}
	struct message *message =
		&handoff->queue[(handoff->head + handoff->count) %
				QUEUE_CAPACITY];
	message->sequence = sequence;
	message->length = length;
	memcpy(message->payload, payload, length);
	handoff->count++;
	if (!handoff->wake_pending) {
		handoff->wake_pending = 1;
		must_wake = 1;
	}
	pthread_mutex_unlock(&handoff->mutex);

	return must_wake ? write_all(handoff->main_wake[1], "W", 1) : 0;
}

static void *socket_service(void *argument)
{
	struct handoff *handoff = argument;
	uint8_t payload[MAX_PAYLOAD];

	for (uint32_t sequence = 1; sequence <= TOTAL_MESSAGES; ++sequence) {
		struct pollfd fds[] = {
			{ .fd = handoff->server_fd, .events = POLLIN },
			{ .fd = handoff->service_control[0], .events = POLLIN },
			{ .fd = handoff->service_inert[0], .events = POLLIN },
		};
		int result;
		do {
			result = ppoll(fds, ARRAY_SIZE(fds), NULL, NULL);
		} while (result < 0 && errno == EINTR);
		if (result < 1 || !(fds[0].revents & POLLIN) ||
		    fds[1].revents || fds[2].revents)
			goto failure;

		uint32_t network_length;
		if (read_all(handoff->server_fd, &network_length,
			     sizeof(network_length)) < 0)
			goto failure;
		uint32_t length = ntohl(network_length);
		if (length != payload_sizes[(sequence - 1) % BATCH_SIZE] ||
		    read_all(handoff->server_fd, payload, length) < 0 ||
		    enqueue_message(handoff, sequence, payload, length) < 0)
			goto failure;
	}
	return NULL;

failure:
	pthread_mutex_lock(&handoff->mutex);
	handoff->failed = 1;
	pthread_mutex_unlock(&handoff->mutex);
	(void)write_all(handoff->main_wake[1], "F", 1);
	return NULL;
}

static void *client(void *argument)
{
	int fd = *(int *)argument;
	uint8_t payload[MAX_PAYLOAD];
	uint8_t acknowledgements[BATCH_SIZE];

	for (uint32_t batch = 0; batch < BATCHES; ++batch) {
		for (uint32_t index = 0; index < BATCH_SIZE; ++index) {
			uint32_t sequence = batch * BATCH_SIZE + index + 1;
			uint32_t length = (uint32_t)payload_sizes[index];
			uint32_t network_length = htonl(length);
			for (size_t offset = 0; offset < length; ++offset)
				payload[offset] =
					payload_byte(sequence, offset);
			if (write_all(fd, &network_length,
				      sizeof(network_length)) < 0 ||
			    write_all(fd, payload, length) < 0)
				return (void *)1;
		}
		if (read_all(fd, acknowledgements, sizeof(acknowledgements)) <
		    0)
			return (void *)1;
		for (uint32_t index = 0; index < BATCH_SIZE; ++index) {
			uint32_t sequence = batch * BATCH_SIZE + index + 1;
			if (acknowledgements[index] != (uint8_t)sequence)
				return (void *)1;
		}
	}
	return NULL;
}

static int drain_queue(struct handoff *handoff, uint32_t *next_sequence)
{
	struct message messages[QUEUE_CAPACITY];
	size_t count;

	pthread_mutex_lock(&handoff->mutex);
	count = handoff->count;
	for (size_t index = 0; index < count; ++index)
		messages[index] =
			handoff->queue[(handoff->head + index) % QUEUE_CAPACITY];
	handoff->head = (handoff->head + count) % QUEUE_CAPACITY;
	handoff->count = 0;
	handoff->wake_pending = 0;
	int failed = handoff->failed;
	pthread_mutex_unlock(&handoff->mutex);
	if (failed)
		return -1;

	for (size_t index = 0; index < count; ++index) {
		struct message *message = &messages[index];
		if (message->sequence != *next_sequence ||
		    message->length !=
			    payload_sizes[(*next_sequence - 1) % BATCH_SIZE])
			return -1;
		for (size_t offset = 0; offset < message->length; ++offset) {
			if (message->payload[offset] !=
			    payload_byte(*next_sequence, offset))
				return -1;
		}
		uint8_t acknowledgement = (uint8_t)*next_sequence;
		if (write_all(handoff->server_fd, &acknowledgement, 1) < 0)
			return -1;
		(*next_sequence)++;
	}
	return 0;
}

int main(void)
{
	struct sigaction action = { .sa_handler = timeout_handler };
	struct sockaddr_in address = {
		.sin_family = AF_INET,
		.sin_addr.s_addr = htonl(INADDR_LOOPBACK),
		.sin_port = 0,
	};
	socklen_t address_length = sizeof(address);
	struct handoff handoff = {
		.mutex = PTHREAD_MUTEX_INITIALIZER,
		.server_fd = -1,
	};
	pthread_t service_thread;
	pthread_t client_thread;
	void *client_result = NULL;
	uint32_t next_sequence = 1;

	sigemptyset(&action.sa_mask);
	if (sigaction(SIGALRM, &action, NULL) < 0)
		fail("sigaction");
	alarm(120);

	int listener = socket(AF_INET, SOCK_STREAM, 0);
	if (listener < 0 ||
	    bind(listener, (struct sockaddr *)&address, sizeof(address)) < 0 ||
	    getsockname(listener, (struct sockaddr *)&address,
			&address_length) < 0 ||
	    listen(listener, 1) < 0)
		fail("listen setup");
	int client_fd = socket(AF_INET, SOCK_STREAM, 0);
	if (client_fd < 0 || connect(client_fd, (struct sockaddr *)&address,
				     sizeof(address)) < 0)
		fail("connect");
	handoff.server_fd = accept(listener, NULL, NULL);
	if (handoff.server_fd < 0 || pipe(handoff.main_wake) < 0 ||
	    pipe(handoff.service_control) < 0 ||
	    pipe(handoff.service_inert) < 0)
		fail("accept or pipe");
	int enabled = 1;
	if (setsockopt(client_fd, IPPROTO_TCP, TCP_NODELAY, &enabled,
		       sizeof(enabled)) < 0 ||
	    setsockopt(handoff.server_fd, IPPROTO_TCP, TCP_NODELAY, &enabled,
		       sizeof(enabled)) < 0)
		fail("setsockopt(TCP_NODELAY)");
	close(listener);

	if (pthread_create(&service_thread, NULL, socket_service, &handoff) !=
		    0 ||
	    pthread_create(&client_thread, NULL, client, &client_fd) != 0)
		fail("pthread_create");

	while (next_sequence <= TOTAL_MESSAGES) {
		struct pollfd fds[] = {
			{ .fd = handoff.main_wake[0], .events = POLLIN },
			{ .fd = handoff.service_control[0], .events = POLLIN },
			{ .fd = handoff.service_inert[0], .events = POLLIN },
		};
		int result;
		do {
			result = ppoll(fds, ARRAY_SIZE(fds), NULL, NULL);
		} while (result < 0 && errno == EINTR);
		char wake;
		if (result < 1 || !(fds[0].revents & POLLIN) ||
		    read(handoff.main_wake[0], &wake, 1) != 1 || wake != 'W' ||
		    drain_queue(&handoff, &next_sequence) < 0) {
			handoff.failed = 1;
			break;
		}
	}

	if (pthread_join(service_thread, NULL) != 0 ||
	    pthread_join(client_thread, &client_result) != 0 || client_result ||
	    handoff.failed || next_sequence != TOTAL_MESSAGES + 1)
		return EXIT_FAILURE;
	alarm(0);
	printf("tcp_event_handoff: passed %u socket-to-queue-to-pipe handoffs\n",
	       TOTAL_MESSAGES);
	return EXIT_SUCCESS;
}
