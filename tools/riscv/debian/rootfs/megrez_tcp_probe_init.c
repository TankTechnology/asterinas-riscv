// SPDX-License-Identifier: MPL-2.0

#define _POSIX_C_SOURCE 200809L

#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

#define PROBE_BODY "ASTERINAS_TCP_PROBE_OK\n"
#define RESPONSE_HEADER_LIMIT (64U * 1024U)
#define RESPONSE_READ_BYTES (8U * 1024U)

#ifndef MEGREZ_TCP_STRESS_BYTES
#define MEGREZ_TCP_STRESS_BYTES (16U * 1024U * 1024U)
#endif

struct probe_failure {
    const char *reason;
    int error;
    unsigned int attempts;
};

static void emit_failure(const struct probe_failure *failure)
{
    printf("ASTERINAS_GMAC_TCP_PROBE_FAIL reason=%s errno=%d attempts=%u\n",
           failure->reason, failure->error, failure->attempts);
}

static const char *find_sequence(const char *data, size_t length,
                                 const char *needle, size_t needle_length)
{
    size_t offset;

    if (needle_length == 0 || needle_length > length) {
        return NULL;
    }
    for (offset = 0; offset <= length - needle_length; ++offset) {
        if (memcmp(data + offset, needle, needle_length) == 0) {
            return data + offset;
        }
    }
    return NULL;
}

#ifdef MEGREZ_TCP_PROBE_SELF_TEST
static bool response_is_valid(const char *response, size_t length)
{
    static const char status_10[] = "HTTP/1.0 200 ";
    static const char status_11[] = "HTTP/1.1 200 ";
    static const char separator[] = "\r\n\r\n";
    const char *body;
    const char *end = response + length;

    if (length == 0 || length > RESPONSE_HEADER_LIMIT) {
        return false;
    }
    if ((length < sizeof(status_10) - 1 ||
         memcmp(response, status_10, sizeof(status_10) - 1) != 0) &&
        (length < sizeof(status_11) - 1 ||
         memcmp(response, status_11, sizeof(status_11) - 1) != 0)) {
        return false;
    }
    body = find_sequence(response, length, separator, sizeof(separator) - 1);
    if (body == NULL) {
        return false;
    }
    body += sizeof(separator) - 1;
    return (size_t)(end - body) == sizeof(PROBE_BODY) - 1 &&
           memcmp(body, PROBE_BODY, sizeof(PROBE_BODY) - 1) == 0;
}
#endif

struct response_stream {
    char header[RESPONSE_HEADER_LIMIT];
    size_t header_length;
    size_t body_length;
    bool header_complete;
};

static bool response_header_is_valid(const char *header, size_t length)
{
    static const char status_10[] = "HTTP/1.0 200 ";
    static const char status_11[] = "HTTP/1.1 200 ";
    static const char content_length[] = "\r\nContent-Length: ";
    const char *field;
    const char *cursor;
    const char *end = header + length;
    size_t value = 0;

    if ((length < sizeof(status_10) - 1 ||
         memcmp(header, status_10, sizeof(status_10) - 1) != 0) &&
        (length < sizeof(status_11) - 1 ||
         memcmp(header, status_11, sizeof(status_11) - 1) != 0)) {
        return false;
    }
    field = find_sequence(header, length, content_length,
                          sizeof(content_length) - 1);
    if (field == NULL) {
        return false;
    }
    cursor = field + sizeof(content_length) - 1;
    if (cursor == end || *cursor < '0' || *cursor > '9') {
        return false;
    }
    while (cursor < end && *cursor >= '0' && *cursor <= '9') {
        size_t digit = (size_t)(*cursor - '0');

        if (value > (MEGREZ_TCP_STRESS_BYTES - digit) / 10U) {
            return false;
        }
        value = value * 10U + digit;
        ++cursor;
    }
    return value == MEGREZ_TCP_STRESS_BYTES && end - cursor >= 2 &&
           cursor[0] == '\r' && cursor[1] == '\n';
}

static bool response_stream_consume(struct response_stream *stream,
                                    const char *data, size_t length)
{
    static const char separator[] = "\r\n\r\n";
    size_t cursor = 0;

    while (!stream->header_complete && cursor < length) {
        if (stream->header_length == sizeof(stream->header)) {
            return false;
        }
        stream->header[stream->header_length++] = data[cursor++];
        if (stream->header_length >= sizeof(separator) - 1 &&
            memcmp(stream->header + stream->header_length -
                       (sizeof(separator) - 1),
                   separator, sizeof(separator) - 1) == 0) {
            if (!response_header_is_valid(stream->header,
                                          stream->header_length)) {
                return false;
            }
            stream->header_complete = true;
        }
    }

    while (cursor < length) {
        unsigned char expected;

        if (stream->body_length == MEGREZ_TCP_STRESS_BYTES) {
            return false;
        }
        expected = (unsigned char)(stream->body_length % 251U);
        if ((unsigned char)data[cursor] != expected) {
            return false;
        }
        ++stream->body_length;
        ++cursor;
    }
    return true;
}

