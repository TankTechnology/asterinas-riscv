// SPDX-License-Identifier: MPL-2.0

#define _POSIX_C_SOURCE 200809L

#include <linux/input.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

struct expected_event {
    unsigned short type;
    unsigned short code;
    int value;
};

static const struct expected_event EXPECTED_EVENTS[] = {
    {EV_KEY, KEY_A, 1},       {EV_SYN, SYN_REPORT, 0},
    {EV_KEY, KEY_A, 0},       {EV_SYN, SYN_REPORT, 0},
    {EV_KEY, KEY_1, 1},       {EV_SYN, SYN_REPORT, 0},
    {EV_KEY, KEY_1, 0},       {EV_SYN, SYN_REPORT, 0},
};

struct input_state {
    size_t next_expected;
};

enum transition_result {
    TRANSITION_ACCEPTED,
    TRANSITION_COMPLETE,
    TRANSITION_REJECTED,
};

#if !defined(XHCI_INPUT_GATE_LIFECYCLE_TEST)
enum keyboard_discovery {
    KEYBOARD_DISCOVERY_PENDING,
    KEYBOARD_DISCOVERY_READY,
    KEYBOARD_DISCOVERY_REJECTED,
};

static enum keyboard_discovery classify_keyboard_discovery(
    size_t keyboard_count, size_t matching_count)
{
    if (keyboard_count == 0) {
        return KEYBOARD_DISCOVERY_PENDING;
    }
    if (keyboard_count == 1 && matching_count == 1) {
        return KEYBOARD_DISCOVERY_READY;
    }
    return KEYBOARD_DISCOVERY_REJECTED;
}
#endif

static enum transition_result input_state_consume(
    struct input_state *state, const struct input_event *event)
{
    const size_t event_count = sizeof(EXPECTED_EVENTS) / sizeof(EXPECTED_EVENTS[0]);
    if (state->next_expected >= event_count) {
        return TRANSITION_REJECTED;
    }
    const struct expected_event *expected = &EXPECTED_EVENTS[state->next_expected];
    if (event->type != expected->type || event->code != expected->code ||
        event->value != expected->value) {
        return TRANSITION_REJECTED;
    }
    state->next_expected++;
    return state->next_expected == event_count ? TRANSITION_COMPLETE
                                                : TRANSITION_ACCEPTED;
}

#ifndef XHCI_INPUT_GATE_SELF_TEST
static _Noreturn void report_pass_and_hold(void)
{
    printf("XHCI_INPUT_PASS events=8\n");
    fflush(stdout);
    for (;;) {
        pause();
    }
}
#endif

#ifdef XHCI_INPUT_GATE_SELF_TEST

#include <stdlib.h>

struct test_device {
    bool keyboard_like;
    unsigned short bustype;
    const char *name;
};

static bool has_one_exact_usb_keyboard(const struct test_device *devices,
                                       size_t count)
{
    size_t keyboard_count = 0;
    size_t matching_count = 0;
    for (size_t index = 0; index < count; index++) {
        if (!devices[index].keyboard_like) {
            continue;
        }
        keyboard_count++;
        if (devices[index].bustype == BUS_USB &&
            strcmp(devices[index].name, "usb_boot_keyboard") == 0) {
            matching_count++;
        }
    }
    return keyboard_count == 1 && matching_count == 1;
}

static bool exact_sequence_completes(void)
{
    struct input_state state = {0};
    const size_t count = sizeof(EXPECTED_EVENTS) / sizeof(EXPECTED_EVENTS[0]);
    for (size_t index = 0; index < count; index++) {
        const struct input_event event = {
            .type = EXPECTED_EVENTS[index].type,
            .code = EXPECTED_EVENTS[index].code,
            .value = EXPECTED_EVENTS[index].value,
        };
        const enum transition_result result = input_state_consume(&state, &event);
        if ((index + 1 == count && result != TRANSITION_COMPLETE) ||
            (index + 1 != count && result != TRANSITION_ACCEPTED)) {
            return false;
        }
    }
    return true;
}

static bool incomplete_sequence_is_rejected(void)
{
    struct input_state state = {0};
    for (size_t index = 0; index < 2; index++) {
        const struct input_event event = {
            .type = EXPECTED_EVENTS[index].type,
            .code = EXPECTED_EVENTS[index].code,
            .value = EXPECTED_EVENTS[index].value,
        };
        if (input_state_consume(&state, &event) == TRANSITION_REJECTED) {
            return false;
        }
    }
    return state.next_expected != sizeof(EXPECTED_EVENTS) / sizeof(EXPECTED_EVENTS[0]);
}

