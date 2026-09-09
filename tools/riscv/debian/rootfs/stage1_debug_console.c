/* SPDX-License-Identifier: MPL-2.0 */

#define _POSIX_C_SOURCE 200809L

#include "stage1_debug_console.h"

#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define STAGE1_PATH_SIZE 4096

static const char DEBUG_CONSOLE_SERVICE[] =
    "[Unit]\n"
    "Description=Asterinas opt-in root serial console\n"
    "DefaultDependencies=no\n"
    "After=systemd-user-sessions.service\n"
    "ConditionPathExists=/run/asterinas-debug-console.enabled\n"
    "\n"
    "[Service]\n"
    "Type=simple\n"
    "ExecStart=/bin/bash --noprofile --rcfile "
    "/run/asterinas-debug-console.bashrc -i\n"
    "TTYPath=/dev/ttyS0\n"
    "StandardInput=tty-force\n"
    "StandardOutput=tty\n"
    "StandardError=tty\n"
    "TTYReset=yes\n"
    "TTYVHangup=yes\n"
    "Restart=always\n"
    "RestartSec=1\n";

static const char DEBUG_CONSOLE_TARGET[] =
    "[Unit]\n"
    "Description=Asterinas opt-in root serial console target\n"
    "DefaultDependencies=no\n"
    "Wants=asterinas-debug-console.service\n"
    "After=asterinas-debug-console.service\n";

static const char DEBUG_CONSOLE_BASHRC[] =
    "printf 'ASTERINAS_DEBUG_CONSOLE_READY uid=%s\\n' \"$(id -u)\"\n"
    "bind 'set enable-bracketed-paste off' 2>/dev/null\n"
    "PS1='root@asterinas-debug:\\w# '\n";

static const char CONSOLE_GETTY_DROP_IN[] =
    "[Unit]\n"
    "ConditionPathExists=!/run/asterinas-debug-console.enabled\n";

static int make_path(char path[STAGE1_PATH_SIZE], const char *root,
                     const char *suffix)
{
    if (root == NULL || root[0] == '\0') {
        return -1;
    }
    int length = snprintf(path, STAGE1_PATH_SIZE, "%s%s", root, suffix);
    return length >= 0 && (size_t)length < STAGE1_PATH_SIZE ? 0 : -1;
}

static int destination_absent(const char *path)
{
    struct stat metadata;
    if (lstat(path, &metadata) == 0) {
        return -1;
    }
    return errno == ENOENT ? 0 : -1;
}

static int ensure_directory(const char *path)
{
    if (mkdir(path, 0755) != 0 && errno != EEXIST) {
        return -1;
    }

    struct stat metadata;
    return lstat(path, &metadata) == 0 && S_ISDIR(metadata.st_mode) ? 0 : -1;
}

static int write_all(int descriptor, const char *content, size_t length)
{
    size_t offset = 0;
    while (offset < length) {
        ssize_t written = write(descriptor, content + offset, length - offset);
        if (written < 0 && errno == EINTR) {
            continue;
        }
        if (written <= 0) {
            return -1;
        }
        offset += (size_t)written;
    }
    return 0;
}

static int create_file(const char *path, const char *content, size_t length)
{
    int descriptor;
    do {
        descriptor = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW |
                                    O_CLOEXEC,
                          0644);
    } while (descriptor < 0 && errno == EINTR);
    if (descriptor < 0) {
        return -1;
    }

    int result = write_all(descriptor, content, length);
    if (close(descriptor) != 0) {
        result = -1;
    }
    return result;
}

int stage1_prepare_debug_console(const char *root)
{
    static const char *const directory_suffixes[] = {
        "/run",
        "/run/systemd",
        "/run/systemd/system",
        "/run/systemd/system/console-getty.service.d",
        "/run/systemd/system/getty.target.wants",
    };
    static const char *const destination_suffixes[] = {
        "/run/asterinas-debug-console.enabled",
        "/run/asterinas-debug-console.bashrc",
        "/run/systemd/system/asterinas-debug-console.service",
        "/run/systemd/system/asterinas-debug-console.target",
        "/run/systemd/system/console-getty.service.d/"
        "asterinas-debug-console.conf",
        "/run/systemd/system/getty.target.wants/"
        "asterinas-debug-console.service",
    };
    char paths[sizeof(destination_suffixes) / sizeof(destination_suffixes[0])]
              [STAGE1_PATH_SIZE];

    for (size_t index = 0;
         index < sizeof(destination_suffixes) / sizeof(destination_suffixes[0]);
         ++index) {
        if (make_path(paths[index], root, destination_suffixes[index]) != 0 ||
            destination_absent(paths[index]) != 0) {
            return -1;
        }
    }

    for (size_t index = 0;
         index < sizeof(directory_suffixes) / sizeof(directory_suffixes[0]);
         ++index) {
        char path[STAGE1_PATH_SIZE];
        if (make_path(path, root, directory_suffixes[index]) != 0 ||
            ensure_directory(path) != 0) {
            return -1;
        }
    }

    if (create_file(paths[0], "", 0) != 0 ||
        create_file(paths[1], DEBUG_CONSOLE_BASHRC,
                    sizeof(DEBUG_CONSOLE_BASHRC) - 1) != 0 ||
        create_file(paths[2], DEBUG_CONSOLE_SERVICE,
                    sizeof(DEBUG_CONSOLE_SERVICE) - 1) != 0 ||
        create_file(paths[3], DEBUG_CONSOLE_TARGET,
                    sizeof(DEBUG_CONSOLE_TARGET) - 1) != 0 ||
        create_file(paths[4], CONSOLE_GETTY_DROP_IN,
                    sizeof(CONSOLE_GETTY_DROP_IN) - 1) != 0 ||
        symlink("../asterinas-debug-console.service", paths[5]) != 0) {
        return -1;
    }
    return 0;
}
