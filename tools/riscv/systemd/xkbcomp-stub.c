/* xkbcomp-stub: replaces xkbcomp, outputs a pre-compiled keymap to stdout.
 * Xorg invokes xkbcomp as:
 *   xkbcomp -w 1 -R/usr/share/X11/xkb -xkm "-" -em1 "..." -emp "> " -eml "..." /tmp/server-0.xkm
 * The "-" arg for -xkm means "write the compiled keymap to stdout".
 * This stub ignores all arguments and writes the pre-compiled keymap to stdout.
 *
 * Cross-compile: riscv64-linux-gnu-gcc -static -o xkbcomp-stub xkbcomp-stub.c
 * Usage: objcopy --add-section .keymap=default.xkm xkbcomp-stub
 */
#include <unistd.h>
#include <string.h>

/* Embedded by objcopy from pre-compiled keymap */
extern const char _binary_default_xkm_start[];
extern const char _binary_default_xkm_end[];

int main(int argc, char **argv) {
    (void)argc;
    (void)argv;
    const char *p = _binary_default_xkm_start;
    unsigned long len = (unsigned long)(_binary_default_xkm_end - _binary_default_xkm_start);
    while (len > 0) {
        ssize_t n = write(1, p, len);
        if (n <= 0) return 1;
        p += n;
        len -= (unsigned long)n;
    }
    return 0;
}