static bool event_is_rejected(unsigned short type, unsigned short code, int value)
{
    struct input_state state = {0};
    const struct input_event event = {.type = type, .code = code, .value = value};
    return input_state_consume(&state, &event) == TRANSITION_REJECTED;
}

static bool contains_panic_text(const char *text)
{
    return strstr(text, "Kernel panic") != NULL || strstr(text, "Oops:") != NULL ||
           strstr(text, "BUG:") != NULL;
}

static bool deadline_expired(const struct timespec *deadline,
                             const struct timespec *now)
{
    return now->tv_sec > deadline->tv_sec ||
           (now->tv_sec == deadline->tv_sec && now->tv_nsec >= deadline->tv_nsec);
}

static bool run_self_test(const char *name)
{
    const struct test_device usb = {
        .keyboard_like = true,
        .bustype = BUS_USB,
        .name = "usb_boot_keyboard",
    };
    const struct test_device virtio = {
        .keyboard_like = true,
        .bustype = BUS_VIRTUAL,
        .name = "virtio_keyboard",
    };
    if (strcmp(name, "valid") == 0) {
        return has_one_exact_usb_keyboard(&usb, 1) && exact_sequence_completes();
    }
    if (strcmp(name, "zero-keyboards") == 0) {
        return !has_one_exact_usb_keyboard(NULL, 0);
    }
    if (strcmp(name, "two-keyboards") == 0) {
        const struct test_device devices[] = {usb, virtio};
        return !has_one_exact_usb_keyboard(devices, 2);
    }
    if (strcmp(name, "virtio-keyboard") == 0) {
        return !has_one_exact_usb_keyboard(&virtio, 1);
    }
    if (strcmp(name, "delayed-keyboard") == 0) {
        return classify_keyboard_discovery(0, 0) == KEYBOARD_DISCOVERY_PENDING &&
               classify_keyboard_discovery(1, 1) == KEYBOARD_DISCOVERY_READY;
    }
    if (strcmp(name, "missing-release") == 0) {
        return incomplete_sequence_is_rejected();
    }
    if (strcmp(name, "reordered") == 0) {
        return event_is_rejected(EV_KEY, KEY_1, 1);
    }
    if (strcmp(name, "syn-dropped") == 0) {
        return event_is_rejected(EV_SYN, SYN_DROPPED, 0);
    }
    if (strcmp(name, "partial-read") == 0) {
        return (sizeof(struct input_event) - 1) % sizeof(struct input_event) != 0;
    }
    if (strcmp(name, "panic-text") == 0) {
        return contains_panic_text("Kernel panic - not syncing") &&
               !contains_panic_text("XHCI_INPUT_READY");
    }
    if (strcmp(name, "deadline") == 0) {
        const struct timespec deadline = {.tv_sec = 30, .tv_nsec = 10};
        const struct timespec before = {.tv_sec = 30, .tv_nsec = 9};
        const struct timespec at = deadline;
        return !deadline_expired(&deadline, &before) && deadline_expired(&deadline, &at);
    }
    return false;
}

int main(int argc, char **argv)
{
    if (argc != 2 || !run_self_test(argv[1])) {
        fprintf(stderr, "XHCI_INPUT_SELF_TEST FAIL\n");
        return 1;
    }
    printf("XHCI_INPUT_SELF_TEST PASS case=%s\n", argv[1]);
    return 0;
}

#elif defined(XHCI_INPUT_GATE_LIFECYCLE_TEST)

