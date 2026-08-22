// SPDX-License-Identifier: MPL-2.0
// M9 "nixos-info" — a small neofetch-style system banner. Reads hostname,
// kernel release (uname), uptime and memory from /proc, and reports how many
// store paths and profile generations nix currently manages. Pure C, no deps.
#define _GNU_SOURCE
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <sys/utsname.h>
#include <dirent.h>
#include <sys/stat.h>

static void read_one(const char *path, char *buf, size_t n) {
    FILE *f = fopen(path, "r");
    if (!f) { buf[0] = '\0'; return; }
    if (!fgets(buf, (int)n, f)) buf[0] = '\0';
    fclose(f);
    size_t l = strlen(buf);
    while (l && (buf[l-1] == '\n' || buf[l-1] == ' ')) buf[--l] = '\0';
}

static long count_dir(const char *path) {
    DIR *d = opendir(path);
    if (!d) return -1;
    long n = 0;
    struct dirent *e;
    while ((e = readdir(d))) {
        if (e->d_name[0] == '.') continue;
        n++;
    }
    closedir(d);
    return n;
}

int main(void) {
    char host[128] = "?", rel[128] = "?", mach[128] = "?";
    char up[128] = "?", memtotal[128] = "?", memavail[128] = "?";

    (void)gethostname(host, sizeof host);
    struct utsname u;
    if (uname(&u) == 0) {
        snprintf(rel, sizeof rel, "%s", u.release);
        snprintf(mach, sizeof mach, "%s", u.machine);
    }
    read_one("/proc/uptime", up, sizeof up);
    read_one("/proc/meminfo", memtotal, sizeof memtotal);
    /* MemAvailable is the 3rd line; grab MemTotal + MemAvailable. */
    FILE *mi = fopen("/proc/meminfo", "r");
    if (mi) {
        char line[256];
        while (fgets(line, sizeof line, mi)) {
            if (!strncmp(line, "MemTotal:", 9)) { snprintf(memtotal, sizeof memtotal, "%s", line); }
            if (!strncmp(line, "MemAvailable:", 13)) { snprintf(memavail, sizeof memavail, "%s", line); }
        }
        fclose(mi);
    }

    long store = count_dir("/nix/store");
    long gens = 0;
    DIR *pd = opendir("/nix/var/nix/profiles");
    if (pd) {
        struct dirent *e;
        while ((e = readdir(pd))) {
            if (e->d_name[0] == '.') continue;
            if (!strncmp(e->d_name, "default-", 8)) gens++;
        }
        closedir(pd);
    }

    puts("      ___     _         _");
    puts("     / _ \\   | |       (_)");
    puts("    / /_\\ \\__| |_ ___ _ _ __ _ _ __   ___");
    puts("    |  _  / __| __/ _ \\ | '__| | '_ \\ / __|");
    puts("    | | | \\__ \\ ||  __/ | |  | | | | |\\__ \\");
    puts("    \\_| |_/___/\\__\\___|_|_|  |_|_| |_|___/");
    puts("");
    printf("  hostname : %s\n", host);
    printf("  kernel   : %s\n", rel);
    printf("  arch     : %s\n", mach);
    printf("  uptime   : %ss\n", up);
    printf("  %s", memtotal);
    printf("  %s", memavail);
    printf("  nix      : %ld store paths, %ld profile generations\n",
           store, gens);
    return 0;
}
