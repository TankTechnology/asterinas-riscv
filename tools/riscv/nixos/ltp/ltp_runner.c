// SPDX-License-Identifier: MPL-2.0
//
// Minimal LTP syscall runner for the Asterinas RISC-V gate.
//
// Reads an LTP `runtest/syscalls`-style manifest (`<tag> <binary> [args...]`),
// execs each binary under a per-test watchdog, and classifies the result from
// the test's own T* tokens (TPASS/TFAIL/TBROK/TCONF/TWARN). This replaces kirk,
// which needs a Python interpreter; a few hundred lines of static C is far
// lighter to pack into the initramfs. Results stream to the serial console and
// a final __LTP_GATE_DONE__ marker followed by __LTP_GATE_PASS__ or
// __LTP_GATE_FAIL__ lets the QEMU driver decide pass/fail.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define MAX_LINE 512
#define MAX_ARGS 32
#ifndef BIN_DIR
#define BIN_DIR "/opt/ltp/testcases/bin"
#endif
#ifndef LOG_DIR
#define LOG_DIR "/tmp/ltp_logs"
#endif
// QEMU TCG is ~100x slower than native; LTP's own guidance for slow machines is
// to raise LTP_TIMEOUT_MUL. We default the per-test watchdog generously so slow
// but correct tests (e.g. sendfile07's 65536-write fill loop) are not killed incorrectly.
#ifndef DEFAULT_TIMEOUT_SEC
#define DEFAULT_TIMEOUT_SEC 300
#endif

static int timeout_sec = DEFAULT_TIMEOUT_SEC;
static char timeout_mul_buf[16] = "8";

enum { R_PASS, R_FAIL, R_CONF, R_CRASH, R_TIMEOUT };

static long long now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static int log_has_token(const char *path, const char *tok) {
    FILE *f = fopen(path, "r");
    if (!f)
        return 0;
    char buf[1024];
    int found = 0;
    while (fgets(buf, sizeof(buf), f)) {
        if (strstr(buf, tok)) {
            found = 1;
            break;
        }
    }
    fclose(f);
    return found;
}

static int classify(const char *log_path, int status, int timed_out) {
    if (timed_out)
        return R_TIMEOUT;
    if (WIFSIGNALED(status))
        return R_CRASH;
    if (log_has_token(log_path, "TFAIL") || log_has_token(log_path, "TBROK"))
        return R_FAIL;
    if (log_has_token(log_path, "TCONF"))
        return R_CONF;
    if (WIFEXITED(status) && WEXITSTATUS(status) != 0)
        return R_FAIL;
    return R_PASS;
}

static const char *verdict_name(int r) {
    switch (r) {
    case R_PASS: return "PASS";
    case R_FAIL: return "FAIL";
    case R_CONF: return "CONF";
    case R_CRASH: return "CRASH";
    case R_TIMEOUT: return "TIMEOUT";
    }
    return "UNKNOWN";
}

static void kill_test_group(pid_t supervisor) {
    if (supervisor <= 0)
        return;
    (void)kill(-supervisor, SIGKILL);
    (void)kill(supervisor, SIGKILL);
}

