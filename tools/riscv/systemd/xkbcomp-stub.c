// SPDX-License-Identifier: MPL-2.0

/* xkbcomp-stub: replaces xkbcomp, outputs a pre-compiled keymap.
 *
 * Xorg invokes xkbcomp as:
 *   xkbcomp -w 1 -R/usr/share/X11/xkb -xkm /tmp/server-0.xkm -em1 "..." -emp "> " -eml "..." <source>
 *
 * The -xkm argument specifies the output file path. Xorg reads the compiled
 * keymap from that file. This stub ignores all arguments, writes the
 * pre-compiled keymap to stdout (for the -xkm "-" path) AND to the file
 * specified by the -xkm argument (for the file output path). As a fallback,
 * it also writes to the last argument if it looks like a path (not "-").
 *
 * Cross-compile: riscv64-linux-gnu-gcc -static -o xkbcomp-stub xkbcomp-stub.c
 * Embed keymap: objcopy --add-section .keymap=default.xkm xkbcomp-stub
 */
#include <unistd.h>
#include <string.h>
#include <fcntl.h>

extern const char _binary_default_xkm_start[];
extern const char _binary_default_xkm_end[];

static void write_all(int fd, const char *data, unsigned long len) {
    while (len > 0) {
        ssize_t n = write(fd, data, len);
        if (n <= 0) return;
        data += n;
        len -= (unsigned long)n;
    }
}

static void write_keymap_to_file(const char *path) {
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd >= 0) {
        write_all(fd, _binary_default_xkm_start,
                  (unsigned long)(_binary_default_xkm_end - _binary_default_xkm_start));
        close(fd);
    }
}

int main(int argc, char **argv) {
    (void)argc;
    unsigned long len = (unsigned long)(_binary_default_xkm_end - _binary_default_xkm_start);

    /* 1. Write to stdout — Xorg reads from stdout when -xkm "-" is used */
    write_all(1, _binary_default_xkm_start, len);

    /* 2. Write to the -xkm output file. Xorg's xkbcomp invocation is:
     *      xkbcomp -w 1 -R/... -xkm /tmp/server-0.xkm -em1 ... -emp ... -eml ... <src>
     *    The argument immediately after -xkm is the output file path. */
    int i;
    for (i = 1; i < argc; i++) {
        if (strcmp(argv[i], "-xkm") == 0 && i + 1 < argc) {
            if (strcmp(argv[i + 1], "-") != 0) {
                write_keymap_to_file(argv[i + 1]);
            }
            break;
        }
    }

    /* 3. Fallback: the last positional arg is often the output file.
     *    Only do this if it's not "-" and we didn't already write via -xkm. */
    if (argc > 1) {
        char *last = argv[argc - 1];
        if (last[0] != '-' && strchr(last, '/') != NULL) {
            /* Check we didn't already write this path via -xkm */
            int already_written = 0;
            for (i = 1; i < argc; i++) {
                if (strcmp(argv[i], "-xkm") == 0 && i + 1 < argc &&
                    strcmp(argv[i + 1], last) == 0) {
                    already_written = 1;
                    break;
                }
            }
            if (!already_written) {
                write_keymap_to_file(last);
            }
        }
    }

    return 0;
}
