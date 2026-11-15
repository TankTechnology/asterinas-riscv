// SPDX-License-Identifier: MPL-2.0
// Isolated reproduction of LTP openfile01's control flow: one shared FILE*,
// 10 threads x 10 fopen("rw"), mutex + two condvars, then close and unlink.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define NFILES 10
#define NTHREADS 10

static pthread_mutex_t m = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t init_cv = PTHREAD_COND_INITIALIZER;
static pthread_cond_t thr_cv = PTHREAD_COND_INITIALIZER;
static int thr_sleeping;

static void *worker(void *arg) {
    long id = (long)arg;
    FILE *fds[NFILES];
    for (int i = 0; i < NFILES; i++) {
        fds[i] = fopen("FILETOOPEN", "rw");
        if (!fds[i]) {
            printf("OPENFILE_PROBE thread=%ld fopen[%d] failed errno=%d\n", id, i, errno);
            fflush(stdout);
            return (void *)1;
        }
    }
    int rc = pthread_mutex_lock(&m);
    if (rc) { printf("OPENFILE_PROBE lock rc=%d\n", rc); return (void *)1; }
    if (++thr_sleeping == NTHREADS) {
        rc = pthread_cond_signal(&init_cv);
        if (rc) printf("OPENFILE_PROBE signal rc=%d\n", rc);
    }
    rc = pthread_cond_wait(&thr_cv, &m);
    if (rc) printf("OPENFILE_PROBE wait rc=%d\n", rc);
    rc = pthread_mutex_unlock(&m);
    if (rc) printf("OPENFILE_PROBE unlock rc=%d\n", rc);
    for (int i = 0; i < NFILES; i++)
        fclose(fds[i]);
    return NULL;
}

int main(void) {
    FILE *fd = fopen("FILETOOPEN", "w");
    if (!fd) { printf("OPENFILE_PROBE create failed errno=%d\n", errno); return 1; }
    fclose(fd);
    printf("OPENFILE_PROBE created\n");
    fflush(stdout);

    pthread_mutex_lock(&m);
    for (long i = 0; i < NTHREADS; i++) {
        pthread_t t;
        int rc = pthread_create(&t, NULL, worker, (void *)i);
        if (rc) { printf("OPENFILE_PROBE create thread %ld rc=%d\n", i, rc); return 1; }
    }
    while (thr_sleeping != NTHREADS) {
        int rc = pthread_cond_wait(&init_cv, &m);
        if (rc) { printf("OPENFILE_PROBE main wait rc=%d\n", rc); return 1; }
    }
    int rc = pthread_cond_broadcast(&thr_cv);
    if (rc) printf("OPENFILE_PROBE broadcast rc=%d\n", rc);
    pthread_mutex_unlock(&m);
    printf("OPENFILE_PROBE threads_done sleeping=%d\n", thr_sleeping);
    fflush(stdout);
    unlink("FILETOOPEN");
    printf("OPENFILE_PROBE done\n");
    fflush(stdout);
    for (;;)
        pause();
}
