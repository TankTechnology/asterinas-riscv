// SPDX-License-Identifier: MPL-2.0
// Report musl's default pthread stack size and the stack margin actually
// available, to decide whether a fixed on-stack array in an LTP test can fit.
#define _GNU_SOURCE
#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static void *probe(void *arg) {
    char local;
    (void)arg;
    pthread_attr_t attr;
    size_t size = 0;
    void *base = NULL;
    if (pthread_getattr_np(pthread_self(), &attr) == 0) {
        pthread_attr_getstack(&attr, &base, &size);
        printf("STACK_PROBE thread_base=%p thread_size=%zu local=%p\n", base, size,
               (void *)&local);
    } else {
        printf("STACK_PROBE getattr_np failed\n");
    }
    fflush(stdout);
    return NULL;
}

int main(void) {
    pthread_t t;
    printf("STACK_PROBE page=%ld\n", sysconf(_SC_PAGESIZE));
    fflush(stdout);
    if (pthread_create(&t, NULL, probe, NULL) != 0) {
        printf("STACK_PROBE create failed\n");
        return 1;
    }
    pthread_join(t, NULL);
    printf("STACK_PROBE done\n");
    fflush(stdout);
    for (;;)
        pause();
}