static bool response_stream_is_complete(const struct response_stream *stream)
{
    return stream->header_complete &&
           stream->body_length == MEGREZ_TCP_STRESS_BYTES;
}

#ifdef MEGREZ_TCP_PROBE_SELF_TEST

int main(void)
{
    const struct probe_failure failure = {
        .reason = "connect-poll",
        .error = 110,
        .attempts = 3,
    };
    static const char valid[] =
        "HTTP/1.1 200 OK\r\nContent-Length: 23\r\nConnection: close\r\n\r\n"
        PROBE_BODY;
    static const char bad_status[] =
        "HTTP/1.1 503 Unavailable\r\n\r\n" PROBE_BODY;
    static const char bad_body[] =
        "HTTP/1.0 200 OK\r\n\r\nASTERINAS_TCP_PROBE_BAD\n";
    static const char trailing_data[] =
        "HTTP/1.0 200 OK\r\n\r\n" PROBE_BODY "unexpected";
    char stress_header[128];
    char stress_body[257];
    struct response_stream stress = {0};
    struct response_stream invalid = {0};
    size_t offset = 0;
    int header_length;

    if (!response_is_valid(valid, sizeof(valid) - 1) ||
        response_is_valid(bad_status, sizeof(bad_status) - 1) ||
        response_is_valid(bad_body, sizeof(bad_body) - 1) ||
        response_is_valid(trailing_data, sizeof(trailing_data) - 1)) {
        return 1;
    }
    header_length = snprintf(stress_header, sizeof(stress_header),
                             "HTTP/1.1 200 OK\r\nContent-Length: %zu\r\n"
                             "Connection: close\r\n\r\n",
                             (size_t)MEGREZ_TCP_STRESS_BYTES);
    if (header_length <= 0 || (size_t)header_length >= sizeof(stress_header) ||
        !response_stream_consume(&stress, stress_header, 7) ||
        !response_stream_consume(&stress, stress_header + 7,
                                 (size_t)header_length - 7)) {
        return 1;
    }
    while (offset < MEGREZ_TCP_STRESS_BYTES) {
        size_t amount = MEGREZ_TCP_STRESS_BYTES - offset;
        size_t index;

        if (amount > sizeof(stress_body)) {
            amount = sizeof(stress_body);
        }
        for (index = 0; index < amount; ++index) {
            stress_body[index] = (char)((offset + index) % 251U);
        }
        if (!response_stream_consume(&stress, stress_body, amount)) {
            return 1;
        }
        offset += amount;
    }
    if (!response_stream_is_complete(&stress) ||
        !response_stream_consume(&invalid, stress_header,
                                 (size_t)header_length) ||
        response_stream_consume(&invalid, "\x01", 1)) {
        return 1;
    }
    emit_failure(&failure);
    puts("MEGREZ_TCP_PROBE_SELF_TEST PASS");
    printf("MEGREZ_TCP_STRESS_SELF_TEST PASS bytes=%zu pattern=mod251\n",
           (size_t)MEGREZ_TCP_STRESS_BYTES);
    return 0;
}

#else

#include <arpa/inet.h>
#include <errno.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define PROBE_ADDRESS "10.100.19.216"
#define PROBE_PORT 18080
#define PROBE_DEADLINE_MILLISECONDS 60000
#define CONNECT_ATTEMPT_MILLISECONDS 3000

static struct probe_failure last_failure = {
    .reason = "not-started",
    .error = 0,
    .attempts = 0,
};

static void record_failure(const char *reason, int error)
{
    last_failure.reason = reason;
    last_failure.error = error;
}

static int64_t monotonic_milliseconds(void)
{
    struct timespec now;

    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return -1;
    }
    return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static void sleep_one_second(void)
{
    struct timespec delay = {.tv_sec = 1, .tv_nsec = 0};

    while (nanosleep(&delay, &delay) != 0 && errno == EINTR) {
    }
}

static int wait_for_fd(int fd, short events, int64_t deadline)
{
    struct pollfd descriptor = {.fd = fd, .events = events, .revents = 0};

    for (;;) {
        int64_t now = monotonic_milliseconds();
        int timeout;
        int result;

        if (now < 0) {
            errno = EIO;
            return -1;
        }
        if (now >= deadline) {
            errno = ETIMEDOUT;
            return -1;
        }
        timeout = (deadline - now > INT_MAX) ? INT_MAX : (int)(deadline - now);
        result = poll(&descriptor, 1, timeout);
        if (result > 0) {
            if ((descriptor.revents & events) != 0) {
                return 0;
            }
            errno = EIO;
            return -1;
        }
        if (result == 0) {
            errno = ETIMEDOUT;
            return -1;
        }
        if (errno != EINTR) {
            return -1;
        }
    }
}

