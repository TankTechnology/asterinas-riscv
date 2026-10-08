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

static const struct expected_event EXPECTED_KEYBOARD_EVENTS[] = {
    {EV_KEY, KEY_A, 1},       {EV_SYN, SYN_REPORT, 0},
    {EV_KEY, KEY_A, 0},       {EV_SYN, SYN_REPORT, 0},
    {EV_KEY, KEY_1, 1},       {EV_SYN, SYN_REPORT, 0},
    {EV_KEY, KEY_1, 0},       {EV_SYN, SYN_REPORT, 0},
};

static const struct expected_event EXPECTED_MOUSE_EVENTS[] = {
    {EV_REL, REL_X, 17},       {EV_REL, REL_Y, -9},
    {EV_SYN, SYN_REPORT, 0},  {EV_KEY, BTN_LEFT, 1},
    {EV_SYN, SYN_REPORT, 0},  {EV_KEY, BTN_LEFT, 0},
    {EV_SYN, SYN_REPORT, 0},
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

#ifdef XHCI_INPUT_GATE_SELF_TEST
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
#endif

static enum transition_result input_state_consume(
    struct input_state *state, const struct input_event *event,
    const struct expected_event *expected_events, size_t event_count)
{
    if (state->next_expected >= event_count) {
        return TRANSITION_REJECTED;
    }
    const struct expected_event *expected = &expected_events[state->next_expected];
    if (event->type != expected->type || event->code != expected->code ||
        event->value != expected->value) {
        return TRANSITION_REJECTED;
    }
    state->next_expected++;
    return state->next_expected == event_count ? TRANSITION_COMPLETE
                                                : TRANSITION_ACCEPTED;
}

#ifndef XHCI_INPUT_GATE_SELF_TEST
static void report_keyboard_pass(void)
{
    printf("XHCI_INPUT_KEYBOARD_PASS events=8\n");
    fflush(stdout);
}

static _Noreturn void report_pointer_pass_and_hold(void)
{
    printf("XHCI_INPUT_POINTER_PASS events=7\n");
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
    bool mouse_like;
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

static bool has_one_exact_usb_mouse(const struct test_device *devices, size_t count)
{
    size_t mouse_count = 0;
    size_t matching_count = 0;
    for (size_t index = 0; index < count; index++) {
        if (!devices[index].mouse_like) {
            continue;
        }
        mouse_count++;
        if (devices[index].bustype == BUS_USB &&
            strcmp(devices[index].name, "usb_boot_mouse") == 0) {
            matching_count++;
        }
    }
    return mouse_count == 1 && matching_count == 1;
}

static bool exact_sequence_completes(const struct expected_event *expected_events,
                                     size_t count)
{
    struct input_state state = {0};
    for (size_t index = 0; index < count; index++) {
        const struct input_event event = {
            .type = expected_events[index].type,
            .code = expected_events[index].code,
            .value = expected_events[index].value,
        };
        const enum transition_result result =
            input_state_consume(&state, &event, expected_events, count);
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
            .type = EXPECTED_KEYBOARD_EVENTS[index].type,
            .code = EXPECTED_KEYBOARD_EVENTS[index].code,
            .value = EXPECTED_KEYBOARD_EVENTS[index].value,
        };
        if (input_state_consume(
                &state, &event, EXPECTED_KEYBOARD_EVENTS,
                sizeof(EXPECTED_KEYBOARD_EVENTS) / sizeof(EXPECTED_KEYBOARD_EVENTS[0])) ==
            TRANSITION_REJECTED) {
            return false;
        }
    }
    return state.next_expected != sizeof(EXPECTED_KEYBOARD_EVENTS) /
                                      sizeof(EXPECTED_KEYBOARD_EVENTS[0]);
}

static bool event_is_rejected(unsigned short type, unsigned short code, int value)
{
    struct input_state state = {0};
    const struct input_event event = {.type = type, .code = code, .value = value};
    return input_state_consume(
               &state, &event, EXPECTED_KEYBOARD_EVENTS,
               sizeof(EXPECTED_KEYBOARD_EVENTS) / sizeof(EXPECTED_KEYBOARD_EVENTS[0])) ==
           TRANSITION_REJECTED;
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
    const struct test_device usb_mouse = {
        .mouse_like = true,
        .bustype = BUS_USB,
        .name = "usb_boot_mouse",
    };
    const struct test_device virtio_mouse = {
        .mouse_like = true,
        .bustype = BUS_VIRTUAL,
        .name = "virtio_mouse",
    };
    if (strcmp(name, "valid") == 0) {
        return has_one_exact_usb_keyboard(&usb, 1) &&
               exact_sequence_completes(
                   EXPECTED_KEYBOARD_EVENTS,
                   sizeof(EXPECTED_KEYBOARD_EVENTS) /
                       sizeof(EXPECTED_KEYBOARD_EVENTS[0]));
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
    if (strcmp(name, "valid-mouse") == 0) {
        return has_one_exact_usb_mouse(&usb_mouse, 1);
    }
    if (strcmp(name, "zero-mice") == 0) {
        return !has_one_exact_usb_mouse(NULL, 0);
    }
    if (strcmp(name, "two-mice") == 0) {
        const struct test_device devices[] = {usb_mouse, virtio_mouse};
        return !has_one_exact_usb_mouse(devices, 2);
    }
    if (strcmp(name, "virtio-mouse") == 0) {
        return !has_one_exact_usb_mouse(&virtio_mouse, 1);
    }
    if (strcmp(name, "mouse-sequence") == 0) {
        return exact_sequence_completes(
            EXPECTED_MOUSE_EVENTS,
            sizeof(EXPECTED_MOUSE_EVENTS) / sizeof(EXPECTED_MOUSE_EVENTS[0]));
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
    struct input_state keyboard = {0};
    const size_t keyboard_count = sizeof(EXPECTED_KEYBOARD_EVENTS) /
                                  sizeof(EXPECTED_KEYBOARD_EVENTS[0]);
    for (size_t index = 0; index < keyboard_count; index++) {
        const struct input_event event = {
            .type = EXPECTED_KEYBOARD_EVENTS[index].type,
            .code = EXPECTED_KEYBOARD_EVENTS[index].code,
            .value = EXPECTED_KEYBOARD_EVENTS[index].value,
        };
        const enum transition_result result = input_state_consume(
            &keyboard, &event, EXPECTED_KEYBOARD_EVENTS, keyboard_count);
        if (result == TRANSITION_COMPLETE) {
            report_keyboard_pass();
        }
        if (result != TRANSITION_ACCEPTED) {
            if (result != TRANSITION_COMPLETE) {
                return 1;
            }
        }
    }
    struct input_state mouse = {0};
    const size_t mouse_count =
        sizeof(EXPECTED_MOUSE_EVENTS) / sizeof(EXPECTED_MOUSE_EVENTS[0]);
    for (size_t index = 0; index < mouse_count; index++) {
        const struct input_event event = {
            .type = EXPECTED_MOUSE_EVENTS[index].type,
            .code = EXPECTED_MOUSE_EVENTS[index].code,
            .value = EXPECTED_MOUSE_EVENTS[index].value,
        };
        const enum transition_result result =
            input_state_consume(&mouse, &event, EXPECTED_MOUSE_EVENTS, mouse_count);
        if (result == TRANSITION_COMPLETE) {
            report_pointer_pass_and_hold();
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
#define REL_BITMAP_SIZE                                                        \
    ((REL_MAX + 1 + BITS_PER_BYTE - 1) / BITS_PER_BYTE)

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

static bool looks_like_mouse(const unsigned char *keys,
                             const unsigned char *relative_axes)
{
    return key_is_advertised(keys, BTN_LEFT) &&
           (relative_axes[REL_X / BITS_PER_BYTE] &
            (1U << (REL_X % BITS_PER_BYTE))) != 0 &&
           (relative_axes[REL_Y / BITS_PER_BYTE] &
            (1U << (REL_Y % BITS_PER_BYTE))) != 0;
}

static enum keyboard_discovery discover_inputs(
    char *keyboard_path, char *mouse_path, size_t path_size,
    int *keyboard_fd_out, int *mouse_fd_out)
{
    int keyboard_fd = -1;
    int mouse_fd = -1;
    size_t keyboard_count = 0;
    size_t matching_keyboard_count = 0;
    size_t mouse_count = 0;
    size_t matching_mouse_count = 0;

    for (unsigned int index = 0; index < INPUT_EVENT_NODE_COUNT; index++) {
        char path[INPUT_NODE_PATH_CAPACITY];
        char name[DEVICE_NAME_CAPACITY] = {0};
        unsigned char keys[KEY_BITMAP_SIZE] = {0};
        unsigned char relative_axes[REL_BITMAP_SIZE] = {0};
        struct input_id id = {0};
        const int length = snprintf(path, sizeof(path), "/dev/input/event%u", index);
        if (length < 0 || (size_t)length >= sizeof(path)) {
            continue;
        }
        const int fd = retrying_open(path);
        if (fd < 0) {
            continue;
        }
        if (retrying_ioctl(fd, EVIOCGBIT(EV_KEY, sizeof(keys)), keys) < 0) {
            close(fd);
            continue;
        }
        const bool keyboard_like = looks_like_keyboard(keys);
        const bool has_relative_axes =
            retrying_ioctl(fd, EVIOCGBIT(EV_REL, sizeof(relative_axes)),
                           relative_axes) >= 0;
        const bool mouse_like =
            has_relative_axes && looks_like_mouse(keys, relative_axes);
        if (!keyboard_like && !mouse_like) {
            close(fd);
            continue;
        }
        const bool id_ok = retrying_ioctl(fd, EVIOCGID, &id) >= 0;
        const bool name_ok = retrying_ioctl(fd, EVIOCGNAME(sizeof(name)), name) >= 0;
        name[sizeof(name) - 1] = '\0';
        if (keyboard_like) {
            keyboard_count++;
            if (id_ok && name_ok && id.bustype == BUS_USB &&
                strcmp(name, "usb_boot_keyboard") == 0) {
                matching_keyboard_count++;
                if (keyboard_fd < 0 &&
                    snprintf(keyboard_path, path_size, "%s", path) >= 0) {
                    keyboard_fd = fd;
                    continue;
                }
            }
        }
        if (mouse_like) {
            mouse_count++;
            if (id_ok && name_ok && id.bustype == BUS_USB &&
                strcmp(name, "usb_boot_mouse") == 0) {
                matching_mouse_count++;
                if (mouse_fd < 0 && snprintf(mouse_path, path_size, "%s", path) >= 0) {
                    mouse_fd = fd;
                    continue;
                }
            }
        }
        close(fd);
    }

    enum keyboard_discovery result = KEYBOARD_DISCOVERY_PENDING;
    if (keyboard_count > 1 || mouse_count > 1 ||
        (keyboard_count == 1 && matching_keyboard_count != 1) ||
        (mouse_count == 1 && matching_mouse_count != 1)) {
        result = KEYBOARD_DISCOVERY_REJECTED;
    } else if (keyboard_count == 1 && matching_keyboard_count == 1 &&
               mouse_count == 1 && matching_mouse_count == 1 && keyboard_fd >= 0 &&
               mouse_fd >= 0) {
        result = KEYBOARD_DISCOVERY_READY;
    }
    if (result != KEYBOARD_DISCOVERY_READY) {
        if (keyboard_fd >= 0) {
            close(keyboard_fd);
        }
        if (mouse_fd >= 0) {
            close(mouse_fd);
        }
        return result;
    }
    *keyboard_fd_out = keyboard_fd;
    *mouse_fd_out = mouse_fd;
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

static bool wait_for_inputs(const struct timespec *deadline, char *keyboard_path,
                            char *mouse_path, size_t path_size,
                            int *keyboard_fd_out, int *mouse_fd_out)
{
    for (;;) {
        int keyboard_fd = -1;
        int mouse_fd = -1;
        const enum keyboard_discovery discovery =
            discover_inputs(keyboard_path, mouse_path, path_size, &keyboard_fd,
                            &mouse_fd);
        if (discovery == KEYBOARD_DISCOVERY_READY) {
            *keyboard_fd_out = keyboard_fd;
            *mouse_fd_out = mouse_fd;
            return true;
        }
        if (discovery == KEYBOARD_DISCOVERY_REJECTED) {
            fail("usb-input-selection");
            return false;
        }

        int timeout_ms;
        if (remaining_timeout_ms(deadline, &timeout_ms) < 0) {
            fail("clock-gettime");
            return false;
        }
        if (timeout_ms == 0) {
            fail("usb-input-timeout");
            return false;
        }
        if (timeout_ms > KEYBOARD_DISCOVERY_RETRY_MILLISECONDS) {
            timeout_ms = KEYBOARD_DISCOVERY_RETRY_MILLISECONDS;
        }
        const int poll_result = poll(NULL, 0, timeout_ms);
        if (poll_result < 0 && errno != EINTR) {
            fail("input-discovery-poll-error");
            return false;
        }
    }
}

static int wait_for_events(int keyboard_fd, int mouse_fd,
                           const struct timespec *deadline)
{
    struct input_state states[2] = {{0}, {0}};
    const struct expected_event *expected[2] = {
        EXPECTED_KEYBOARD_EVENTS,
        EXPECTED_MOUSE_EVENTS,
    };
    const size_t expected_counts[2] = {
        sizeof(EXPECTED_KEYBOARD_EVENTS) / sizeof(EXPECTED_KEYBOARD_EVENTS[0]),
        sizeof(EXPECTED_MOUSE_EVENTS) / sizeof(EXPECTED_MOUSE_EVENTS[0]),
    };
    const char *sources[2] = {"keyboard", "mouse"};
    bool completed[2] = {false, false};

    for (;;) {
        int timeout_ms;
        if (remaining_timeout_ms(deadline, &timeout_ms) < 0) {
            return fail("clock-gettime");
        }
        if (timeout_ms == 0) {
            return fail("input-timeout");
        }
        struct pollfd descriptors[2] = {
            {.fd = keyboard_fd, .events = POLLIN},
            {.fd = mouse_fd, .events = POLLIN},
        };
        const int poll_result = poll(descriptors, 2, timeout_ms);
        if (poll_result < 0) {
            if (errno == EINTR) {
                continue;
            }
            return fail("poll-error");
        }
        if (poll_result == 0) {
            return fail("input-timeout");
        }
        for (size_t device_index = 0; device_index < 2; device_index++) {
            if ((descriptors[device_index].revents &
                 (POLLERR | POLLHUP | POLLNVAL)) != 0) {
                return fail("input-device-poll-error");
            }
            if ((descriptors[device_index].revents & POLLIN) == 0) {
                continue;
            }

            struct input_event events[INPUT_EVENT_BATCH_SIZE];
            ssize_t bytes;
            do {
                bytes = read(descriptors[device_index].fd, events, sizeof(events));
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
                const enum transition_result result =
                    completed[device_index]
                        ? TRANSITION_REJECTED
                        : input_state_consume(
                              &states[device_index], &events[index],
                              expected[device_index], expected_counts[device_index]);
                if (result == TRANSITION_REJECTED) {
                    return fail(events[index].type == EV_SYN &&
                                        events[index].code == SYN_DROPPED
                                    ? "syn-dropped"
                                    : "invalid-event-sequence");
                }
                printf("XHCI_INPUT_EVENT source=%s type=%u code=%u value=%d\n",
                       sources[device_index], events[index].type, events[index].code,
                       events[index].value);
                fflush(stdout);
                if (result == TRANSITION_COMPLETE) {
                    completed[device_index] = true;
                    if (device_index == 0) {
                        report_keyboard_pass();
                    } else {
                        report_pointer_pass_and_hold();
                    }
                }
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
    char keyboard_path[INPUT_NODE_PATH_CAPACITY] = {0};
    char mouse_path[INPUT_NODE_PATH_CAPACITY] = {0};
    int keyboard_fd = -1;
    int mouse_fd = -1;
    if (!wait_for_inputs(&deadline, keyboard_path, mouse_path, sizeof(keyboard_path),
                         &keyboard_fd, &mouse_fd)) {
        return 1;
    }
    printf("XHCI_INPUT_READY kind=keyboard path=%s bustype=%u "
           "name=usb_boot_keyboard\n",
           keyboard_path, BUS_USB);
    printf("XHCI_INPUT_READY kind=mouse path=%s bustype=%u name=usb_boot_mouse\n",
           mouse_path, BUS_USB);
    fflush(stdout);
    const int result = wait_for_events(keyboard_fd, mouse_fd, &deadline);
    close(keyboard_fd);
    close(mouse_fd);
    return result;
}

#endif
