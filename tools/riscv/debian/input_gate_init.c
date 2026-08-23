// SPDX-License-Identifier: MPL-2.0

#define _POSIX_C_SOURCE 200809L

#include <linux/input.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>

struct expected_event {
    unsigned short code;
    int value;
};

static const struct expected_event EXPECTED_EVENTS[] = {
    {KEY_A, 1},         {KEY_A, 0},
    {KEY_LEFTSHIFT, 1}, {KEY_B, 1},
    {KEY_B, 0},         {KEY_LEFTSHIFT, 0},
    {KEY_BACKSPACE, 1}, {KEY_BACKSPACE, 0},
    {KEY_LEFTCTRL, 1},  {KEY_C, 1},
    {KEY_C, 0},         {KEY_LEFTCTRL, 0},
};

struct input_state {
    size_t next_expected;
};

enum transition_result {
    TRANSITION_IGNORED,
    TRANSITION_ACCEPTED,
    TRANSITION_COMPLETE,
    TRANSITION_REJECTED,
};

static enum transition_result input_state_consume(
    struct input_state *state, const struct input_event *event)
{
    const size_t expected_event_count =
        sizeof(EXPECTED_EVENTS) / sizeof(EXPECTED_EVENTS[0]);

    if (event->type != EV_KEY || event->value == 2) {
        return TRANSITION_IGNORED;
    }
    if (state->next_expected >= expected_event_count ||
        (event->value != 0 && event->value != 1)) {
        return TRANSITION_REJECTED;
    }

    const struct expected_event *expected =
        &EXPECTED_EVENTS[state->next_expected];
    if (event->code != expected->code || event->value != expected->value) {
        return TRANSITION_REJECTED;
    }

    state->next_expected++;
    if (state->next_expected == expected_event_count) {
        return TRANSITION_COMPLETE;
    }
    return TRANSITION_ACCEPTED;
}

#ifdef INPUT_GATE_SELF_TEST

static bool exact_sequence_completes(void)
{
    struct input_state state = {0};
    const size_t expected_event_count =
        sizeof(EXPECTED_EVENTS) / sizeof(EXPECTED_EVENTS[0]);

    for (size_t index = 0; index < expected_event_count; index++) {
        const struct input_event event = {
            .type = EV_KEY,
            .code = EXPECTED_EVENTS[index].code,
            .value = EXPECTED_EVENTS[index].value,
        };
        const enum transition_result result = input_state_consume(&state, &event);

        if (index + 1 == expected_event_count) {
            if (result != TRANSITION_COMPLETE) {
                return false;
            }
        } else if (result != TRANSITION_ACCEPTED) {
            return false;
        }
    }
    return true;
}

static bool ignored_events_do_not_advance(void)
{
    struct input_state state = {0};
    const struct input_event synchronization_event = {.type = EV_SYN};
    const struct input_event repeat_event = {
        .type = EV_KEY,
        .code = KEY_A,
        .value = 2,
    };

    return input_state_consume(&state, &synchronization_event) ==
               TRANSITION_IGNORED &&
           input_state_consume(&state, &repeat_event) == TRANSITION_IGNORED &&
           state.next_expected == 0;
}

static bool out_of_order_event_is_rejected(void)
{
    struct input_state state = {0};
    const struct input_event out_of_order_event = {
        .type = EV_KEY,
        .code = KEY_B,
        .value = 1,
    };

    return input_state_consume(&state, &out_of_order_event) ==
               TRANSITION_REJECTED &&
           state.next_expected == 0;
}

int main(void)
{
    if (!exact_sequence_completes() || !ignored_events_do_not_advance() ||
        !out_of_order_event_is_rejected()) {
        fprintf(stderr, "input gate state machine: FAIL\n");
        return 1;
    }

    printf("input gate state machine: PASS\n");
    return 0;
}

#else

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <stdint.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

static _Noreturn void report_pass_and_hold(void)
{
    printf("__DEBIAN_INPUT_GATE_PASS__\n");
    fflush(stdout);

    for (;;) {
        if (pause() < 0 && errno == EINTR) {
            continue;
        }
    }
}

#ifdef INPUT_GATE_LIFECYCLE_TEST

int main(void)
{
    struct input_state state = {0};
    const size_t expected_event_count =
        sizeof(EXPECTED_EVENTS) / sizeof(EXPECTED_EVENTS[0]);

    for (size_t index = 0; index < expected_event_count; index++) {
        const struct input_event event = {
            .type = EV_KEY,
            .code = EXPECTED_EVENTS[index].code,
            .value = EXPECTED_EVENTS[index].value,
        };
        const enum transition_result result = input_state_consume(&state, &event);

        if (result == TRANSITION_COMPLETE) {
            report_pass_and_hold();
        }
        if (result != TRANSITION_ACCEPTED) {
            return 1;
        }
    }
    return 1;
}