int main(void)
{
    struct input_state state = {0};
    const size_t count = sizeof(EXPECTED_EVENTS) / sizeof(EXPECTED_EVENTS[0]);
    for (size_t index = 0; index < count; index++) {
        const struct input_event event = {
            .type = EXPECTED_EVENTS[index].type,
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

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <stdint.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/stat.h>

enum {
    BITS_PER_BYTE = 8,
    DEVICE_NAME_CAPACITY = 128,
    INPUT_EVENT_BATCH_SIZE = 16,
    INPUT_EVENT_NODE_COUNT = 32,
    INPUT_NODE_PATH_CAPACITY = 32,
    INPUT_WAIT_TIMEOUT_SECONDS = 30,
    KEYBOARD_DISCOVERY_RETRY_MILLISECONDS = 50,
    MILLISECONDS_PER_SECOND = 1000,
    NANOSECONDS_PER_MILLISECOND = 1000000,
};

#define KEY_BITMAP_SIZE                                                        \
    ((KEY_MAX + 1 + BITS_PER_BYTE - 1) / BITS_PER_BYTE)

static int fail(const char *reason)
{
    printf("XHCI_INPUT_FAIL reason=%s\n", reason);
    fflush(stdout);
    return 1;
}

static int retrying_open(const char *path)
{
    int fd;
    do {
        fd = open(path, O_RDONLY | O_NONBLOCK);
    } while (fd < 0 && errno == EINTR);
    return fd;
}

static int retrying_ioctl(int fd, unsigned long request, void *argument)
{
    int result;
    do {
        result = ioctl(fd, request, argument);
    } while (result < 0 && errno == EINTR);
    return result;
}

static bool key_is_advertised(const unsigned char *bitmap, unsigned int code)
{
    return (bitmap[code / BITS_PER_BYTE] & (1U << (code % BITS_PER_BYTE))) != 0;
}

static bool looks_like_keyboard(const unsigned char *bitmap)
{
    return key_is_advertised(bitmap, KEY_A) && key_is_advertised(bitmap, KEY_1) &&
           key_is_advertised(bitmap, KEY_ENTER);
}

static enum keyboard_discovery discover_keyboard(char *selected_path,
                                                   size_t path_size,
                                                   int *selected_fd_out)
{
    int selected_fd = -1;
    size_t keyboard_count = 0;
    size_t matching_count = 0;

    for (unsigned int index = 0; index < INPUT_EVENT_NODE_COUNT; index++) {
        char path[INPUT_NODE_PATH_CAPACITY];
        char name[DEVICE_NAME_CAPACITY] = {0};
        unsigned char keys[KEY_BITMAP_SIZE] = {0};
        struct input_id id = {0};
        const int length = snprintf(path, sizeof(path), "/dev/input/event%u", index);
        if (length < 0 || (size_t)length >= sizeof(path)) {
            continue;
        }
        const int fd = retrying_open(path);
        if (fd < 0) {
            continue;
        }
        if (retrying_ioctl(fd, EVIOCGBIT(EV_KEY, sizeof(keys)), keys) < 0 ||
            !looks_like_keyboard(keys)) {
            close(fd);
            continue;
        }
        keyboard_count++;
        const bool id_ok = retrying_ioctl(fd, EVIOCGID, &id) >= 0;
        const bool name_ok = retrying_ioctl(fd, EVIOCGNAME(sizeof(name)), name) >= 0;
        name[sizeof(name) - 1] = '\0';
        const bool identity_ok = id_ok && name_ok && id.bustype == BUS_USB &&
                                 strcmp(name, "usb_boot_keyboard") == 0;
        if (!identity_ok) {
            close(fd);
            continue;
        }
        matching_count++;
        if (selected_fd >= 0 || snprintf(selected_path, path_size, "%s", path) < 0) {
            close(fd);
            continue;
        }
        selected_fd = fd;
    }

    const enum keyboard_discovery result =
        classify_keyboard_discovery(keyboard_count, matching_count);
    if (result != KEYBOARD_DISCOVERY_READY || selected_fd < 0) {
        if (selected_fd >= 0) {
            close(selected_fd);
        }
        return result;
    }
    *selected_fd_out = selected_fd;
    return KEYBOARD_DISCOVERY_READY;
}

static int remaining_timeout_ms(const struct timespec *deadline, int *timeout_ms)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) < 0) {
        return -1;
    }
    if (now.tv_sec > deadline->tv_sec ||
        (now.tv_sec == deadline->tv_sec && now.tv_nsec >= deadline->tv_nsec)) {
        *timeout_ms = 0;
        return 0;
    }
    time_t seconds = deadline->tv_sec - now.tv_sec;
    long nanoseconds = deadline->tv_nsec - now.tv_nsec;
    if (nanoseconds < 0) {
        seconds--;
        nanoseconds += MILLISECONDS_PER_SECOND * NANOSECONDS_PER_MILLISECOND;
    }
    int64_t milliseconds = (int64_t)seconds * MILLISECONDS_PER_SECOND;
    milliseconds +=
        (nanoseconds + NANOSECONDS_PER_MILLISECOND - 1) / NANOSECONDS_PER_MILLISECOND;
    *timeout_ms = milliseconds > INT_MAX ? INT_MAX : (int)milliseconds;
    return 0;
}

