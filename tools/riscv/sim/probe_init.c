// RISC-V QEMU 仿真探针：作为最小 initramfs 的 /init 运行，直接打串口
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/mman.h>
#include <sys/utsname.h>
#include <pthread.h>

static void *worker(void *arg) { *(int *)arg = 42; return NULL; }

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("SIM_PROBE_BEGIN\n");

    struct utsname u;
    if (uname(&u) == 0) printf("SIM_UNAME sysname=%s release=%s machine=%s\n", u.sysname, u.release, u.machine);
    else perror("uname");

    FILE *f = fopen("/proc/self/status", "r");
    if (f) {
        char line[256]; int n = 0;
        while (fgets(line, sizeof line, f) && n < 5) {
            if (!strncmp(line, "Name:", 5) || !strncmp(line, "Pid:", 4) || !strncmp(line, "VmRSS:", 6) || !strncmp(line, "Threads:", 8)) {
                line[strcspn(line, "\n")] = 0; printf("SIM_STATUS %s\n", line); n++;
            }
        }
        fclose(f);
    } else perror("open /proc/self/status");

    size_t len = 1 << 20;
    void *p = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (p != MAP_FAILED) {
        memset(p, 0xA5, len);
        printf("SIM_MMAP ok bytes=%zu first=%02x last=%02x\n", len, ((unsigned char*)p)[0], ((unsigned char*)p)[len-1]);
        munmap(p, len);
    } else perror("mmap");

    int v = 0; pthread_t t;
    if (pthread_create(&t, NULL, worker, &v) == 0) { pthread_join(t, NULL); printf("SIM_PTHREAD ok value=%d\n", v); }
    else perror("pthread_create");

    FILE *w = fopen("/tmp/sim_write.txt", "w");
    if (w) { fputs("sim-probe\n", w); fflush(w); fsync(fileno(w)); fclose(w); printf("SIM_FSYNC ok\n"); }
    else perror("fopen /tmp");

    printf("SIM_PROBE_END\n");
    for (;;) pause();
    return 0;
}
