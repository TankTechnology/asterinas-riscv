// SPDX-License-Identifier: MPL-2.0
//
// M4 repro #2: general-dynamic TLS across a shared library. The main thread
// reads the shared `__thread` variable fine (DTV set up at startup), but the
// child thread faults in `__tls_get_addr` because its DTV is NULL. This is the
// actual nix blocker, narrowed down in M4-report.md.

#include <pthread.h>
#include <stdio.h>

extern int *get_shared_tls(void);

static void *thread_fn(void *arg) {
    int *p = get_shared_tls();
    printf("child: *p=%d (addr %p)\n", *p, (void *)p);
    return NULL;
}

int main(void) {
    int *p = get_shared_tls();
    printf("main: *p=%d (addr %p)\n", *p, (void *)p);

    pthread_t t;
    if (pthread_create(&t, NULL, thread_fn, NULL) != 0) {
        printf("pthread_create failed\n");
        return 1;
    }
    pthread_join(t, NULL);
    printf("__M4_SHARED_TLS_DONE__\n");
    return 0;
}