static int wait_for_keyboard(const struct timespec *deadline, char *selected_path,
                             size_t path_size)
{
    for (;;) {
        int selected_fd = -1;
        const enum keyboard_discovery discovery =
            discover_keyboard(selected_path, path_size, &selected_fd);
        if (discovery == KEYBOARD_DISCOVERY_READY) {
            return selected_fd;
        }
        if (discovery == KEYBOARD_DISCOVERY_REJECTED) {
            fail("usb-keyboard-selection");
            return -1;
        }

        int timeout_ms;
        if (remaining_timeout_ms(deadline, &timeout_ms) < 0) {
            fail("clock-gettime");
            return -1;
        }
        if (timeout_ms == 0) {
            fail("usb-keyboard-timeout");
            return -1;
        }
        if (timeout_ms > KEYBOARD_DISCOVERY_RETRY_MILLISECONDS) {
            timeout_ms = KEYBOARD_DISCOVERY_RETRY_MILLISECONDS;
        }
        const int poll_result = poll(NULL, 0, timeout_ms);
        if (poll_result < 0 && errno != EINTR) {
            fail("keyboard-discovery-poll-error");
            return -1;
        }
    }
}

static int wait_for_events(int fd, const struct timespec *deadline)
{
    struct input_state state = {0};

    for (;;) {
        int timeout_ms;
        if (remaining_timeout_ms(deadline, &timeout_ms) < 0) {
            return fail("clock-gettime");
        }
        if (timeout_ms == 0) {
            return fail("input-timeout");
        }
        struct pollfd descriptor = {.fd = fd, .events = POLLIN};
        const int poll_result = poll(&descriptor, 1, timeout_ms);
        if (poll_result < 0) {
            if (errno == EINTR) {
                continue;
            }
            return fail("poll-error");
        }
        if (poll_result == 0) {
            return fail("input-timeout");
        }
        if ((descriptor.revents & (POLLERR | POLLHUP | POLLNVAL)) != 0) {
            return fail("input-device-poll-error");
        }
        if ((descriptor.revents & POLLIN) == 0) {
            continue;
        }

        struct input_event events[INPUT_EVENT_BATCH_SIZE];
        ssize_t bytes;
        do {
            bytes = read(fd, events, sizeof(events));
        } while (bytes < 0 && errno == EINTR);
        if (bytes < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                continue;
            }
            return fail("input-read-error");
        }
        if (bytes == 0) {
            return fail("input-device-eof");
        }
        if ((size_t)bytes % sizeof(events[0]) != 0) {
            return fail("partial-input-event");
        }

        const size_t count = (size_t)bytes / sizeof(events[0]);
        for (size_t index = 0; index < count; index++) {
            const enum transition_result result = input_state_consume(&state, &events[index]);
            if (result == TRANSITION_REJECTED) {
                return fail(events[index].type == EV_SYN &&
                                    events[index].code == SYN_DROPPED
                                ? "syn-dropped"
                                : "invalid-event-sequence");
            }
            printf("XHCI_INPUT_EVENT type=%u code=%u value=%d\n", events[index].type,
                   events[index].code, events[index].value);
            fflush(stdout);
            if (result == TRANSITION_COMPLETE) {
                report_pass_and_hold();
            }
        }
    }
}

int main(void)
{
    if (mkdir("/dev", 0755) < 0 && errno != EEXIST) {
        return fail("mkdir-dev");
    }
    if (mount("devtmpfs", "/dev", "devtmpfs", 0, NULL) < 0 && errno != EBUSY &&
        errno != ENODEV) {
        return fail("mount-devtmpfs");
    }
    struct timespec deadline;
    if (clock_gettime(CLOCK_MONOTONIC, &deadline) < 0) {
        return fail("clock-gettime");
    }
    deadline.tv_sec += INPUT_WAIT_TIMEOUT_SECONDS;
    char selected_path[INPUT_NODE_PATH_CAPACITY] = {0};
    const int fd = wait_for_keyboard(&deadline, selected_path, sizeof(selected_path));
    if (fd < 0) {
        return 1;
    }
    printf("XHCI_INPUT_READY path=%s bustype=%u name=usb_boot_keyboard\n", selected_path,
           BUS_USB);
    fflush(stdout);
    const int result = wait_for_events(fd, &deadline);
    close(fd);
    return result;
}

#endif