static int connect_probe(int64_t overall_deadline)
{
    struct sockaddr_in address = {
        .sin_family = AF_INET,
        .sin_port = htons(PROBE_PORT),
    };
    int64_t now;
    int64_t attempt_deadline;
    int error = 0;
    socklen_t error_length = sizeof(error);
    int fd;

    fd = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK, 0);
    if (fd < 0) {
        record_failure("socket", errno);
        return -1;
    }
    if (inet_pton(AF_INET, PROBE_ADDRESS, &address.sin_addr) != 1) {
        record_failure("address", 0);
        close(fd);
        return -1;
    }
    if (connect(fd, (const struct sockaddr *)&address, sizeof(address)) == 0) {
        return fd;
    }
    if (errno != EINPROGRESS) {
        record_failure("connect", errno);
        close(fd);
        return -1;
    }
    now = monotonic_milliseconds();
    if (now < 0) {
        record_failure("clock", errno);
        close(fd);
        return -1;
    }
    attempt_deadline = now + CONNECT_ATTEMPT_MILLISECONDS;
    if (attempt_deadline > overall_deadline) {
        attempt_deadline = overall_deadline;
    }
    if (wait_for_fd(fd, POLLOUT, attempt_deadline) != 0) {
        record_failure("connect-poll", errno);
        close(fd);
        return -1;
    }
    if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &error_length) != 0) {
        record_failure("getsockopt", errno);
        close(fd);
        return -1;
    }
    if (error != 0) {
        record_failure("connect-error", error);
        close(fd);
        return -1;
    }
    return fd;
}

static bool send_request(int fd, int64_t deadline)
{
    static const char request[] =
        "GET /asterinas-probe HTTP/1.0\r\n"
        "Host: " PROBE_ADDRESS "\r\n"
        "Connection: close\r\n\r\n";
    size_t written = 0;

    while (written < sizeof(request) - 1) {
        ssize_t amount = send(fd, request + written, sizeof(request) - 1 - written, 0);

        if (amount > 0) {
            written += (size_t)amount;
            continue;
        }
        if (amount < 0 && errno == EINTR) {
            continue;
        }
        if (amount < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) &&
            wait_for_fd(fd, POLLOUT, deadline) == 0) {
            continue;
        }
        record_failure(
            amount < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)
                ? "send-poll"
                : "send",
            errno);
        return false;
    }
    return true;
}

static bool receive_response(int fd, int64_t deadline)
{
    char response[RESPONSE_READ_BYTES];
    struct response_stream stream = {0};

    for (;;) {
        ssize_t amount;

        if (wait_for_fd(fd, POLLIN, deadline) != 0) {
            record_failure("receive-poll", errno);
            return false;
        }
        amount = recv(fd, response, sizeof(response), 0);
        if (amount > 0) {
            if (!response_stream_consume(&stream, response, (size_t)amount)) {
                record_failure("http-response", 0);
                return false;
            }
            continue;
        }
        if (amount == 0) {
            bool valid = response_stream_is_complete(&stream);

            if (!valid) {
                record_failure("http-response", 0);
            }
            return valid;
        }
        if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) {
            continue;
        }
        record_failure("receive", errno);
        return false;
    }
}

static _Noreturn void terminal(bool passed)
{
    if (passed) {
        printf("ASTERINAS_GMAC_TCP_PROBE_READY peer=" PROBE_ADDRESS
               ":18080 status=200 bytes=%zu pattern=mod251\n",
               (size_t)MEGREZ_TCP_STRESS_BYTES);
    } else {
        emit_failure(&last_failure);
    }
    fflush(stdout);
    for (;;) {
        pause();
    }
}

int main(void)
{
    int64_t start = monotonic_milliseconds();
    int64_t deadline;

    signal(SIGPIPE, SIG_IGN);
    if (start < 0) {
        record_failure("clock", errno);
        terminal(false);
    }
    deadline = start + PROBE_DEADLINE_MILLISECONDS;
    while (monotonic_milliseconds() < deadline) {
        ++last_failure.attempts;
        int fd = connect_probe(deadline);

        if (fd >= 0) {
            bool passed = send_request(fd, deadline) && receive_response(fd, deadline);

            close(fd);
            if (passed) {
                terminal(true);
            }
        }
        sleep_one_second();
    }
    terminal(false);
}

#endif
