// SPDX-License-Identifier: MPL-2.0
//
// AUDIO-M1 smoke test: open the virtio-sound playback node, synthesize a sine
// wave, and write it as raw PCM (S16LE, 48 kHz, stereo). Runs as pid 1 on
// Asterinas RISC-V. Prints `__AUDIO_*_{OK,FAIL}__` markers and a final
// `__AUDIO_DONE__` + `__AUDIO_PASS__`/`__AUDIO_FAIL__`.
//
// The host side verifies the data actually left the guest by pointing QEMU's
// virtio-sound at a `wav` backend and checking the output file grew by the
// expected number of bytes.

#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

#define RATE 48000
#define CHANNELS 2
#define SECONDS 1
#define NFRAMES (RATE * SECONDS)
#define NBYTES (NFRAMES * CHANNELS * (int)sizeof(int16_t))

#define DEV "/dev/snd/pcmC0D0p"

static int failures = 0;

static void ok(const char *name) {
    printf("[AUDIO] %s: OK  __AUDIO_%s_OK__\n", name, name);
}

static void fail(const char *name, const char *msg) {
    failures++;
    printf("[AUDIO] %s: FAIL (%s) __AUDIO_%s_FAIL__\n", name, name, msg);
}

// 440 Hz sine, S16LE, interleaved stereo.
static int16_t *generate_sine(int nframes, int channels, int rate) {
    int16_t *buf = malloc((size_t)nframes * (size_t)channels * sizeof(int16_t));
    if (!buf)
        return NULL;
    const double freq = 440.0;
    const double two_pi = 6.283185307179586476925286766559;
    for (int i = 0; i < nframes; i++) {
        double t = (double)i / (double)rate;
        int16_t s = (int16_t)(16383.0 * sin(two_pi * freq * t));
        for (int c = 0; c < channels; c++)
            buf[i * channels + c] = s;
    }
    return buf;
}

int main(void) {
    int fd = open(DEV, O_WRONLY);
    if (fd < 0) {
        fail("open", "cannot open playback node");
        printf("[AUDIO] errno=%d\n", errno);
        printf("__AUDIO_DONE__ __AUDIO_FAIL__\n");
        return 1;
    }
    ok("open");

    int16_t *buf = generate_sine(NFRAMES, CHANNELS, RATE);
    if (!buf) {
        fail("generate", "malloc");
        printf("__AUDIO_DONE__ __AUDIO_FAIL__\n");
        return 1;
    }

    ssize_t total = 0;
    while (total < NBYTES) {
        ssize_t n = write(fd, (const char *)buf + total, (size_t)(NBYTES - total));
        if (n < 0) {
            fail("write", "write returned error");
            printf("[AUDIO] write errno=%d\n", errno);
            break;
        }
        if (n == 0) {
            fail("write", "short write (0)");
            break;
        }
        total += n;
    }
    printf("[AUDIO] wrote %ld bytes __AUDIO_WRITE_BYTES=%ld__\n",
           (long)total, (long)total);
    if (total == NBYTES)
        ok("write");
    else
        fail("write", "incomplete write");

    close(fd);
    free(buf);

    printf("__AUDIO_DONE__ %s\n", failures ? "__AUDIO_FAIL__" : "__AUDIO_PASS__");
    return failures ? 1 : 0;
}
