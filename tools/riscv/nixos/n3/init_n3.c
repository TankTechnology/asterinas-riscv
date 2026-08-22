// SPDX-License-Identifier: MPL-2.0
//
// NIXOS-N3 initramfs /init: boots the official glibc Nix 2.30.2 closure,
// verifies `nix --version`, starts nix-daemon, pings it through the client
// socket, and installs the closure's busybox into the default profile via
// the daemon — the first real package install (N3-1..N3-3).

#define _GNU_SOURCE
#include <fcntl.h>
#include <string.h>
#include <sys/mount.h>
#include <unistd.h>

static void say(const char *s) {
    (void)write(1, s, strlen(s));
}

// Everything runs through busybox sh so we get shell semantics (background
// daemon, loops, redirection). Each step prints a fixed marker.
static const char N3_SCRIPT[] =
    "export PATH=/nix/store/355b1vblxfwy4iw3kbglqavshjlav14z-nix-riscv64-unknown-linux-gnu-2.30.2/bin:/bin\n"
    "export HOME=/root TMPDIR=/tmp\n"
    "echo __N3_VERSION_START__\n"
    "nix --version\n"
    "echo __N3_VERSION_EXIT__=$?\n"
    "nix-store --version\n"
    // Register the closure's paths as valid (the tarball ships .reginfo).
    "nix-store --load-db < /nix/.reginfo >/tmp/reginfo.log 2>&1\n"
    "echo loaddb-rc=$?\n"
    "cat /tmp/reginfo.log\n"
    "echo __N3_REGINFO_EXIT__=$?\n"
    // Bring up the daemon (N3-2).
    "mkdir -p /nix/var/nix/daemon-socket\n"
    "nix-daemon --daemon >/tmp/daemon.log 2>&1 &\n"
    "for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do\n"
    "  test -S /nix/var/nix/daemon-socket/socket && break\n"
    "  sleep 1\n"
    "done\n"
    "echo __N3_DAEMON_LOG__\n"
    "cat /tmp/daemon.log\n"
    "echo __N3_PING_START__\n"
    "NIX_REMOTE=daemon nix store ping 2>&1\n"
    "echo __N3_PING_EXIT__=$?\n"
    // N3-5: real substitution. The busybox store path is not rooted yet
    // (the profile install below comes later), so it can be deleted and
    // re-realised, forcing a real download from cache.nixos.org.
    // Install the closure's busybox into the default profile via the daemon
    // (N3-3): a real package install (DB write + profile generation + gcroot).
    "echo __N3_INSTALL_START__\n"
    "NIX_REMOTE=daemon nix profile add --profile /nix/var/nix/profiles/default \\\n"
    "  /nix/store/7g4f0sx5kcf62d1qnc3sl6ijn5mgn978-busybox-riscv64-unknown-linux-gnu-1.36.1 2>&1\n"
    "echo __N3_INSTALL_EXIT__=$?\n"
    "ls -l /nix/var/nix/profiles/default/bin/ 2>&1\n"
    "/nix/var/nix/profiles/default/bin/busybox --help 2>&1 | head -n 2\n"
    "echo __N3_PROFILE_RUN_EXIT__=$?\n"
    // Build a trivial derivation through the daemon (N3-3, real build):
    // busybox sh writes a file into the output path.
    "echo __N3_BUILD_START__\n"
    "nix-instantiate --expr 'derivation { name = \"n3-hello\"; system = \"riscv64-linux\"; builder = \"/nix/store/7g4f0sx5kcf62d1qnc3sl6ijn5mgn978-busybox-riscv64-unknown-linux-gnu-1.36.1/bin/sh\"; args = [ \"-c\" \"echo hello-from-n3 > $out\" ]; }' >/tmp/hello.drv 2>/tmp/drv.err\n"
    "echo instantiate-rc=$?\n"
    "cat /tmp/hello.drv /tmp/drv.err\n"
    "DRV=$(cat /tmp/hello.drv | tail -n 1)\n"
    "NIX_REMOTE=daemon nix-store --realise \"$DRV\" >/tmp/build.log 2>&1\n"
    "echo __N3_BUILD_RC__=$?\n"
    "cat /tmp/build.log\n"
    "cat /nix/store/*-n3-hello 2>/dev/null\n"
    // N3-5 stretch: HTTPS substituter reachability (DNS + TCP + TLS through
    // QEMU slirp).
    "echo __N3_NET_START__\n"
    "netprobe\n"
    "NIX_SSL_CERT_FILE=/nix/store/p72fm684r47aw9whpmg9ggnjz998hy2r-nss-cacert-riscv64-unknown-linux-gnu-3.108/etc/ssl/certs/ca-bundle.crt nix store ping --store https://cache.nixos.org >/tmp/netping.log 2>&1\n"
    "echo __N3_NET_RC__=$?\n"
    "cat /tmp/netping.log\n"
    "echo __N3_SUBST_START__\n"
    // A path from the nix 2.28.5 closure: confirmed present on
    // cache.nixos.org (host-side narinfo 200) and absent locally, so
    // realising it forces a real substitution (editline + its glibc + attr).
    "SUB=/nix/store/23xc4k52ymabr5lda1p1vhk9pkv8xxpz-editline-riscv64-unknown-linux-gnu-1.17.1\n"
    "test -e \"$SUB\" && echo unexpectedly-present || echo not-local\n"
    "NIX_SSL_CERT_FILE=/nix/store/p72fm684r47aw9whpmg9ggnjz998hy2r-nss-cacert-riscv64-unknown-linux-gnu-3.108/etc/ssl/certs/ca-bundle.crt nix copy --from https://cache.nixos.org --to daemon \"$SUB\" --no-check-sigs 2>&1\n"
    "echo __N3_SUBST_RC__=$?\n"
    "ls \"$SUB\"/lib/ 2>&1\n"
    "test -f \"$SUB\"/lib/libeditline.so.1 && echo __N3_SUBST_RESTORED__\n"
    // N4 task 3: profile generations + GC smoke.
    "echo __N3_GEN_START__\n"
    "NIX_REMOTE=daemon nix profile add --profile /nix/var/nix/profiles/default \"$SUB\" 2>&1\n"
    "echo gen2-rc=$?\n"
    "ls /nix/var/nix/profiles/\n"
    "NIX_REMOTE=daemon nix profile rollback --profile /nix/var/nix/profiles/default 2>&1\n"
    "echo __N3_ROLLBACK_RC__=$?\n"
    // The n3-hello build output is unrooted, so a GC must collect it.
    "NIX_REMOTE=daemon nix store gc 2>&1\n"
    "echo __N3_GC_RC__=$?\n"
    "test -e /nix/store/4namh6905i9mhrvk28byaw04qxy4js6j-n3-hello && echo gc-missed || echo __N3_GC_COLLECTED__\n"
    "echo __N3_DONE__\n";

int main(void) {
    int fd = open("/dev/console", O_RDWR);
    if (fd < 0)
        fd = open("/dev/ttyS0", O_RDWR);
    if (fd >= 0) {
        (void)dup2(fd, 0);
        (void)dup2(fd, 1);
        (void)dup2(fd, 2);
        if (fd > 2)
            (void)close(fd);
    }

    say(">>> N3 init: mounting /proc /sys /tmp <<<\n");

    if (mount("proc", "/proc", "proc", 0, NULL) != 0)
        say("init: mount /proc failed\n");
    if (mount("sysfs", "/sys", "sysfs", 0, NULL) != 0)
        say("init: mount /sys failed\n");
    if (mount("tmpfs", "/tmp", "tmpfs", 0, NULL) != 0)
        say("init: mount /tmp failed\n");

    say(">>> N3 init: running nix-daemon script <<<\n");

    char *const argv[] = { "/bin/sh", "-c", (char *)N3_SCRIPT, NULL };
    (void)execv("/bin/sh", argv);

    say("init: exec /bin/sh failed\n");
    for (;;)
        (void)pause();
    return 0;
}
