// SPDX-License-Identifier: MPL-2.0
//
// Checks the three things `lsblk`, `blkid` and `iostat` need, the way those
// tools use them: enumerate `/sys/dev/block`, resolve each entry to a device
// directory, read the attributes from there, and parse `/proc/diskstats`.
//
// Run as init in QEMU (or on the board) and compare the output with a Linux
// host of the same kernel version. Exit status 0 means every check passed.
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

static int failures;

static void check(int condition, const char *what, const char *detail)
{
    if (condition) {
        printf("ok   %s%s%s\n", what, detail ? ": " : "", detail ? detail : "");
    } else {
        printf("FAIL %s%s%s\n", what, detail ? ": " : "", detail ? detail : "");
        failures++;
    }
}

// Reads the first line of a file, without the newline.
static int read_first_line(const char *path, char *buffer, size_t size)
{
    int fd = open(path, O_RDONLY);
    if (fd < 0)
        return -1;
    ssize_t n = read(fd, buffer, size - 1);
    close(fd);
    if (n <= 0)
        return -1;
    buffer[n] = '\0';
    char *newline = strchr(buffer, '\n');
    if (newline)
        *newline = '\0';
    return 0;
}

static int count_entries(const char *dir)
{
    DIR *handle = opendir(dir);
    if (!handle)
        return -1;
    int count = 0;
    struct dirent *entry;
    while ((entry = readdir(handle)) != NULL) {
        if (entry->d_name[0] == '.' && (entry->d_name[1] == '\0' ||
                                        (entry->d_name[1] == '.' && entry->d_name[2] == '\0')))
            continue;
        count++;
    }
    closedir(handle);
    return count;
}

int main(void)
{
    char line[512];

    mkdir("/tmp", 0777);
    mkdir("/proc", 0555);
    mkdir("/sys", 0555);
    mount("proc", "/proc", "proc", 0, "");
    mount("sysfs", "/sys", "sysfs", 0, "");

    // 1. The directories lsblk/blkid insist on before they do anything.
    int class_block = count_entries("/sys/class/block");
    check(class_block > 0, "/sys/class/block lists devices", NULL);
    int dev_block = count_entries("/sys/dev/block");
    check(dev_block > 0, "/sys/dev/block lists devices", NULL);
    check(class_block == dev_block, "class and dev listings agree", NULL);

    // 2. Every /sys/dev/block entry resolves to a directory with a `dev` file,
    //    which is what lsblk reads to learn the device numbers.
    DIR *handle = opendir("/sys/dev/block");
    int resolved = 0, broken = 0;
    if (handle) {
        struct dirent *entry;
        while ((entry = readdir(handle)) != NULL) {
            if (entry->d_name[0] == '.')
                continue;
            char path[512], target[512];
            snprintf(path, sizeof(path), "/sys/dev/block/%s", entry->d_name);
            ssize_t n = readlink(path, target, sizeof(target) - 1);
            if (n <= 0) {
                broken++;
                if (broken <= 3)
                    printf("     unreadable link: %s\n", path);
                continue;
            }
            target[n] = '\0';
            char dev_path[512];
            snprintf(dev_path, sizeof(dev_path), "%s/dev", path);
            if (read_first_line(dev_path, line, sizeof(line)) == 0) {
                resolved++;
            } else {
                broken++;
                if (broken <= 3)
                    printf("     unreadable dev file: %s -> %s (errno %d: %s)\n",
                           path, target, errno, strerror(errno));
            }
        }
        closedir(handle);
    }
    check(broken == 0, "/sys/dev/block entries resolve through the symlink", NULL);
    check(resolved == dev_block, "every dev/block entry has a readable dev file", NULL);

    // 3. /sys/class/block shares the same device directories.
    char class_dev[512];
    int class_ok = 0, class_total = 0;
    handle = opendir("/sys/class/block");
    if (handle) {
        struct dirent *entry;
        while ((entry = readdir(handle)) != NULL) {
            if (entry->d_name[0] == '.')
                continue;
            class_total++;
            snprintf(class_dev, sizeof(class_dev), "/sys/class/block/%s/size", entry->d_name);
            if (read_first_line(class_dev, line, sizeof(line)) == 0)
                class_ok++;
        }
        closedir(handle);
    }
    check(class_ok == class_total && class_total > 0,
          "every class/block entry exposes attributes", NULL);

    // 4. /proc/diskstats: one line per device, at least fourteen fields.
    FILE *stats = fopen("/proc/diskstats", "r");
    int lines = 0, wide_enough = 0;
    if (stats) {
        while (fgets(line, sizeof(line), stats)) {
            lines++;
            int fields = 0;
            for (char *cursor = line; *cursor; cursor++)
                if (*cursor == ' ')
                    fields++;
            if (fields >= 13) // 12 separators between 14 fields, then leading
                wide_enough++;
        }
        fclose(stats);
    }
    check(lines > 0, "/proc/diskstats has lines", NULL);
    check(wide_enough == lines, "every diskstats line has fourteen fields", NULL);
    check(lines == class_block + 0 || lines >= class_block,
          "diskstats covers the devices sysfs lists", NULL);

    // Targeted: is it the symlink traversal or the name?
    const char *targets[] = {
        "/sys/class/block/loop0/size",
        "/sys/class/block/loop0/dev",
        "/sys/dev/block/252:0/dev",
        "/sys/dev/block/252:0/size",
        "/sys/block/loop0/dev",
    };
    for (unsigned i = 0; i < sizeof(targets) / sizeof(targets[0]); i++) {
        int fd = open(targets[i], O_RDONLY);
        printf("     %-34s %s\n", targets[i], fd >= 0 ? "open ok" : strerror(errno));
        if (fd >= 0)
            close(fd);
    }

    FILE *dump = fopen("/proc/diskstats", "r");
    if (dump) {
        printf("     --- /proc/diskstats ---\n");
        int shown = 0;
        while (fgets(line, sizeof(line), dump) && shown < 4) {
            printf("     %s", line);
            shown++;
        }
        fclose(dump);
    }
    char stat_line[256];
    if (read_first_line("/sys/block/vda/stat", stat_line, sizeof(stat_line)) == 0)
        printf("     /sys/block/vda/stat: %s\n", stat_line);

    printf("DEVPROBE %s (%d failure(s))\n", failures ? "FAILED" : "OK", failures);
    return failures ? 1 : 0;
}
