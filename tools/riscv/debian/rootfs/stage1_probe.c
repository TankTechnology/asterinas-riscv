// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/utsname.h>
#include <termios.h>
#include <unistd.h>

#include "stage1_probe.h"

enum {
    PROBE_REQUEST_MAX = 512,
    PROBE_COUNT_MAX = 5,
    PROBE_NONCE_LENGTH = 32,
    PROBE_DMESG_MAX = 32 * 1024,
    PROBE_FILE_MAX = 4096,
    ASTERINAS_SYSLOG = 116,
    ASTERINAS_READAHEAD = 213,
    ASTERINAS_KCMP = 272,
    SYSLOG_READ_ALL = 3,
    SYSLOG_SIZE_BUFFER = 10,
};

static const char *const PROBE_NAMES[] = {
    "boot",
    "syscall213",
    "syscall272",
    "ext2-writeback",
    "systemd-compat",
};

struct ProbeRequest {
    char nonce[PROBE_NONCE_LENGTH + 1];
    char *names[PROBE_COUNT_MAX];
    size_t count;
    int shell;
};

struct ProbeResult {
    int passed;
    int error_number;
    const char *detail;
};

static void flush_line(void)
{
    (void)fflush(stdout);
}

static int disable_input_echo(void)
{
    struct termios attributes;
    if (tcgetattr(STDIN_FILENO, &attributes) != 0) {
        return errno == ENOTTY ? 0 : -1;
    }
    attributes.c_lflag &= (tcflag_t) ~(ECHO | ECHONL);
    while (tcsetattr(STDIN_FILENO, TCSANOW, &attributes) != 0) {
        if (errno != EINTR) {
            return -1;
        }
    }
    return 0;
}

static int is_probe_name(const char *name)
{
    for (size_t index = 0;
         index < sizeof(PROBE_NAMES) / sizeof(PROBE_NAMES[0]); ++index) {
        if (strcmp(name, PROBE_NAMES[index]) == 0) {
            return 1;
        }
    }
    return 0;
}

static int valid_nonce(const char *nonce)
{
    if (strlen(nonce) != PROBE_NONCE_LENGTH) {
        return 0;
    }
    for (size_t index = 0; index < PROBE_NONCE_LENGTH; ++index) {
        const char character = nonce[index];
        if (!((character >= '0' && character <= '9') ||
              (character >= 'a' && character <= 'f'))) {
            return 0;
        }
    }
    return 1;
}

static int read_protocol_line(char line[PROBE_REQUEST_MAX + 2])
{
    if (fgets(line, PROBE_REQUEST_MAX + 2, stdin) == NULL) {
        return -1;
    }
    const size_t length = strlen(line);
    if (length == 0 || length > PROBE_REQUEST_MAX || line[length - 1] != '\n') {
        return -1;
    }
    line[length - 1] = '\0';
    if (length >= 2 && line[length - 2] == '\r') {
        line[length - 2] = '\0';
    }
    return 0;
}

static int split_request_fields(char *line, char *fields[5])
{
    if (line[0] == '\0' || line[0] == ' ' || strstr(line, "  ") != NULL) {
        return -1;
    }
    size_t count = 0;
    char *cursor = line;
    while (count < 5) {
        fields[count++] = cursor;
        char *space = strchr(cursor, ' ');
        if (space == NULL) {
            break;
        }
        if (count == 5) {
            return -1;
        }
        *space = '\0';
        cursor = space + 1;
    }
    return count == 5 && strchr(fields[4], ' ') == NULL ? 0 : -1;
}

static int parse_probe_names(char *value, struct ProbeRequest *request)
{
    if (value[0] == '\0' || value[0] == ',' ||
        value[strlen(value) - 1] == ',' || strstr(value, ",,") != NULL) {
        return -1;
    }
    char *save = NULL;
    for (char *name = strtok_r(value, ",", &save); name != NULL;
         name = strtok_r(NULL, ",", &save)) {
        if (request->count == PROBE_COUNT_MAX || !is_probe_name(name)) {
            return -1;
        }
        for (size_t index = 0; index < request->count; ++index) {
            if (strcmp(request->names[index], name) == 0) {
                return -1;
            }
        }
        request->names[request->count++] = name;
    }
    return request->count == 0 ? -1 : 0;
}

