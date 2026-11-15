// SPDX-License-Identifier: MPL-2.0
//
// Init process for the fio comparison gate. Mounts tmpfs at /tmp, optionally
// mounts the ext4 virtio-blk disk at /mnt, then runs fio (JSON output) for the
// tmpfs and ext4 workloads with a watchdog per job.
#define _GNU_SOURCE
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define JOB_SECONDS 60

struct job {
    const char *name;
    const char *dir;
    const char *rw;
};

static const struct job jobs[] = {
    {"tmpfs-read", "/tmp", "read"},
    {"tmpfs-write", "/tmp", "write"},
    {"tmpfs-randread", "/tmp", "randread"},
    {"tmpfs-randwrite", "/tmp", "randwrite"},
    {"ext4-read", "/mnt", "read"},
    {"ext4-write", "/mnt", "write"},
    {"ext4-randread", "/mnt", "randread"},
    {"ext4-randwrite", "/mnt", "randwrite"},
};

static double now_seconds(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static void run_job(const struct job *job) {
    char file[128];
    snprintf(file, sizeof(file), "%s/fio.dat", job->dir);
    printf("FIO_RUN %s\n", job->name);
    fflush(stdout);
    pid_t child = fork();
    if (child == 0) {
        char name_arg[64], file_arg[160], rw_arg[32];
        snprintf(name_arg, sizeof(name_arg), "--name=%s", job->name);
        snprintf(file_arg, sizeof(file_arg), "--filename=%s", file);
        snprintf(rw_arg, sizeof(rw_arg), "--rw=%s", job->rw);
        char *argv[] = {
            "fio", name_arg, file_arg, rw_arg,
            "--size=64M", "--bs=4k", "--ioengine=psync", "--iodepth=1",
            "--runtime=5", "--time_based", "--direct=0", "--group_reporting",
            "--output-format=json", "--eta=never",
            NULL,
        };
        execv("/fio", argv);
        _exit(127);
    }
    if (child < 0) {
        printf("FIO_END %s status=spawn-failed\n", job->name);
        return;
    }
    double deadline = now_seconds() + JOB_SECONDS;
    int status = 0;
    int timed_out = 0;
    for (;;) {
        pid_t waited = waitpid(child, &status, WNOHANG);
        if (waited == child)
            break;
        if (waited < 0 && errno != EINTR)
            break;
        if (now_seconds() >= deadline) {
            timed_out = 1;
            kill(child, SIGKILL);
            waitpid(child, &status, 0);
            break;
        }
        struct timespec pause = {0, 5 * 1000 * 1000};
        nanosleep(&pause, NULL);
    }
    const char *outcome = timed_out ? "timeout"
                          : (WIFEXITED(status) && WEXITSTATUS(status) == 0) ? "ok"
                          : "error";
    printf("FIO_END %s status=%s\n", job->name, outcome);
    fflush(stdout);
}

int main(void) {
    mkdir("/dev", 0755);
    mkdir("/proc", 0755);
    mkdir("/tmp", 0755);
    mkdir("/mnt", 0755);
    mount("devtmpfs", "/dev", "devtmpfs", 0, NULL);
    mount("proc", "/proc", "proc", 0, NULL);
    mount("tmpfs", "/tmp", "tmpfs", 0, NULL);
    int have_disk = mount("/dev/vda", "/mnt", "ext4", 0, NULL) == 0;
    printf("FIO_BEGIN disk=%d\n", have_disk);
    fflush(stdout);
    for (size_t i = 0; i < sizeof(jobs) / sizeof(jobs[0]); i++) {
        if (strncmp(jobs[i].dir, "/mnt", 4) == 0 && !have_disk)
            continue;
        run_job(&jobs[i]);
    }
    printf("FIO_DONE\n");
    fflush(stdout);
    for (;;) {
        int status;
        while (waitpid(-1, &status, WNOHANG) > 0)
            ;
        pause();
    }
}
