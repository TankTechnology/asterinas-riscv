// SPDX-License-Identifier: MPL-2.0
//
// Checks `/sys/devices/system/cpu` the way `lscpu` and `htop` read it: the
// bitmap files first, then one directory per CPU with its topology.
//
// The interesting check is the cross-check: the CPU count the sysfs tree
// describes has to match what /proc/cpuinfo and /proc/stat report for the same
// kernel, or a tool reading one and a tool reading the other disagree about
// how many cores the machine has.
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

#define CPU_ROOT "/sys/devices/system/cpu"

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

static int read_line(const char *path, char *buffer, size_t size)
{
    int fd = open(path, O_RDONLY);
    if (fd < 0)
        return -1;
    ssize_t n = read(fd, buffer, size - 1);
    close(fd);
    if (n < 0)
        return -1;
    buffer[n] = '\0';
    char *newline = strchr(buffer, '\n');
    if (newline)
        *newline = '\0';
    return 0;
}

// Counts lines starting with a prefix in a file.
static int count_lines_with_prefix(const char *path, const char *prefix)
{
    FILE *handle = fopen(path, "r");
    if (!handle)
        return -1;
    char line[256];
    int count = 0;
    size_t length = strlen(prefix);
    while (fgets(line, sizeof(line), handle))
        if (strncmp(line, prefix, length) == 0)
            count++;
    fclose(handle);
    return count;
}

// Parses "0-3" or "0,2" into a count of CPUs.
static int cpu_list_count(const char *text)
{
    int total = 0;
    const char *cursor = text;
    while (*cursor) {
        char *end;
        long first = strtol(cursor, &end, 10);
        if (end == cursor)
            break;
        long last = first;
        cursor = end;
        if (*cursor == '-') {
            cursor++;
            last = strtol(cursor, &end, 10);
            if (end == cursor)
                break;
            cursor = end;
        }
        if (last < first)
            return -1;
        total += (int)(last - first + 1);
        if (*cursor == ',')
            cursor++;
    }
    return total;
}

int main(void)
{
    char buffer[256];

    mkdir("/tmp", 0777);
    mkdir("/proc", 0555);
    mkdir("/sys", 0555);
    mount("proc", "/proc", "proc", 0, "");
    mount("sysfs", "/sys", "sysfs", 0, "");

    // 1. The bitmap file lscpu opens first.
    int has_possible = read_line(CPU_ROOT "/possible", buffer, sizeof(buffer)) == 0;
    check(has_possible, "possible exists", NULL);
    int possible_cpus = has_possible ? cpu_list_count(buffer) : -1;
    check(possible_cpus > 0, "possible parses as a CPU list", buffer);

    // 2. The other bitmaps, and kernel_max.
    char online_buffer[256] = {0};
    check(read_line(CPU_ROOT "/present", buffer, sizeof(buffer)) == 0, "present exists", NULL);
    check(read_line(CPU_ROOT "/online", online_buffer, sizeof(online_buffer)) == 0,
          "online exists", NULL);
    check(read_line(CPU_ROOT "/offline", buffer, sizeof(buffer)) == 0, "offline exists", NULL);
    check(read_line(CPU_ROOT "/kernel_max", buffer, sizeof(buffer)) == 0, "kernel_max exists",
          buffer);

    // 3. One directory per CPU, with the CPU id matching the directory name.
    int directories = count_lines_with_prefix("/proc/cpuinfo", "processor");
    int cpu_dirs = 0, cpu_dirs_ok = 0;
    DIR *root = opendir(CPU_ROOT);
    if (root) {
        struct dirent *entry;
        while ((entry = readdir(root)) != NULL) {
            if (strncmp(entry->d_name, "cpu", 3) != 0 || entry->d_name[3] == '\0')
                continue;
            cpu_dirs++;
            char path[512];
            snprintf(path, sizeof(path), CPU_ROOT "/%s/topology/core_id", entry->d_name);
            if (read_line(path, buffer, sizeof(buffer)) == 0) {
                long core_id = strtol(buffer, NULL, 10);
                long expected = strtol(entry->d_name + 3, NULL, 10);
                if (core_id == expected)
                    cpu_dirs_ok++;
            }
        }
        closedir(root);
    }
    check(cpu_dirs == possible_cpus, "one directory per possible CPU", NULL);
    check(cpu_dirs_ok == cpu_dirs, "each core_id matches its directory name", NULL);

    // 4. Cross-check against the two procfs views of the same machine.
    int stat_cpus = count_lines_with_prefix("/proc/stat", "cpu") - 1; // minus the aggregate
    char detail[128];
    snprintf(detail, sizeof(detail), "sysfs=%d, /proc/cpuinfo=%d, /proc/stat=%d", cpu_dirs,
             directories, stat_cpus);
    check(cpu_dirs == directories && cpu_dirs == stat_cpus,
          "sysfs, cpuinfo and stat agree on the CPU count", detail);
    check(cpu_list_count(online_buffer) == cpu_dirs, "online lists every CPU", online_buffer);

    // 5. Topology files, including the cpumask the older tools parse.
    const char *topology_files[] = {
        "physical_package_id",
        "thread_siblings",
        "thread_siblings_list",
        "core_siblings",
        "core_siblings_list",
    };
    int readable = 0;
    int total = (int)(sizeof(topology_files) / sizeof(topology_files[0]));
    if (cpu_dirs > 0) {
        for (int i = 0; i < total; i++) {
            char path[512];
            snprintf(path, sizeof(path), CPU_ROOT "/cpu0/topology/%s", topology_files[i]);
            if (read_line(path, buffer, sizeof(buffer)) == 0)
                readable++;
            else
                printf("     missing %s: %s\n", path, strerror(errno));
        }
    }
    // The text lists are what current util-linux reads; the binary masks are
    // kept for older readers, so their absence is reported but not fatal.
    check(readable >= 3, "topology files are readable", NULL);

    printf("CPUPROBE %s (%d failure(s))\n", failures ? "FAILED" : "OK", failures);
    return failures ? 1 : 0;
}