static int parse_request(char *line, struct ProbeRequest *request)
{
    char *fields[5];
    memset(request, 0, sizeof(*request));
    if (split_request_fields(line, fields) != 0 ||
        strcmp(fields[0], "ASTERINAS_PROBE_RUN") != 0 ||
        strcmp(fields[1], "v=1") != 0 ||
        strncmp(fields[2], "nonce=", sizeof("nonce=") - 1) != 0 ||
        strncmp(fields[3], "probes=", sizeof("probes=") - 1) != 0 ||
        strncmp(fields[4], "shell=", sizeof("shell=") - 1) != 0) {
        return -1;
    }
    const char *nonce = fields[2] + sizeof("nonce=") - 1;
    if (!valid_nonce(nonce)) {
        return -1;
    }
    memcpy(request->nonce, nonce, PROBE_NONCE_LENGTH + 1);
    if (strcmp(fields[4], "shell=0") == 0) {
        request->shell = 0;
    } else if (strcmp(fields[4], "shell=1") == 0) {
        request->shell = 1;
    } else {
        return -1;
    }
    return parse_probe_names(fields[3] + sizeof("probes=") - 1, request);
}

#if !defined(DEBIAN_STAGE1_PROBE_SELF_TEST)

static int read_small_file(const char *path, char buffer[PROBE_FILE_MAX],
                           size_t *length)
{
    int fd;
    do {
        fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    } while (fd < 0 && errno == EINTR);
    if (fd < 0) {
        return -1;
    }
    ssize_t result;
    do {
        result = read(fd, buffer, PROBE_FILE_MAX - 1);
    } while (result < 0 && errno == EINTR);
    const int saved_errno = errno;
    (void)close(fd);
    errno = saved_errno;
    if (result <= 0) {
        return -1;
    }
    buffer[result] = '\0';
    *length = (size_t)result;
    return 0;
}

static int cmdline_has_parameter(const char *cmdline, const char *parameter)
{
    const size_t parameter_length = strlen(parameter);
    const char *cursor = cmdline;
    while (*cursor != '\0') {
        while (*cursor == ' ' || *cursor == '\n') {
            ++cursor;
        }
        size_t length = 0;
        while (cursor[length] != '\0' && cursor[length] != ' ' &&
               cursor[length] != '\n' && cursor[length] != '=') {
            const char actual = cursor[length] == '-' ? '_' : cursor[length];
            if (length >= parameter_length || actual != parameter[length]) {
                break;
            }
            ++length;
        }
        if (length == parameter_length &&
            (cursor[length] == '\0' || cursor[length] == ' ' ||
             cursor[length] == '\n' || cursor[length] == '=')) {
            return 1;
        }
        while (*cursor != '\0' && *cursor != ' ' && *cursor != '\n') {
            ++cursor;
        }
    }
    return 0;
}

static struct ProbeResult probe_boot(void)
{
    struct utsname name;
    if (getpid() != 1) {
        return (struct ProbeResult){ 0, EINVAL, "pid-not-one" };
    }
    if (uname(&name) != 0) {
        return (struct ProbeResult){ 0, errno, "uname-failed" };
    }
    if (fcntl(STDIN_FILENO, F_GETFL) < 0) {
        return (struct ProbeResult){ 0, errno, "console-failed" };
    }
    return (struct ProbeResult){ 1, 0, "boot-ok" };
}

static struct ProbeResult probe_unimplemented(long number)
{
    errno = 0;
    long result;
    if (number == ASTERINAS_READAHEAD) {
        result = syscall(number, -1, 0, 0);
    } else {
        result = syscall(number, getpid(), getpid(), 0, 0, 0);
    }
    if (result == -1 && errno == ENOSYS) {
        return (struct ProbeResult){ 1, 0, "enosys" };
    }
    return (struct ProbeResult){ 0, errno, "unexpected-syscall-result" };
}