#else

enum {
    BITS_PER_BYTE = 8,
    DEVICE_NAME_CAPACITY = 128,
    INPUT_EVENT_BATCH_SIZE = 16,
    INPUT_EVENT_NODE_COUNT = 32,
    INPUT_NODE_PATH_CAPACITY = 32,
    INPUT_WAIT_TIMEOUT_SECONDS = 30,
    NANOSECONDS_PER_MILLISECOND = 1000000,
    MILLISECONDS_PER_SECOND = 1000,
};

#define KEY_BITMAP_SIZE                                                        \
    ((KEY_MAX + 1 + BITS_PER_BYTE - 1) / BITS_PER_BYTE)

static int fail(const char *reason)
{
    printf("__DEBIAN_INPUT_GATE_FAIL__ reason=%s\n", reason);
    fflush(stdout);
    return 1;
}

static int retrying_open(const char *path)
{
    int file_descriptor;

    do {
        file_descriptor = open(path, O_RDONLY | O_NONBLOCK);
    } while (file_descriptor < 0 && errno == EINTR);
    return file_descriptor;
}

static int retrying_ioctl(int file_descriptor, unsigned long request,
                          void *argument)
{
    int result;

    do {
        result = ioctl(file_descriptor, request, argument);
    } while (result < 0 && errno == EINTR);
    return result;
}

static bool key_is_advertised(const unsigned char *key_bitmap,
                              unsigned int key_code)
{
    return (key_bitmap[key_code / BITS_PER_BYTE] &
            (1U << (key_code % BITS_PER_BYTE))) != 0;
}

static bool has_required_keyboard_keys(const unsigned char *key_bitmap)
{
    static const unsigned short required_keys[] = {
        KEY_A,         KEY_B,         KEY_C,        KEY_ENTER,
        KEY_BACKSPACE, KEY_LEFTSHIFT, KEY_LEFTCTRL,
    };

    for (size_t index = 0;
         index < sizeof(required_keys) / sizeof(required_keys[0]); index++) {
        if (!key_is_advertised(key_bitmap, required_keys[index])) {
            return false;
        }
    }
    return true;
}

static int discover_keyboard(char *selected_node, size_t selected_node_size,
                             char *device_name, size_t device_name_size)
{
    for (unsigned int index = 0; index < INPUT_EVENT_NODE_COUNT; index++) {
        char candidate_node[INPUT_NODE_PATH_CAPACITY];
        unsigned char key_bitmap[KEY_BITMAP_SIZE] = {0};

        const int node_length = snprintf(candidate_node, sizeof(candidate_node),
                                         "/dev/input/event%u", index);
        if (node_length < 0 || (size_t)node_length >= sizeof(candidate_node)) {
            continue;
        }

        const int file_descriptor = retrying_open(candidate_node);
        if (file_descriptor < 0) {
            continue;
        }

        if (retrying_ioctl(file_descriptor,
                           EVIOCGBIT(EV_KEY, sizeof(key_bitmap)), key_bitmap) <
                0 ||
            !has_required_keyboard_keys(key_bitmap)) {
            close(file_descriptor);
            continue;
        }

        const int selected_node_length =
            snprintf(selected_node, selected_node_size, "%s", candidate_node);
        if (selected_node_length < 0 ||
            (size_t)selected_node_length >= selected_node_size) {
            close(file_descriptor);
            continue;
        }

        memset(device_name, 0, device_name_size);
        if (retrying_ioctl(file_descriptor, EVIOCGNAME(device_name_size),
                           device_name) < 0 ||
            device_name[0] == '\0') {
            snprintf(device_name, device_name_size, "unavailable");
        } else {
            device_name[device_name_size - 1] = '\0';
        }
        return file_descriptor;
    }
    return -1;
}

