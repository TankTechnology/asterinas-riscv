// SPDX-License-Identifier: MPL-2.0

// Acceptance probe for the ALSA PCM uapi.
//
// It speaks the ioctl interface libasound uses, without libasound:
//
//   * `SNDRV_PCM_IOCTL_PVERSION` and `INFO`,
//   * `HW_REFINE` (asks for 48 kHz/S16_LE/stereo and checks the driver
//     constrains the parameter space), then `HW_PARAMS`,
//   * `PREPARE`, `WRITEI_FRAMES` with a half-second 440 Hz square wave,
//     `START` and `DRAIN`.
//
// It also reads the control node's card and PCM info, the way `aplay -l`
// enumerates devices.
//
// As PID 1 it mounts devtmpfs first, so it can be a one-file initramfs
// `/init`. Prints ALSAPROBE_PASS on success.

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

#define PCM_MAGIC 'A'
#define CTL_MAGIC 'U'

#define SNDRV_PCM_IOCTL_PVERSION _IOR(PCM_MAGIC, 0x00, int)
#define SNDRV_PCM_IOCTL_INFO _IOR(PCM_MAGIC, 0x01, struct snd_pcm_info)
#define SNDRV_PCM_IOCTL_HW_REFINE _IOWR(PCM_MAGIC, 0x10, struct snd_pcm_hw_params)
#define SNDRV_PCM_IOCTL_HW_PARAMS _IOWR(PCM_MAGIC, 0x11, struct snd_pcm_hw_params)
#define SNDRV_PCM_IOCTL_HW_FREE _IO(PCM_MAGIC, 0x12)
#define SNDRV_PCM_IOCTL_PREPARE _IO(PCM_MAGIC, 0x40)
#define SNDRV_PCM_IOCTL_START _IO(PCM_MAGIC, 0x42)
#define SNDRV_PCM_IOCTL_DRAIN _IO(PCM_MAGIC, 0x44)
#define SNDRV_PCM_IOCTL_WRITEI_FRAMES _IOW(PCM_MAGIC, 0x50, struct snd_xferi)
#define SNDRV_CTL_IOCTL_PVERSION _IOR(CTL_MAGIC, 0x00, int)
#define SNDRV_CTL_IOCTL_CARD_INFO _IOR(CTL_MAGIC, 0x01, struct snd_ctl_card_info)
#define SNDRV_CTL_IOCTL_PCM_INFO _IOWR(CTL_MAGIC, 0x31, struct snd_pcm_info)

#define SNDRV_MASK_MAX 256
struct snd_mask {
    uint32_t bits[(SNDRV_MASK_MAX + 31) / 32];
};

struct snd_interval {
    unsigned int min, max;
    unsigned int openmin : 1, openmax : 1, integer : 1, empty : 1;
};

struct snd_pcm_hw_params {
    unsigned int flags;
    struct snd_mask masks[3];
    struct snd_mask mres[5];
    struct snd_interval intervals[12];
    struct snd_interval ires[9];
    unsigned int rmask, cmask, info, msbits;
    unsigned int rate_num, rate_den;
    uint64_t fifo_size;
    unsigned char sync[16];
    unsigned char reserved[48];
};

struct snd_pcm_info {
    unsigned int device, subdevice;
    int stream, card;
    unsigned char id[64];
    unsigned char name[80];
    unsigned char subname[32];
    int dev_class, dev_subclass;
    unsigned int subdevices_count, subdevices_avail;
    unsigned char pad1[16];
    unsigned char reserved[64];
};

struct snd_xferi {
    int64_t result;
    void *buf;
    uint64_t frames;
};

struct snd_ctl_card_info {
    int card, pad;
    unsigned char id[16];
    unsigned char driver[16];
    unsigned char name[32];
    unsigned char longname[80];
    unsigned char reserved[16];
    unsigned char mixername[80];
    unsigned char components[128];
};

enum {
    PARAM_ACCESS = 0,
    PARAM_FORMAT = 1,
    PARAM_SUBFORMAT = 2,
    PARAM_SAMPLE_BITS = 8,
    PARAM_FRAME_BITS = 9,
    PARAM_CHANNELS = 10,
    PARAM_RATE = 11,
};

#define ACCESS_RW_INTERLEAVED 3
#define FORMAT_S16_LE 2
#define SUBFORMAT_STD 0

static void fail(const char *step) {
    printf("ALSAPROBE_FAIL %s errno=%d (%s)\n", step, errno, strerror(errno));
    exit(1);
}

static void mask_set(struct snd_mask *mask, unsigned int bit) {
    mask->bits[bit / 32] |= 1u << (bit % 32);
}

