// SPDX-License-Identifier: MPL-2.0
// M9 "fortune" — prints a random quip from a fixed list. Deterministic-ish
// seed from pid + monotonic clock; no libc rand() needed.
#include <stdio.h>
#include <unistd.h>
#include <time.h>

static const char *const QUOTES[] = {
    "Asterinas: an OS kernel written in Rust.",
    "Nix is a package manager, a language, and a lifestyle.",
    "RISC-V: the instruction set that just won't quit.",
    "No systemd here — just busybox, nix, and a dream.",
    "The store is content-addressed; the joy is not.",
    "One profile to rule them all, and in the nix store bind them.",
};

int main(void) {
    struct timespec ts;
    (void)clock_gettime(CLOCK_MONOTONIC, &ts);
    unsigned long seed = (unsigned long)ts.tv_nsec ^ (unsigned long)getpid();
    int n = (int)(sizeof(QUOTES) / sizeof(QUOTES[0]));
    int i = (int)(seed % (unsigned long)n);
    printf("%s\n", QUOTES[i]);
    return 0;
}
