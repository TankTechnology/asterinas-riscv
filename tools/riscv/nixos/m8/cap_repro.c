// SPDX-License-Identifier: MPL-2.0
//
// M8 kernel-capability repro: verify, one at a time in QEMU, the kernel
// features NixOS stage-1/stage-2 activation depends on. Each test prints a
// fixed marker plus its result (and errno on failure) so boot_m8_cap_smoke.py
// can assert them unambiguously.
//
//   T1 /dev device nodes (devtmpfs-equivalent nodes from the device registry)
//   T2 mount("devtmpfs", ...) attempt (expect ENODEV — fstype not registered)
//   T3 /proc/self magic symlink (M1 gap)
//   T4 mount namespace: unshare(CLONE_NEWNS) + isolation from parent
//   T5 pivot_root into a fresh tmpfs in a private mount namespace
//   T6 cgroup2 mount + cgroup.controllers + subtree_control activation
//   T7 mount propagation: MS_SLAVE|MS_REC (systemd default; expect unsupported)
//
// Static glibc, same /init pattern as M1-M7.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static void say(const char *s) { (void)write(1, s, strlen(s)); }

/* --- T1: /dev device nodes ------------------------------------------------ */
static void t1_dev_nodes(void) {
    struct stat st;
    const char *nodes[] = {"/dev/null", "/dev/zero", "/dev/console", "/dev/ttyS0"};
    int ok = 1;
    for (size_t i = 0; i < sizeof(nodes) / sizeof(nodes[0]); i++) {
        if (stat(nodes[i], &st) != 0) {
            printf("  %s: MISSING errno=%d\n", nodes[i], errno);
            ok = 0;
        } else if (!S_ISCHR(st.st_mode)) {
            printf("  %s: not a char dev (mode=%o)\n", nodes[i], st.st_mode);
            ok = 0;
        } else {
            printf("  %s: char dev %d:%d\n", nodes[i],
                   (int)(st.st_rdev >> 8), (int)(st.st_rdev & 0xff));
        }
    }
    printf("__M8_T1_DEV__=%s\n", ok ? "OK" : "FAIL");
}

/* --- T2: devtmpfs mount attempt ------------------------------------------- */
static void t2_devtmpfs_mount(void) {
    int r = mount("devtmpfs", "/dev", "devtmpfs", 0, NULL);
    printf("__M8_T2_DEVTMPS__=%s errno=%d (%s)\n",
           r == 0 ? "OK" : "ENODEV", errno, strerror(errno));
}

/* --- T3: /proc/self magic symlink ----------------------------------------- */
static void t3_proc_self(void) {
    char buf[64];
    ssize_t n = readlink("/proc/self", buf, sizeof(buf) - 1);
    if (n < 0) {
        printf("__M8_T3_PROCSELF__=MISSING errno=%d (%s)\n", errno, strerror(errno));
    } else {
        buf[n] = 0;
        printf("__M8_T3_PROCSELF__=OK -> %s\n", buf);
    }
}

/* --- T4: mount namespace isolation ---------------------------------------- */
static void t4_mount_ns(void) {
    pid_t pid = fork();
    if (pid < 0) { printf("__M8_T4_NS__=FAIL fork\n"); return; }
    if (pid == 0) {
        int r = unshare(CLONE_NEWNS);
        if (r != 0) {
            printf("__M8_T4_UNSHARE__=FAIL errno=%d (%s)\n", errno, strerror(errno));
            _exit(2);
        }
        printf("__M8_T4_UNSHARE__=OK\n");
        (void)mkdir("/mntns", 0755);
        r = mount("tmpfs", "/mntns", "tmpfs", 0, NULL);
        printf("__M8_T4_MOUNT__=%s errno=%d\n", r == 0 ? "OK" : "FAIL", errno);
        int fd = open("/mntns/child-marker", O_CREAT | O_WRONLY, 0644);
        if (fd >= 0) { (void)write(fd, "x", 1); (void)close(fd); }
        _exit(0);
    }
    int st = 0;
    (void)waitpid(pid, &st, 0);
    // Parent: the child's mount must not be visible here.
    struct stat sb;
    int visible = (stat("/mntns/child-marker", &sb) == 0);
    printf("__M8_T4_ISOLATED__=%s\n", visible ? "FAIL" : "OK");
}

/* --- T5: pivot_root ------------------------------------------------------- */
static void t5_pivot_root(void) {
    pid_t pid = fork();
    if (pid < 0) { printf("__M8_T5_PIVOT__=FAIL fork\n"); return; }
    if (pid == 0) {
        (void)unshare(CLONE_NEWNS);  // isolate so parent's root is untouched
        (void)mkdir("/pivot", 0755);
        int r = mount("tmpfs", "/pivot", "tmpfs", 0, NULL);
        printf("__M8_T5_MKROOT__=%s errno=%d\n", r == 0 ? "OK" : "FAIL", errno);
        if (r != 0) _exit(2);

        (void)mkdir("/pivot/putold", 0755);
        int fd = open("/pivot/NEWROOT_MARKER", O_CREAT | O_WRONLY, 0644);
        if (fd >= 0) { (void)write(fd, "pivot-ok", 8); (void)close(fd); }

        r = syscall(SYS_pivot_root, "/pivot", "/pivot/putold");
        printf("__M8_T5_PIVOT__=%s errno=%d (%s)\n",
               r == 0 ? "OK" : "FAIL", errno, strerror(errno));
        if (r != 0) _exit(3);

        (void)chdir("/");
        struct stat sb;
        int newroot_ok = (stat("/NEWROOT_MARKER", &sb) == 0);
        int putold_ok = (stat("/putold/etc", &sb) == 0) || (stat("/putold/init", &sb) == 0);
        printf("__M8_T5_NEWROOT__=%s __M8_T5_PUTOLD__=%s\n",
               newroot_ok ? "OK" : "FAIL", putold_ok ? "OK" : "FAIL");
        _exit(0);
    }
    int st = 0;
    (void)waitpid(pid, &st, 0);
}