static void interval_fixed(struct snd_interval *interval, unsigned int value) {
    interval->min = value;
    interval->max = value;
    interval->integer = 1;
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);

    if (getpid() == 1) {
        mkdir("/dev", 0755);
        if (mount("devtmpfs", "/dev", "devtmpfs", 0, NULL) != 0 && errno != EBUSY) {
            fail("mount-devtmpfs");
        }
    }

    int control = open("/dev/snd/controlC0", O_RDWR | O_CLOEXEC);
    if (control < 0) {
        fail("open-control");
    }
    int version = 0;
    if (ioctl(control, SNDRV_CTL_IOCTL_PVERSION, &version) != 0) {
        fail("ctl-pversion");
    }
    printf("ALSAPROBE_CTL_VERSION %#x\n", version);

    struct snd_ctl_card_info card_info = {0};
    if (ioctl(control, SNDRV_CTL_IOCTL_CARD_INFO, &card_info) != 0) {
        fail("card-info");
    }
    printf("ALSAPROBE_CARD id=%s name=%s driver=%s\n", card_info.id,
           card_info.name, card_info.driver);

    struct snd_pcm_info ctl_pcm = {0};
    ctl_pcm.device = 0;
    ctl_pcm.subdevice = 0;
    ctl_pcm.stream = 0; /* playback */
    if (ioctl(control, SNDRV_CTL_IOCTL_PCM_INFO, &ctl_pcm) != 0) {
        fail("ctl-pcm-info");
    }
    printf("ALSAPROBE_CTL_PCM id=%s name=%s subdevices=%u\n", ctl_pcm.id,
           ctl_pcm.name, ctl_pcm.subdevices_count);
    close(control);

    int pcm = open("/dev/snd/pcmC0D0p", O_RDWR | O_CLOEXEC);
    if (pcm < 0) {
        fail("open-pcm");
    }
    if (ioctl(pcm, SNDRV_PCM_IOCTL_PVERSION, &version) != 0) {
        fail("pcm-pversion");
    }
    printf("ALSAPROBE_PCM_VERSION %#x\n", version);

    struct snd_pcm_info info = {0};
    if (ioctl(pcm, SNDRV_PCM_IOCTL_INFO, &info) != 0) {
        fail("pcm-info");
    }
    printf("ALSAPROBE_INFO name=%s subname=%s\n", info.name, info.subname);

    // Ask for the widest parameter space, the way libasound does, then let
    // the driver constrain it.
    struct snd_pcm_hw_params params;
    memset(&params, 0xFF, sizeof(params));
    memset(params.masks, 0xFF, sizeof(params.masks));
    for (int index = 0; index < 12; index++) {
        params.intervals[index].min = 0;
        params.intervals[index].max = UINT32_MAX;
        params.intervals[index].integer = 1;
    }
    if (ioctl(pcm, SNDRV_PCM_IOCTL_HW_REFINE, &params) != 0) {
        fail("hw-refine");
    }
    printf("ALSAPROBE_REFINE cmask=%#x rate=[%u,%u] channels=[%u,%u] bits=[%u,%u]\n",
           params.cmask,
           params.intervals[PARAM_RATE - PARAM_SAMPLE_BITS].min,
           params.intervals[PARAM_RATE - PARAM_SAMPLE_BITS].max,
           params.intervals[PARAM_CHANNELS - PARAM_SAMPLE_BITS].min,
           params.intervals[PARAM_CHANNELS - PARAM_SAMPLE_BITS].max,
           params.intervals[PARAM_SAMPLE_BITS - PARAM_SAMPLE_BITS].min,
           params.intervals[PARAM_SAMPLE_BITS - PARAM_SAMPLE_BITS].max);

    memset(&params, 0, sizeof(params));
    mask_set(&params.masks[PARAM_ACCESS], ACCESS_RW_INTERLEAVED);
    mask_set(&params.masks[PARAM_FORMAT], FORMAT_S16_LE);
    mask_set(&params.masks[PARAM_SUBFORMAT], SUBFORMAT_STD);
    interval_fixed(&params.intervals[PARAM_SAMPLE_BITS - PARAM_SAMPLE_BITS], 16);
    interval_fixed(&params.intervals[PARAM_FRAME_BITS - PARAM_SAMPLE_BITS], 32);
    interval_fixed(&params.intervals[PARAM_CHANNELS - PARAM_SAMPLE_BITS], 2);
    interval_fixed(&params.intervals[PARAM_RATE - PARAM_SAMPLE_BITS], 48000);
    interval_fixed(&params.intervals[12 - PARAM_SAMPLE_BITS], 1024); /* period size */
    interval_fixed(&params.intervals[13 - PARAM_SAMPLE_BITS], 4096); /* period bytes */
    interval_fixed(&params.intervals[15 - PARAM_SAMPLE_BITS], 8);    /* periods */
    interval_fixed(&params.intervals[17 - PARAM_SAMPLE_BITS], 8192); /* buffer size */
    interval_fixed(&params.intervals[18 - PARAM_SAMPLE_BITS], 32768);/* buffer bytes */
    if (ioctl(pcm, SNDRV_PCM_IOCTL_HW_PARAMS, &params) != 0) {
        fail("hw-params");
    }
    printf("ALSAPROBE_HW_PARAMS ok\n");

    if (ioctl(pcm, SNDRV_PCM_IOCTL_PREPARE) != 0) {
        fail("prepare");
    }
    if (ioctl(pcm, SNDRV_PCM_IOCTL_START) != 0) {
        fail("start");
    }

    // Half a second of a 440 Hz square wave.
    const uint64_t frames = 24000;
    int16_t *tone = malloc(frames * 4);
    if (!tone) {
        fail("malloc");
    }
    for (uint64_t frame = 0; frame < frames; frame++) {
        int16_t sample = ((frame / 54) % 2 == 0) ? 8000 : -8000;
        tone[frame * 2] = sample;
        tone[frame * 2 + 1] = sample;
    }
    struct snd_xferi xfer = {
        .result = 0,
        .buf = tone,
        .frames = frames,
    };
    if (ioctl(pcm, SNDRV_PCM_IOCTL_WRITEI_FRAMES, &xfer) != 0) {
        fail("writei");
    }
    if (xfer.result != (int64_t)frames) {
        printf("ALSAPROBE_FAIL writei-short result=%lld\n",
               (long long)xfer.result);
        return 1;
    }
    printf("ALSAPROBE_WRITEI frames=%llu\n", (unsigned long long)xfer.result);

    if (ioctl(pcm, SNDRV_PCM_IOCTL_DRAIN) != 0) {
        fail("drain");
    }
    ioctl(pcm, SNDRV_PCM_IOCTL_HW_FREE, 0);
    close(pcm);

    printf("ALSAPROBE_PASS\n");
    if (getpid() == 1) {
        sleep(3600);
    }
    return 0;
}