int main(int argc, char **argv) {
    const char *manifest = "/opt/ltp/runtest/syscalls";
    if (argc > 1)
        manifest = argv[1];
    const char *t = getenv("LTP_PER_TEST_TIMEOUT");
    if (t && *t)
        timeout_sec = atoi(t);
    const char *mul = getenv("LTP_TIMEOUT_MUL");
    if (mul && *mul) {
        snprintf(timeout_mul_buf, sizeof(timeout_mul_buf), "%s", mul);
    }

    (void)mkdir(LOG_DIR, 0777);

    FILE *f = fopen(manifest, "r");
    if (!f) {
        printf("[BROK] cannot open manifest %s: %s\n", manifest, strerror(errno));
        printf("__LTP_GATE_DONE__\n__LTP_GATE_FAIL__\n");
        return 1;
    }

    int total = 0, pass = 0, fail = 0, conf = 0, crash = 0, timeout = 0;
    char line[MAX_LINE];

    while (fgets(line, sizeof(line), f)) {
        line[strcspn(line, "\r\n")] = 0;
        if (line[0] == '#' || line[0] == 0)
            continue;

        char *tag = strtok(line, " \t");
        char *bin = strtok(NULL, " \t");
        if (!tag || !bin)
            continue;

        char *args[MAX_ARGS];
        int nargs = 0;
        args[nargs++] = bin;
        char *tok;
        while ((tok = strtok(NULL, " \t")) && nargs < MAX_ARGS - 1)
            args[nargs++] = tok;
        args[nargs] = NULL;

        char binpath[256];
        snprintf(binpath, sizeof(binpath), "%s/%s", BIN_DIR, bin);

        char log_path[256];
        snprintf(log_path, sizeof(log_path), "%s/%s.log", LOG_DIR, tag);
        printf("[RUN] %d %s\n", total + 1, tag);
        fflush(stdout);
        int logfd = open(log_path, O_CREAT | O_TRUNC | O_WRONLY, 0644);
        if (logfd < 0)
            logfd = open("/dev/null", O_WRONLY);

        pid_t pid = fork();
        if (pid == 0) {
            // Keep the persistent runner one process away from tests that
            // intentionally exercise kill()/process-group behavior.
            (void)setpgid(0, 0);
            pid_t test = fork();
            if (test == 0) {
                if (logfd >= 0) {
                    (void)dup2(logfd, 1);
                    (void)dup2(logfd, 2);
                    if (logfd > 2)
                        (void)close(logfd);
                }
                char env_path[512];
                snprintf(env_path, sizeof(env_path), "%s:%s", BIN_DIR,
                         "/usr/bin:/bin");
                setenv("PATH", env_path, 1);
                setenv("TMPDIR", "/tmp", 1);
                setenv("LTPROOT", "/opt/ltp", 1);
                setenv("LTP_TIMEOUT_MUL", timeout_mul_buf, 1);
                setenv("LTP_COLORIZE_OUTPUT", "0", 1);
                setenv("KCONFIG_SKIP_CHECK", "1", 1);
                // Dynamic test binaries resolve libltp.so / libc.so here.
                setenv("LD_LIBRARY_PATH", "/opt/ltp/lib", 1);
                execv(binpath, args);
                dprintf(2, "TCONF: cannot exec %s\n", binpath);
                _exit(32);
            }
            if (test < 0)
                _exit(125);

            int test_status;
            pid_t waited;
            do {
                waited = waitpid(test, &test_status, 0);
            } while (waited < 0 && errno == EINTR);
            if (waited < 0)
                _exit(125);
            if (WIFEXITED(test_status))
                _exit(WEXITSTATUS(test_status));
            if (WIFSIGNALED(test_status)) {
                int test_signal = WTERMSIG(test_status);
                (void)signal(test_signal, SIG_DFL);
                (void)kill(getpid(), test_signal);
                _exit(128 + test_signal);
            }
            _exit(125);
        }
        if (pid > 0)
            (void)setpgid(pid, pid);

        // Watchdog poll.
        long long start = now_ms();
        int status = 125 << 8;
        int timed_out = 0;
        int done = pid < 0;
        while (!done) {
            int w = waitpid(pid, &status, WNOHANG);
            if (w == pid || w < 0) {
                done = 1;
            } else if (now_ms() - start > (long long)timeout_sec * 1000) {
                timed_out = 1;
                kill_test_group(pid);
                waitpid(pid, &status, 0);
                done = 1;
            } else {
                usleep(10000);
            }
        }
        if (WIFSIGNALED(status))
            kill_test_group(pid);
        if (logfd >= 0)
            close(logfd);

        int r = classify(log_path, status, timed_out);
        total++;
        switch (r) {
        case R_PASS: pass++; break;
        case R_FAIL: fail++; break;
        case R_CONF: conf++; break;
        case R_CRASH: crash++; fail++; break;
        case R_TIMEOUT: timeout++; fail++; break;
        }
        printf("[%s] %s\n", verdict_name(r), tag);
        // On failure, dump the first part of the captured output so loader/libc
        // errors are visible on the serial console (otherwise they stay in the
        // per-test log under /tmp).
        if (r == R_FAIL || r == R_CRASH || r == R_TIMEOUT) {
            FILE *lf = fopen(log_path, "r");
            if (lf) {
                char b[4096];
                size_t got = fread(b, 1, sizeof(b) - 1, lf);
                if (got > 0) {
                    b[got] = 0;
                    printf("  -- %s output: %.*s\n", tag, (int)got, b);
                }
                fclose(lf);
            }
        }
    }
    fclose(f);

    printf("__LTP_GATE_DONE__\n");
    printf("[summary] total=%d pass=%d fail=%d conf=%d crash=%d timeout=%d\n",
           total, pass, fail, conf, crash, timeout);
    if (fail == 0)
        printf("__LTP_GATE_PASS__\n");
    else
        printf("__LTP_GATE_FAIL__\n");
    return 0;
}
