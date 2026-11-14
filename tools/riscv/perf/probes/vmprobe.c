// SPDX-License-Identifier: MPL-2.0
//
// Checks the four memory-reporting files `vmstat`, `sar` and monitoring agents
// read: parse shape, required keys, and -- the part that matters -- whether the
// buddy histogram adds up to the free memory the rest of the system reports.
//
// Run as init in QEMU, then again on the board.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

#define PAGE_SIZE 4096

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

// Returns the value of a "key value" line, or -1 when the key is absent.
static long key_value(const char *path, const char *key)
{
    FILE *handle = fopen(path, "r");
    if (!handle)
        return -1;
    char line[256];
    long value = -1;
    size_t key_len = strlen(key);
    while (fgets(line, sizeof(line), handle)) {
        // `/proc/meminfo` writes "Key:   value", `/proc/zoneinfo` indents its
        // keys; both are still the same table to a reader.
        char *start = line;
        while (*start == ' ' || *start == '\t')
            start++;
        if (strncmp(start, key, key_len) != 0)
            continue;
        char *rest = start + key_len;
        if (*rest == ':')
            rest++;
        if (*rest != ' ' && *rest != '\t')
            continue;
        value = strtol(rest, NULL, 10);
        break;
    }
    fclose(handle);
    return value;
}

// Sum of count[order] * 2^order over the buddyinfo columns, in pages.
static long buddy_pages(void)
{
    FILE *handle = fopen("/proc/buddyinfo", "r");
    if (!handle)
        return -1;
    char line[2048];
    long pages = 0;
    int lines = 0;
    while (fgets(line, sizeof(line), handle)) {
        char *cursor = strstr(line, "Normal");
        if (!cursor)
            continue;
        cursor += strlen("Normal");
        int order = 0;
        char *end;
        while (*cursor) {
            long count = strtol(cursor, &end, 10);
            if (end == cursor)
                break;
            pages += count << order;
            order++;
            cursor = end;
        }
        lines++;
    }
    fclose(handle);
    return lines == 1 ? pages : -1;
}

int main(void)
{
    mkdir("/tmp", 0777);
    mkdir("/proc", 0555);
    mkdir("/sys", 0555);
    mount("proc", "/proc", "proc", 0, "");
    mount("sysfs", "/sys", "sysfs", 0, "");

    // /proc/vmstat: the keys vmstat dereferences without checking.
    const char *required[] = {
        "nr_free_pages",
        "pgpgin",
        "pgpgout",
        "pswpin",
        "pswpout",
        "pgfault",
        "pgmajfault",
    };
    int present = 0;
    for (unsigned i = 0; i < sizeof(required) / sizeof(required[0]); i++)
        if (key_value("/proc/vmstat", required[i]) >= 0)
            present++;
    check(present == (int)(sizeof(required) / sizeof(required[0])),
          "/proc/vmstat carries the keys vmstat reads", NULL);

    // /proc/buddyinfo: one zone, and it has to add up.
    long pages = buddy_pages();
    check(pages > 0, "/proc/buddyinfo parses as one zone of free pages", NULL);

    long mem_free_kb = key_value("/proc/meminfo", "MemFree");
    check(mem_free_kb > 0, "/proc/meminfo reports MemFree", NULL);
    if (pages > 0 && mem_free_kb > 0) {
        long expected = mem_free_kb * 1024 / PAGE_SIZE;
        long delta = pages - expected;
        if (delta < 0)
            delta = -delta;
        // The two are read at different instants and allocation continues in
        // between; a tenth of a percent is enough to tell "same number" from
        // "wrong accounting".
        long tolerance = expected / 1000 + 64;
        char detail[128];
        snprintf(detail, sizeof(detail), "buddy=%ld pages, MemFree=%ld pages, delta=%ld",
                 pages, expected, delta);
        check(delta <= tolerance, "buddyinfo adds up to MemFree", detail);
    }

    // /proc/zoneinfo: the fields sar and monitoring agents read.
    long zone_free = key_value("/proc/zoneinfo", "pages free");
    long spanned = key_value("/proc/zoneinfo", "spanned");
    check(zone_free > 0, "/proc/zoneinfo reports pages free", NULL);
    check(spanned > 0, "/proc/zoneinfo reports spanned pages", NULL);
    if (zone_free > 0 && pages > 0) {
        long delta = zone_free - pages;
        if (delta < 0)
            delta = -delta;
        char detail[128];
        snprintf(detail, sizeof(detail), "zoneinfo=%ld, buddy=%ld", zone_free, pages);
        check(delta <= pages / 1000 + 64, "zoneinfo agrees with buddyinfo", detail);
    }

    // /proc/pagetypeinfo: header plus one row per migrate type.
    FILE *pt = fopen("/proc/pagetypeinfo", "r");
    int rows = 0, header = 0;
    if (pt) {
        char line[512];
        while (fgets(line, sizeof(line), pt)) {
            if (strstr(line, "Page block order"))
                header = 1;
            if (strstr(line, "type "))
                rows++;
        }
        fclose(pt);
    }
    check(header, "/proc/pagetypeinfo has a page block header", NULL);
    check(rows >= 6, "/proc/pagetypeinfo lists the migrate types", NULL);

    printf("VMPROBE %s (%d failure(s))\n", failures ? "FAILED" : "OK", failures);
    return failures ? 1 : 0;
}