/* --- T6: cgroup2 ---------------------------------------------------------- */
static void t6_cgroup2(void) {
    (void)mkdir("/sys/fs/cgroup", 0755);
    int r = mount("cgroup2", "/sys/fs/cgroup", "cgroup2", 0, NULL);
    printf("__M8_T6_MOUNT__=%s errno=%d (%s)\n",
           r == 0 ? "OK" : "FAIL", errno, strerror(errno));
    if (r != 0) return;

    char buf[256];
    int fd = open("/sys/fs/cgroup/cgroup.controllers", O_RDONLY);
    if (fd < 0) {
        printf("__M8_T6_CONTROLLERS__=MISSING errno=%d\n", errno);
    } else {
        ssize_t n = read(fd, buf, sizeof(buf) - 1);
        (void)close(fd);
        buf[n < 0 ? 0 : n] = 0;
        buf[strcspn(buf, "\n")] = 0;
        printf("__M8_T6_CONTROLLERS__=%s\n", buf);
    }

    (void)mkdir("/sys/fs/cgroup/m8test", 0755);
    fd = open("/sys/fs/cgroup/cgroup.subtree_control", O_WRONLY);
    if (fd < 0) {
        printf("__M8_T6_SUBTREE__=MISSING errno=%d\n", errno);
    } else {
        const char *ctrl = "+cpu +memory +pids";
        ssize_t n = write(fd, ctrl, strlen(ctrl));
        (void)close(fd);
        printf("__M8_T6_SUBTREE__=%s wrote=%zd errno=%d\n", n > 0 ? "OK" : "FAIL", n, errno);
    }

    fd = open("/sys/fs/cgroup/m8test/cgroup.controllers", O_RDONLY);
    if (fd < 0) {
        printf("__M8_T6_CHILDCTRL__=MISSING errno=%d\n", errno);
    } else {
        ssize_t n = read(fd, buf, sizeof(buf) - 1);
        (void)close(fd);
        buf[n < 0 ? 0 : n] = 0;
        buf[strcspn(buf, "\n")] = 0;
        printf("__M8_T6_CHILDCTRL__=%s\n", buf);
    }
}

/* --- T8: chroot (the busybox switch_root path NixOS stage-1 uses) -------- */
static void t8_chroot(void) {
    pid_t pid = fork();
    if (pid < 0) { printf("__M8_T8_CHROOT__=FAIL fork\n"); return; }
    if (pid == 0) {
        (void)unshare(CLONE_NEWNS);
        (void)mkdir("/chr", 0755);
        int r = mount("tmpfs", "/chr", "tmpfs", 0, NULL);
        if (r != 0) { printf("__M8_T8_CHROOT__=FAIL mkroot errno=%d\n", errno); _exit(2); }
        // Populate a marker and /bin so the chroot can stat itself.
        int fd = open("/chr/CHROOT_MARKER", O_CREAT | O_WRONLY, 0644);
        if (fd >= 0) { (void)write(fd, "chr", 3); (void)close(fd); }
        r = chroot("/chr");
        printf("__M8_T8_CHROOT__=%s errno=%d (%s)\n",
               r == 0 ? "OK" : "FAIL", errno, strerror(errno));
        if (r != 0) _exit(3);
        (void)chdir("/");
        struct stat sb;
        int marker_ok = (stat("/CHROOT_MARKER", &sb) == 0);
        printf("__M8_T8_CHROOT_ROOT__=%s\n", marker_ok ? "OK" : "FAIL");
        _exit(0);
    }
    int st = 0;
    (void)waitpid(pid, &st, 0);
}

/* --- T7: mount propagation ------------------------------------------------ */
static void t7_mount_propagation(void) {
    // systemd makes / a shared or slave subtree; Asterinas is expected to only
    // support MS_PRIVATE (see upstream systemd overlay patch 0003).
    int r = mount(NULL, "/", NULL, MS_SLAVE | MS_REC, NULL);
    printf("__M8_T7_SLAVE__=%s errno=%d (%s)\n",
           r == 0 ? "OK" : "UNSUPPORTED", errno, strerror(errno));
    r = mount(NULL, "/", NULL, MS_PRIVATE | MS_REC, NULL);
    printf("__M8_T7_PRIVATE__=%s errno=%d (%s)\n",
           r == 0 ? "OK" : "UNSUPPORTED", errno, strerror(errno));
}

int main(void) {
    int fd = open("/dev/console", O_RDWR);
    if (fd < 0) fd = open("/dev/ttyS0", O_RDWR);
    if (fd >= 0) {
        (void)dup2(fd, 0); (void)dup2(fd, 1); (void)dup2(fd, 2);
        if (fd > 2) (void)close(fd);
    }

    say(">>> M8 init: kernel capability repro <<<\n");
    (void)mount("proc", "/proc", "proc", 0, NULL);
    (void)mount("sysfs", "/sys", "sysfs", 0, NULL);
    (void)mount("tmpfs", "/tmp", "tmpfs", 0, NULL);

    t1_dev_nodes();
    t2_devtmpfs_mount();
    t3_proc_self();
    t4_mount_ns();
    t5_pivot_root();
    t6_cgroup2();
    t7_mount_propagation();
    t8_chroot();

    say(">>> M8 capability repro done <<<\n");
    for (;;) (void)pause();
    return 0;
}