static struct ProbeResult probe_ext2_writeback(void)
{
    char cmdline[PROBE_FILE_MAX];
    size_t length;
    if (read_small_file("/proc/cmdline", cmdline, &length) != 0) {
        return (struct ProbeResult){ 0, errno, "cmdline-unavailable" };
    }
    (void)length;
    if (cmdline_has_parameter(cmdline, "asterinas.mmc_write_partition2")) {
        return (struct ProbeResult){ 0, EPERM, "write-gate-open" };
    }
    return (struct ProbeResult){ 1, 0, "write-gate-closed" };
}

static struct ProbeResult probe_systemd_compat(void)
{
    char contents[PROBE_FILE_MAX];
    size_t length;
    if (read_small_file("/proc/sys/kernel/random/boot_id", contents, &length) !=
            0 ||
        length < 32) {
        return (struct ProbeResult){ 0, errno, "random-interface-missing" };
    }

    int uuid_fd;
    do {
        uuid_fd = open("/proc/sys/kernel/random/uuid",
                       O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    } while (uuid_fd < 0 && errno == EINTR);
    if (uuid_fd < 0) {
        return (struct ProbeResult){ 0, errno, "random-interface-missing" };
    }

    char first_uuid[64];
    char second_uuid[64];
    char seek_uuid[64];
    ssize_t first_length;
    ssize_t second_length;
    ssize_t seek_length;
    do {
        first_length = pread(uuid_fd, first_uuid, sizeof(first_uuid), 0);
    } while (first_length < 0 && errno == EINTR);
    if (first_length < 0) {
        const int error_number = errno;
        (void)close(uuid_fd);
        return (struct ProbeResult){ 0, error_number,
                                     "random-interface-missing" };
    }
    do {
        second_length = pread(uuid_fd, second_uuid, sizeof(second_uuid), 0);
    } while (second_length < 0 && errno == EINTR);
    if (second_length < 0) {
        const int error_number = errno;
        (void)close(uuid_fd);
        return (struct ProbeResult){ 0, error_number,
                                     "random-interface-missing" };
    }
    off_t seek_result;
    do {
        seek_result = lseek(uuid_fd, 0, SEEK_SET);
    } while (seek_result < 0 && errno == EINTR);
    if (seek_result < 0) {
        const int error_number = errno;
        (void)close(uuid_fd);
        return (struct ProbeResult){ 0, error_number,
                                     "random-interface-missing" };
    }
    do {
        seek_length = read(uuid_fd, seek_uuid, sizeof(seek_uuid));
    } while (seek_length < 0 && errno == EINTR);
    if (seek_length < 0) {
        const int error_number = errno;
        (void)close(uuid_fd);
        return (struct ProbeResult){ 0, error_number,
                                     "random-interface-missing" };
    }
    if (close(uuid_fd) != 0) {
        return (struct ProbeResult){ 0, errno, "random-interface-missing" };
    }
    if (first_length != 37 || second_length != 37 || seek_length != 37) {
        return (struct ProbeResult){ 0, EIO, "random-interface-malformed" };
    }
    if (memcmp(first_uuid, second_uuid, (size_t)first_length) == 0 ||
        memcmp(first_uuid, seek_uuid, (size_t)first_length) == 0) {
        return (struct ProbeResult){ 0, EIO, "random-interface-stale" };
    }
    errno = 0;
    if (syscall(ASTERINAS_SYSLOG, SYSLOG_SIZE_BUFFER, 0, 0) <= 0) {
        return (struct ProbeResult){ 0, errno, "syslog-interface-missing" };
    }
    return (struct ProbeResult){ 1, 0, "interfaces-ready" };
}

static struct ProbeResult execute_probe(const char *name)
{
    if (strcmp(name, "boot") == 0) {
        return probe_boot();
    }
    if (strcmp(name, "syscall213") == 0) {
        return probe_unimplemented(ASTERINAS_READAHEAD);
    }
    if (strcmp(name, "syscall272") == 0) {
        return probe_unimplemented(ASTERINAS_KCMP);
    }
    if (strcmp(name, "ext2-writeback") == 0) {
        return probe_ext2_writeback();
    }
    return probe_systemd_compat();
}

#else

static struct ProbeResult execute_probe(const char *name)
{
    if (strcmp(name, "boot") == 0) {
#if defined(DEBIAN_STAGE1_PROBE_SELF_TEST_FAIL_BOOT)
        return (struct ProbeResult){ 0, EIO, "uname-failed" };
#else
        return (struct ProbeResult){ 1, 0, "boot-ok" };
#endif
    }
    if (strcmp(name, "syscall213") == 0 ||
        strcmp(name, "syscall272") == 0) {
        return (struct ProbeResult){ 1, 0, "enosys" };
    }
    if (strcmp(name, "ext2-writeback") == 0) {
        return (struct ProbeResult){ 1, 0, "write-gate-closed" };
    }
    return (struct ProbeResult){ 1, 0, "interfaces-ready" };
}

#endif

static void emit_dmesg(const char *nonce)
{
#if defined(DEBIAN_STAGE1_PROBE_SELF_TEST)
    const char *const contents = "self-test dmesg\n";
    const long length = (long)strlen(contents);
#else
    static char contents[PROBE_DMESG_MAX + 1];
    errno = 0;
    long length = syscall(ASTERINAS_SYSLOG, SYSLOG_READ_ALL, contents,
                          PROBE_DMESG_MAX);
    if (length < 0) {
        length = 0;
    }
#endif
    (void)printf("ASTERINAS_PROBE_DMESG_BEGIN v=1 nonce=%s bytes=%ld\n",
                 nonce, length);
    if (length > 0) {
        (void)fwrite(contents, 1, (size_t)length, stdout);
        if (contents[length - 1] != '\n') {
            (void)putchar('\n');
        }
    }
    (void)printf("ASTERINAS_PROBE_DMESG_END v=1 nonce=%s\n", nonce);
    flush_line();
}

static int run_batch(const struct ProbeRequest *request)
{
    int passed = 1;
    size_t attempted = 0;
    for (size_t index = 0; index < request->count; ++index) {
        const char *name = request->names[index];
        (void)printf(
            "ASTERINAS_PROBE_START v=1 nonce=%s seq=%zu name=%s\n",
            request->nonce, index, name);
        flush_line();
        const struct ProbeResult result = execute_probe(name);
        attempted = index + 1;
        if (result.passed) {
            (void)printf(
                "ASTERINAS_PROBE_PASS v=1 nonce=%s seq=%zu name=%s detail=%s\n",
                request->nonce, index, name, result.detail);
        } else {
            passed = 0;
            (void)printf(
                "ASTERINAS_PROBE_FAIL v=1 nonce=%s seq=%zu name=%s errno=%d detail=%s\n",
                request->nonce, index, name, result.error_number,
                result.detail);
        }
        flush_line();
        if (!result.passed) {
            break;
        }
    }
    if (!passed) {
        emit_dmesg(request->nonce);
    }
    (void)printf(
        "ASTERINAS_PROBE_DONE v=1 nonce=%s count=%zu status=%s\n",
        request->nonce, attempted, passed ? "pass" : "fail");
    flush_line();
    return passed;
}

static void print_shell_help(void)
{
    (void)printf(
        "ASTERINAS_PROBE_SHELL_COMMANDS help,dmesg,mounts,boot,syscall213,syscall272,ext2-writeback,systemd-compat,exit\n");
    flush_line();
}

static void print_mounts(void)
{
#if defined(DEBIAN_STAGE1_PROBE_SELF_TEST)
    (void)printf("ASTERINAS_PROBE_SHELL_MOUNTS proc /proc proc\n");
#else
    char contents[PROBE_FILE_MAX];
    size_t length;
    if (read_small_file("/proc/mounts", contents, &length) == 0) {
        (void)printf("ASTERINAS_PROBE_SHELL_MOUNTS_BEGIN\n");
        (void)fwrite(contents, 1, length, stdout);
        if (contents[length - 1] != '\n') {
            (void)putchar('\n');
        }
        (void)printf("ASTERINAS_PROBE_SHELL_MOUNTS_END\n");
    } else {
        (void)printf("ASTERINAS_PROBE_SHELL_REJECT reason=mounts-unavailable\n");
    }
#endif
    flush_line();
}

static void run_shell(const struct ProbeRequest *request)
{
    char line[PROBE_REQUEST_MAX + 2];
    (void)printf("ASTERINAS_PROBE_SHELL_READY v=1 nonce=%s\n",
                 request->nonce);
    flush_line();
    for (;;) {
        if (read_protocol_line(line) != 0) {
            return;
        }
        if (strcmp(line, "exit") == 0) {
            return;
        }
        if (strcmp(line, "help") == 0) {
            print_shell_help();
        } else if (strcmp(line, "dmesg") == 0) {
            emit_dmesg(request->nonce);
        } else if (strcmp(line, "mounts") == 0) {
            print_mounts();
        } else if (is_probe_name(line)) {
            const struct ProbeResult result = execute_probe(line);
            (void)printf(
                "ASTERINAS_PROBE_SHELL_RESULT name=%s status=%s errno=%d detail=%s\n",
                line, result.passed ? "pass" : "fail", result.error_number,
                result.detail);
            flush_line();
        } else {
            (void)printf(
                "ASTERINAS_PROBE_SHELL_REJECT reason=unknown-command\n");
            flush_line();
        }
    }
}

static int prepare_proc(void)
{
#if defined(DEBIAN_STAGE1_PROBE_SELF_TEST)
    return 0;
#else
    if (mkdir("/proc", 0555) != 0 && errno != EEXIST) {
        return -1;
    }
    if (mount("proc", "/proc", "proc", 0, NULL) != 0 && errno != EBUSY) {
        return -1;
    }
    return 0;
#endif
}

static void request_reboot(const char *nonce)
{
#if defined(DEBIAN_STAGE1_PROBE_SELF_TEST)
    (void)nonce;
#else
    char line[PROBE_REQUEST_MAX + 2];
    char expected[96];
    (void)snprintf(expected, sizeof(expected),
                   "ASTERINAS_PROBE_REBOOT v=1 nonce=%s", nonce);
    if (read_protocol_line(line) != 0 || strcmp(line, expected) != 0) {
        (void)printf(
            "ASTERINAS_PROBE_REBOOT_REJECT v=1 reason=invalid-request\n");
        flush_line();
        for (;;) {
            (void)pause();
        }
    }
    sync();
    (void)reboot(RB_AUTOBOOT);
    for (;;) {
        (void)pause();
    }
#endif
}

int stage1_run_probe_agent(void)
{
    char line[PROBE_REQUEST_MAX + 2];
    struct ProbeRequest request;
    if (disable_input_echo() != 0) {
        (void)printf("DEBIAN_STAGE1_FAIL reason=probe-console-echo\n");
        flush_line();
        return 2;
    }
    (void)printf("ASTERINAS_PROBE_READY v=1 pid=1\n");
    flush_line();
    if (prepare_proc() != 0 || read_protocol_line(line) != 0 ||
        parse_request(line, &request) != 0) {
        (void)printf(
            "ASTERINAS_PROBE_REQUEST_FAIL v=1 reason=invalid-request\n");
        flush_line();
        return 2;
    }
    (void)run_batch(&request);
    if (request.shell) {
        run_shell(&request);
    }
    (void)printf("ASTERINAS_PROBE_REBOOT_READY v=1 nonce=%s\n",
                 request.nonce);
    flush_line();
    request_reboot(request.nonce);
    return 0;
}

#if defined(DEBIAN_STAGE1_PROBE_SELF_TEST)
int main(void)
{
    return stage1_run_probe_agent();
}
#endif