static int remaining_timeout_milliseconds(const struct timespec *deadline,
                                          int *timeout_milliseconds)
{
    struct timespec now;

    if (clock_gettime(CLOCK_MONOTONIC, &now) < 0) {
        return -1;
    }
    if (now.tv_sec > deadline->tv_sec ||
        (now.tv_sec == deadline->tv_sec && now.tv_nsec >= deadline->tv_nsec)) {
        *timeout_milliseconds = 0;
        return 0;
    }

    time_t remaining_seconds = deadline->tv_sec - now.tv_sec;
    long remaining_nanoseconds = deadline->tv_nsec - now.tv_nsec;
    if (remaining_nanoseconds < 0) {
        remaining_seconds--;
        remaining_nanoseconds +=
            MILLISECONDS_PER_SECOND * NANOSECONDS_PER_MILLISECOND;
    }

    int64_t remaining_milliseconds =
        (int64_t)remaining_seconds * MILLISECONDS_PER_SECOND;
    remaining_milliseconds +=
        (remaining_nanoseconds + NANOSECONDS_PER_MILLISECOND - 1) /
        NANOSECONDS_PER_MILLISECOND;
    *timeout_milliseconds = remaining_milliseconds > INT_MAX
                                ? INT_MAX
                                : (int)remaining_milliseconds;
    return 0;
}

static int report_invalid_order(const struct input_state *state,
                                const struct input_event *event)
{
    const size_t expected_event_count =
        sizeof(EXPECTED_EVENTS) / sizeof(EXPECTED_EVENTS[0]);

    if (state->next_expected < expected_event_count) {
        const struct expected_event *expected =
            &EXPECTED_EVENTS[state->next_expected];
        printf("__DEBIAN_INPUT_GATE_FAIL__ reason=invalid-key-order "
               "expected_code=%u expected_value=%d actual_code=%u "
               "actual_value=%d\n",
               expected->code, expected->value, event->code, event->value);
    } else {
        printf("__DEBIAN_INPUT_GATE_FAIL__ reason=unexpected-key-after-completion "
               "actual_code=%u actual_value=%d\n",
               event->code, event->value);
    }
    fflush(stdout);
    return 1;
}

static int wait_for_expected_events(int file_descriptor)
{
    struct input_state state = {0};
    struct timespec deadline;

    if (clock_gettime(CLOCK_MONOTONIC, &deadline) < 0) {
        return fail("clock-gettime");
    }
    deadline.tv_sec += INPUT_WAIT_TIMEOUT_SECONDS;

    for (;;) {
        int timeout_milliseconds;
        if (remaining_timeout_milliseconds(&deadline, &timeout_milliseconds) <
            0) {
            return fail("clock-gettime");
        }
        if (timeout_milliseconds == 0) {
            return fail("input-timeout");
        }

        struct pollfd poll_descriptor = {
            .fd = file_descriptor,
            .events = POLLIN,
        };
        const int poll_result =
            poll(&poll_descriptor, 1, timeout_milliseconds);
        if (poll_result < 0) {
            if (errno == EINTR) {
                continue;
            }
            return fail("poll-error");
        }
        if (poll_result == 0) {
            return fail("input-timeout");
        }
        if ((poll_descriptor.revents & (POLLERR | POLLHUP | POLLNVAL)) != 0) {
            return fail("input-device-poll-error");
        }
        if ((poll_descriptor.revents & POLLIN) == 0) {
            continue;
        }

        struct input_event events[INPUT_EVENT_BATCH_SIZE];
        ssize_t byte_count;
        do {
            byte_count = read(file_descriptor, events, sizeof(events));
        } while (byte_count < 0 && errno == EINTR);

        if (byte_count < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                continue;
            }
            return fail("input-read-error");
        }
        if (byte_count == 0) {
            return fail("input-device-eof");
        }
        if ((size_t)byte_count % sizeof(events[0]) != 0) {
            return fail("partial-input-event");
        }

        const size_t event_count = (size_t)byte_count / sizeof(events[0]);
        for (size_t index = 0; index < event_count; index++) {
            const enum transition_result result =
                input_state_consume(&state, &events[index]);

            if (result == TRANSITION_REJECTED) {
                return report_invalid_order(&state, &events[index]);
            }
            if (result == TRANSITION_COMPLETE) {
                report_pass_and_hold();
            }
        }
    }
}

int main(void)
{
    char selected_node[INPUT_NODE_PATH_CAPACITY];
    char device_name[DEVICE_NAME_CAPACITY];
    const int file_descriptor =
        discover_keyboard(selected_node, sizeof(selected_node), device_name,
                          sizeof(device_name));

    if (file_descriptor < 0) {
        return fail("no-compatible-keyboard");
    }

    printf("__DEBIAN_INPUT_GATE_READY__ node=%s name=%s\n", selected_node,
           device_name);
    fflush(stdout);

    const int result = wait_for_expected_events(file_descriptor);
    close(file_descriptor);
    return result;
}

#endif

#endif
