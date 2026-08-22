#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <sched.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

#define CHECK(cond, msg) do { if (!(cond)) { printf("FAIL: %s (errno=%d %s)\n", msg, errno, strerror(errno)); _exit(1); } printf("ok: %s\n", msg); } while (0)

static int pivot_root(const char *new_root, const char *put_old)
{
    return syscall(SYS_pivot_root, new_root, put_old);
}

static void make_file(const char *path, const char *content)
{
    int fd = open(path, O_CREAT | O_WRONLY, 0644);
    if (fd < 0) { printf("FAIL: create %s: %s\n", path, strerror(errno)); _exit(1); }
    if (write(fd, content, strlen(content)) < 0) { printf("FAIL: write %s\n", path); _exit(1); }
    close(fd);
}

static int file_has(const char *path, const char *want)
{
    char buf[64];
    int fd = open(path, O_RDONLY);
    if (fd < 0) return 0;
    ssize_t n = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (n <= 0) return 0;
    buf[n] = 0;
    return strstr(buf, want) != NULL;
}

static int mountinfo_has(const char *needle)
{
    int fd = open("/proc/self/mountinfo", O_RDONLY);
    if (fd < 0) return 0;
    static char buf[65536];
    ssize_t n = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (n <= 0) return 0;
    buf[n] = 0;
    return strstr(buf, needle) != NULL;
}

int main(void)
{
    mkdir("/proc", 0555);
    if (mount("proc", "/proc", "proc", 0, NULL) != 0) { printf("FAIL: mount proc\n"); return 1; }

    CHECK(unshare(CLONE_NEWUSER) == 0, "unshare(NEWUSER)");
    CHECK(unshare(CLONE_NEWNS) == 0, "unshare(NEWNS)");

    /* Nix sandbox step 1: make the whole tree private, recursively. */
    CHECK(mount(NULL, "/", NULL, MS_REC | MS_PRIVATE, NULL) == 0,
          "mount(NULL, /, MS_REC|MS_PRIVATE)");
    CHECK(mountinfo_has(" / "), "mountinfo readable after MS_PRIVATE|MS_REC");

    /* Nix sandbox step 2: fresh tmpfs workdir + bind mounts into it. */
    mkdir("/tmp", 0755);
    CHECK(mount("tmpfs", "/tmp", "tmpfs", 0, NULL) == 0, "mount tmpfs /tmp");
    mkdir("/tmp/upper", 0755);
    mkdir("/tmp/target", 0755);
    make_file("/tmp/upper/hello", "store-path-content");

    CHECK(mount("/tmp/upper", "/tmp/target", NULL, MS_BIND, NULL) == 0,
          "bind mount /tmp/upper -> /tmp/target");
    CHECK(file_has("/tmp/target/hello", "store-path-content"), "bind mount exposes source file");

    /* Nix sandbox step 3: remount the bind read-only. */
    CHECK(mount(NULL, "/tmp/target", NULL, MS_BIND | MS_REMOUNT | MS_RDONLY, NULL) == 0,
          "remount bind target read-only");
    errno = 0;
    int fd = open("/tmp/target/newfile", O_CREAT | O_WRONLY, 0644);
    CHECK(fd < 0 && errno == EROFS, "writes to ro-remounted bind fail with EROFS");
    make_file("/tmp/upper/another", "x"); /* the source stays writable */
    CHECK(file_has("/tmp/target/another", "x"), "source remains writable; change visible via bind");
    CHECK(mountinfo_has("/tmp/target"), "mountinfo lists /tmp/target");

    /* Recursive bind: a submount must come along with MS_BIND|MS_REC. */
    mkdir("/tmp/rupper", 0755);
    CHECK(mount("tmpfs", "/tmp/rupper", "tmpfs", 0, NULL) == 0, "mount tmpfs /tmp/rupper");
    mkdir("/tmp/rupper/sub", 0755);
    CHECK(mount("tmpfs", "/tmp/rupper/sub", "tmpfs", 0, NULL) == 0, "mount tmpfs /tmp/rupper/sub");
    make_file("/tmp/rupper/sub/deep", "deep-content");
    mkdir("/tmp/rtarget", 0755);
    CHECK(mount("/tmp/rupper", "/tmp/rtarget", NULL, MS_BIND | MS_REC, NULL) == 0,
          "recursive bind mount");
    CHECK(file_has("/tmp/rtarget/sub/deep", "deep-content"), "recursive bind includes submount");

    /* umount2, including MNT_DETACH. */
    CHECK(umount2("/tmp/rtarget", 0) == 0 || umount2("/tmp/rtarget", MNT_DETACH) == 0,
          "umount2 recursive bind");
    CHECK(umount2("/tmp/target", MNT_DETACH) == 0, "umount2(MNT_DETACH)");

    /* pivot_root dance: new tmpfs root, old root stashed and detached. */
    mkdir("/tmp/newroot", 0755);
    CHECK(mount("tmpfs", "/tmp/newroot", "tmpfs", 0, NULL) == 0, "mount tmpfs /tmp/newroot");
    mkdir("/tmp/newroot/oldroot", 0755);
    make_file("/tmp/newroot/marker", "in-new-root");
    CHECK(pivot_root("/tmp/newroot", "/tmp/newroot/oldroot") == 0, "pivot_root into tmpfs");
    CHECK(chdir("/") == 0, "chdir / after pivot_root");
    CHECK(file_has("/marker", "in-new-root"), "new root contents visible");
    CHECK(umount2("/oldroot", MNT_DETACH) == 0, "detach old root");
    errno = 0;
    CHECK(open("/oldroot/proc/version", O_RDONLY) < 0, "old root no longer accessible");

    printf("MOUNT_NS_TEST_PASS\n");
    return 0;
}
