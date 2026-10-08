// SPDX-License-Identifier: MPL-2.0
// M9 "heartbeat" — the demonstration *service*. A tiny long-running daemon
// that appends one line every 2 seconds to a log file. Installed into
// /nix/store by a Nix derivation (nix-derivation-driven service), then started
// by /etc/init.d/heartbeat via the profile. Proves a Nix-managed service stays
// up across the session.
#define _GNU_SOURCE
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <time.h>

int main(int argc, char **argv) {
    const char *log = "/var/log/heartbeat.log";
    if (argc > 1)
        log = argv[1];

    char host[64] = "nixos";
    (void)gethostname(host, sizeof host);

    FILE *f = fopen(log, "a");
    if (!f) {
        fprintf(stderr, "heartbeat: cannot open %s\n", log);
        return 1;
    }

    long n = 0;
    for (;;) {
        struct timespec ts;
        (void)clock_gettime(CLOCK_REALTIME, &ts);
        fprintf(f, "[%ld.%03ld] %s heartbeat #%ld pid=%d\n",
                (long)ts.tv_sec, (long)(ts.tv_nsec / 1000000),
                host, ++n, (int)getpid());
        fflush(f);
        sleep(2);
    }
    return 0;
}
