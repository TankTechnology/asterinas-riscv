#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# mknodat behaviour probe: compares Asterinas against Linux semantics for the
# cases gnulib's mknod/mkfifoat/mknodat checks exercise.
set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
log() { printf '%s\n' "$*" | tee -a /tmp/mknodat_probe.log >/dev/console; }
log "mk_start $(date -u '+%Y-%m-%dT%H:%M:%SZ') uid=$(id -u)"
cat >/tmp/mknodat_probe.c <<'C'
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

static void r(const char *label, int rc, int err) {
    printf("mk_case %-28s rc=%d errno=%s\n", label, rc,
           (rc == 0) ? "-" : strerror(err));
}

int main(void) {
    mkdir("/tmp/mkp", 0777);
    chdir("/tmp/mkp");
    struct stat st;
    int rc, err;

    errno = 0; rc = mknodat(AT_FDCWD, "fifo", S_IFIFO | 0600, 0); err = errno;
    r("fifo:create", rc, err);

    errno = 0; rc = mknodat(AT_FDCWD, "fifo", S_IFIFO | 0600, 0); err = errno;
    r("fifo:exists", rc, err);

    int dfd = open(".", O_RDONLY | O_DIRECTORY);
    errno = 0; rc = mknodat(dfd, "fifo2", S_IFIFO | 0600, 0); err = errno;
    r("fifo:dirfd", rc, err);

    errno = 0; rc = mknodat(AT_FDCWD, "reg", S_IFREG | 0644, 0); err = errno;
    r("reg:create", rc, err);
    if (stat("reg", &st) == 0) printf("mk_stat reg is_reg=%d mode=%o\n",
                                     S_ISREG(st.st_mode), st.st_mode & 07777);

    errno = 0; rc = mknodat(AT_FDCWD, "mode0", 0644, 0); err = errno;
    r("mode0:create", rc, err);

    errno = 0; rc = mknodat(AT_FDCWD, "dirtype", S_IFDIR | 0755, 0); err = errno;
    r("dirtype", rc, err);

    errno = 0; rc = mknodat(AT_FDCWD, "socktype", S_IFSOCK | 0600, 0); err = errno;
    r("socktype", rc, err);

    errno = 0; rc = mknodat(AT_FDCWD, "chr", S_IFCHR | 0600, makedev(1, 3)); err = errno;
    r("chr:root", rc, err);
    if (rc == 0 && stat("chr", &st) == 0)
        printf("mk_stat chr is_chr=%d dev=%u,%u\n", S_ISCHR(st.st_mode),
               major(st.st_rdev), minor(st.st_rdev));

    errno = 0; rc = mknodat(AT_FDCWD, "blk", S_IFBLK | 0600, makedev(7, 0)); err = errno;
    r("blk:root", rc, err);

    errno = 0; rc = mknodat(AT_FDCWD, "fifo3/", S_IFIFO | 0600, 0); err = errno;
    r("fifo:trailing_slash", rc, err);

    umask(027);
    errno = 0; rc = mknodat(AT_FDCWD, "fifo_umask", S_IFIFO | 0666, 0); err = errno;
    r("fifo:umask027", rc, err);
    if (stat("fifo_umask", &st) == 0)
        printf("mk_stat fifo_umask mode=%o\n", st.st_mode & 07777);
    umask(022);

    int regfd = open("reg", O_RDONLY);
    errno = 0; rc = mknodat(regfd, "x", S_IFIFO | 0600, 0); err = errno;
    r("dirfd_is_file", rc, err);

    return 0;
}
C
gcc -O2 -o /tmp/mknodat_probe /tmp/mknodat_probe.c 2>/tmp/mk-gcc.err
log "mk_build rc=$? err=$(head -1 /tmp/mk-gcc.err | head -c 100)"
[ -s /tmp/mk-gcc.err ] && [ "$(head -c 4 /tmp/mk-gcc.err)" != "mk_" ] && log "mk_gcc_warn=$(tail -2 /tmp/mk-gcc.err | tr '\n' ' ' | head -c 160)"
/tmp/mknodat_probe 2>&1 | while IFS= read -r l; do log "$l"; done

# Same device-node cases as an unprivileged user (uid 1000), Linux expects EPERM.
if command -v setpriv >/dev/null 2>&1; then
    cat >/tmp/mknodat_probe_np.c <<'C'
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>
int main(void) {
    chdir("/tmp/mkp");
    errno = 0; int rc = mknodat(AT_FDCWD, "np_chr", S_IFCHR | 0600, makedev(1, 3));
    printf("mk_case chr:uid1000 rc=%d errno=%s\n", rc, rc ? strerror(errno) : "-");
    errno = 0; rc = mknodat(AT_FDCWD, "np_fifo", S_IFIFO | 0600, 0);
    printf("mk_case fifo:uid1000 rc=%d errno=%s\n", rc, rc ? strerror(errno) : "-");
    return 0;
}
C
    gcc -O2 -o /tmp/mknodat_probe_np /tmp/mknodat_probe_np.c 2>/tmp/mk-np-gcc.err
    log "mk_np_build rc=$? err=$(head -1 /tmp/mk-np-gcc.err | head -c 100)"
    chmod 777 /tmp/mkp
    setpriv --reuid=1000 --regid=1000 --clear-groups /tmp/mknodat_probe_np 2>&1 | \
        while IFS= read -r l; do log "$l"; done
else
    log "mk_no_setpriv"
fi
log "mk_done $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo DAILY_DONE=ok >/dev/console
