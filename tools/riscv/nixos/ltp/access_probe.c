// SPDX-License-Identifier: MPL-2.0
//
// M33 probe: access(2) real-uid semantics + supplementary-group permission check.
//
// Runs as /init (root) in a bare initramfs. Verifies:
//   1. access() without AT_EACCESS checks the *real* uid (ruid=root reads a
//      0600 root file even when euid=nobody).
//   2. faccessat2(AT_EACCESS) checks the *effective* ids (EACCES for nobody).
//   3. open() keeps using the fsuid (EACCES for nobody).
//   4. Permission checks consider supplementary groups (group-only-readable
//      file readable when the group is supplementary).

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <stdio.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

static int nfail = 0;

static void check(const char *name, int ok, int e) {
    if (ok) {
        printf("[PASS] %s\n", name);
    } else {
        printf("[FAIL] %s (errno=%d)\n", name, e);
        nfail++;
    }
}

int main(void) {
    // Setup (as root): a 0600 root-only file and a 040 group-5 file.
    int fd = open("/root_only", O_CREAT | O_WRONLY, 0600);
    if (fd < 0) {
        printf("[FAIL] setup: create /root_only (errno=%d)\n", errno);
        nfail++;
    } else {
        close(fd);
    }
    fd = open("/grp_only", O_CREAT | O_WRONLY, 0600);
    if (fd < 0) {
        printf("[FAIL] setup: create /grp_only (errno=%d)\n", errno);
        nfail++;
    } else {
        close(fd);
        chown("/grp_only", 0, 5);
        chmod("/grp_only", 040);
    }

    // Phase 1: ruid=root, euid=nobody(65534).
    if (setreuid((uid_t)-1, 65534) != 0) {
        printf("[FAIL] setup: setreuid (errno=%d)\n", errno);
        goto out;
    }

    errno = 0;
    check("access(R_OK) uses real uid", access("/root_only", R_OK) == 0, errno);

    errno = 0;
    check("faccessat2(AT_EACCESS) uses effective uid",
          syscall(SYS_faccessat2, AT_FDCWD, "/root_only", R_OK, 0x200) == -1
              && errno == EACCES,
          errno);

    errno = 0;
    check("open() still uses fsuid",
          open("/root_only", O_RDONLY) == -1 && errno == EACCES, errno);

    // Phase 2: full nobody credentials, but with group 5 as a supplementary
    // group. The group-only-readable file must be readable.
    if (setreuid((uid_t)-1, 0) != 0) {
        printf("[FAIL] setup: restore euid (errno=%d)\n", errno);
        goto out;
    }
    {
        gid_t g = 5;
        if (setgroups(1, &g) != 0) {
            printf("[FAIL] setup: setgroups (errno=%d)\n", errno);
            goto out;
        }
    }
    if (setresgid(65534, 65534, 65534) != 0 || setresuid(65534, 65534, 65534) != 0) {
        printf("[FAIL] setup: drop ids (errno=%d)\n", errno);
        goto out;
    }

    errno = 0;
    check("open() considers supplementary groups",
          (fd = open("/grp_only", O_RDONLY)) >= 0, errno);
    if (fd >= 0)
        close(fd);

    errno = 0;
    check("access(R_OK) considers supplementary groups",
          access("/grp_only", R_OK) == 0, errno);

out:
    printf("__LTP_GATE_DONE__\n");
    printf(nfail == 0 ? "__LTP_GATE_PASS__\n" : "__LTP_GATE_FAIL__\n");
    for (;;)
        pause();
}
