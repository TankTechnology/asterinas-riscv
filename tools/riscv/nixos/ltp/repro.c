// SPDX-License-Identifier: MPL-2.0
//
// Minimal static repro for LTP syscall bugs, run as /init in a bare initramfs.
// Prints per-probe [PASS]/[FAIL] lines plus __LTP_GATE_DONE__ / __LTP_GATE_PASS__
// / __LTP_GATE_FAIL__ so the existing boot_ltp_gate.py driver can collect it.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

static int nfail = 0;

static void check(const char *name, int ok, long ret, int err) {
    printf("[%s] %s (ret=%ld errno=%d %s)\n", ok ? "PASS" : "FAIL", name,
           ret, err, strerror(err));
    if (!ok) nfail++;
}

int main(void) {
    if (mount("proc", "/proc", "proc", 0, NULL) != 0) {}
    if (mount("sysfs", "/sys", "sysfs", 0, NULL) != 0) {}
    if (mount("tmpfs", "/tmp", "tmpfs", 0, NULL) != 0) {}

    const char *file = "/tmp/tf";
    const char *sym = "/tmp/sl";
    int fd = open(file, O_CREAT | O_RDWR, 0644);
    (void)fd;
    if (symlink(file, sym) != 0) {
        printf("[FAIL] symlink setup (errno=%d %s)\n", errno, strerror(errno));
        nfail++;
    }

    char buf[256];
    errno = 0;
    // readlink(bufsize=0) must fail with EINVAL.
    long r = readlink(sym, buf, 0);
    check("readlink(sym,0) -> EINVAL", r == -1 && errno == EINVAL, r, errno);

    // readlinkat(dirfd, symlink, bufsiz=0) must fail with EINVAL.
    int dfd = open("/tmp", O_RDONLY | O_DIRECTORY);
    errno = 0;
    r = readlinkat(dfd, "sl", buf, 0);
    check("readlinkat(dfd,0) -> EINVAL", r == -1 && errno == EINVAL, r, errno);

    // RAW syscall (bypass musl wrapper) with bufsize=0.
    errno = 0;
    long raw = syscall(SYS_readlinkat, dfd, "sl", buf, 0);
    int e = errno;
    printf("[raw] SYS_readlinkat(dfd,sl,buf,0) ret=%ld errno=%d %s\n", raw, e, strerror(e));

    // readlink on a regular file must fail with EINVAL.
    errno = 0;
    r = readlink(file, buf, sizeof(buf));
    check("readlink(file) -> EINVAL", r == -1 && errno == EINVAL, r, errno);

    // sbrk(0) returns current break (not -1).
    void *b0 = sbrk(0);
    check("sbrk(0) ok", b0 != (void *)-1, (long)b0, errno);

    // sbrk(+8192) must extend and return the old break.
    errno = 0;
    void *b1 = sbrk(8192);
    check("sbrk(+8192) extends", b1 != (void *)-1, (long)b1, errno);

    // sbrk(-8192) must shrink and return the old break.
    errno = 0;
    void *b2 = sbrk(-8192);
    check("sbrk(-8192) shrinks", b2 != (void *)-1, (long)b2, errno);

    // RAW brk syscall: brk(0) then brk(cur + 8192) should extend (proves the
    // kernel brk works, isolating sbrk01's failure to musl's sbrk stub).
    errno = 0;
    long cur = syscall(SYS_brk, 0);
    long want = cur + 8192;
    long got = syscall(SYS_brk, want);
    printf("[raw] SYS_brk: cur=%ld brk(%ld)=%ld expect=%ld %s\n",
           cur, want, got, want, got == want ? "OK" : "MISMATCH");

    // pwrite errno cases: EBADF on a read-only fd, EFAULT on a NULL buffer.
    int ro = open(file, O_RDONLY);
    errno = 0;
    long pw = pwrite(ro, "x", 1, 0);
    check("pwrite(ro_fd) -> EBADF", pw == -1 && errno == EBADF, pw, errno);

    int rw = open(file, O_RDWR);
    errno = 0;
    pw = pwrite(rw, NULL, 1, 0);
    check("pwrite(NULL) -> EFAULT", pw == -1 && errno == EFAULT, pw, errno);

    // access(X_OK) on a 0555 file should return 0 (permission bits only).
    if (chmod(file, 0555) != 0) {}
    errno = 0;
    long ac = access(file, X_OK);
    printf("[raw] access(%s, X_OK) = %ld errno=%d %s\n", file, ac, errno, strerror(errno));

    printf("__LTP_GATE_DONE__\n");
    if (nfail == 0)
        printf("__LTP_GATE_PASS__\n");
    else
        printf("__LTP_GATE_FAIL__\n");
    return 0;
}
