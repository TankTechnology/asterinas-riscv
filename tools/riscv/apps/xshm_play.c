/* Play a video full-screen on an X server through MIT-SHM.
 *
 * The board's X server has no accelerated draw path: a frame sent with
 * `XPutImage` costs the server about 10 ms per megabyte, and mpv's `x11`
 * output does not use shared memory, so mpv caps at four to six frames per
 * second at 1080p no matter how fast the socket underneath it is. The same
 * frame through `XShmPutImage` costs 0.3 ms. This is the small player that
 * takes the shared-memory path: decode with libav, convert and scale with
 * libswscale straight into an XShm segment, hand the segment to the server.
 *
 *   xshm_play <file> [--loop] [--no-scale] [--untimed]
 *
 *   --no-scale   leave the video at its own resolution instead of filling the
 *                screen. Upscaling 480p or 720p to 1920x1080 costs more than
 *                everything else in the frame, so this is the difference
 *                between 17 and 34 frames per second for 720p material.
 *   --untimed    decode as fast as possible instead of honouring the
 *                timestamps; use it to measure, not to watch.
 *
 * Every two seconds it prints frames per second plus the mean cost of each
 * stage, so a change can be attributed to decode, scale or display.
 *
 * Build on the board (needs libx11-dev, libxext-dev and the libav*-dev
 * packages):
 *
 *   gcc -O2 -I/usr/include/riscv64-linux-gnu -o xshm_play xshm_play.c \
 *       -lX11 -lXext -lavformat -lavcodec -lswscale -lavutil
 */
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <X11/extensions/XShm.h>
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/imgutils.h>
#include <libswscale/swscale.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ipc.h>
#include <sys/shm.h>
#include <sys/time.h>
#include <unistd.h>

static volatile sig_atomic_t stop_requested;

static void on_signal(int signo) { (void)signo; stop_requested = 1; }

static double now(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return tv.tv_sec + tv.tv_usec / 1e6;
}

struct shm_screen {
    Display *dpy;
    int screen;
    Window win;
    GC gc;
    XImage *image;
    XShmSegmentInfo shm;
    int width, height;
};

static int shm_completion_event(struct shm_screen *s) {
    return XShmGetEventBase(s->dpy) + ShmCompletion;
}

/* Waits for the server to finish reading the segment, so the next frame does
 * not overwrite pixels the server has not taken yet. */
static void shm_wait_completion(struct shm_screen *s) {
    XEvent event;
    while (XCheckTypedEvent(s->dpy, shm_completion_event(s), &event))
        ;
}

static int shm_open_screen(struct shm_screen *s) {
    s->dpy = XOpenDisplay(NULL);
    if (!s->dpy) {
        fprintf(stderr, "cannot open display\n");
        return -1;
    }
    s->screen = DefaultScreen(s->dpy);
    s->width = DisplayWidth(s->dpy, s->screen);
    s->height = DisplayHeight(s->dpy, s->screen);
    if (!XShmQueryExtension(s->dpy)) {
        fprintf(stderr, "the server has no MIT-SHM\n");
        return -1;
    }

    XVisualInfo vinfo;
    if (!XMatchVisualInfo(s->dpy, s->screen, 24, TrueColor, &vinfo)) {
        fprintf(stderr, "no 24-bit TrueColor visual\n");
        return -1;
    }
    XSetWindowAttributes attrs = {0};
    attrs.override_redirect = True;
    attrs.background_pixel = BlackPixel(s->dpy, s->screen);
    attrs.event_mask = ExposureMask | KeyPressMask;
    s->win = XCreateWindow(s->dpy, RootWindow(s->dpy, s->screen), 0, 0, s->width,
                           s->height, 0, vinfo.depth, InputOutput, vinfo.visual,
                           CWOverrideRedirect | CWBackPixel | CWEventMask, &attrs);
    XStoreName(s->dpy, s->win, "asterinas video");
    s->gc = XCreateGC(s->dpy, s->win, 0, NULL);
    XMapRaised(s->dpy, s->win);
    XSync(s->dpy, False);

    s->image = XShmCreateImage(s->dpy, vinfo.visual, vinfo.depth, ZPixmap, NULL,
                               &s->shm, s->width, s->height);
    if (!s->image) {
        fprintf(stderr, "XShmCreateImage failed\n");
        return -1;
    }
    size_t bytes = (size_t)s->image->bytes_per_line * s->image->height;
    s->shm.shmid = shmget(IPC_PRIVATE, bytes, IPC_CREAT | 0600);
    if (s->shm.shmid < 0) {
        perror("shmget");
        return -1;
    }
    s->shm.shmaddr = s->image->data = shmat(s->shm.shmid, NULL, 0);
    if (s->shm.shmaddr == (char *)-1) {
        perror("shmat");
        return -1;
    }
    s->shm.readOnly = False;
    if (!XShmAttach(s->dpy, &s->shm)) {
        fprintf(stderr, "XShmAttach failed\n");
        return -1;
    }
    XSync(s->dpy, False);
    /* The server keeps its own reference after attaching. */
    shmctl(s->shm.shmid, IPC_RMID, NULL);
    return 0;
}

static void shm_close_screen(struct shm_screen *s) {
    XShmDetach(s->dpy, &s->shm);
    XDestroyImage(s->image);
    shmdt(s->shm.shmaddr);
    XFreeGC(s->dpy, s->gc);
    XDestroyWindow(s->dpy, s->win);
    XCloseDisplay(s->dpy);
}

static int play_once(struct shm_screen *s, const char *path, int scale, int untimed) {
    AVFormatContext *fmt = NULL;
    if (avformat_open_input(&fmt, path, NULL, NULL) < 0) {
        fprintf(stderr, "cannot open %s\n", path);
        return -1;
    }
    if (avformat_find_stream_info(fmt, NULL) < 0) {
        avformat_close_input(&fmt);
        return -1;
    }
    int idx = av_find_best_stream(fmt, AVMEDIA_TYPE_VIDEO, -1, -1, NULL, 0);
    if (idx < 0) {
        fprintf(stderr, "no video stream\n");
        avformat_close_input(&fmt);
        return -1;
    }

    AVStream *st = fmt->streams[idx];
    const AVCodec *codec = avcodec_find_decoder(st->codecpar->codec_id);
    AVCodecContext *dec = avcodec_alloc_context3(codec);
    avcodec_parameters_to_context(dec, st->codecpar);
    if (avcodec_open2(dec, codec, NULL) < 0) {
        fprintf(stderr, "decoder open failed\n");
        return -1;
    }

    int out_w = s->width, out_h = s->height;
    if (!scale) {
        out_w = dec->width;
        out_h = dec->height;
    }
    struct SwsContext *sws = sws_getContext(dec->width, dec->height, dec->pix_fmt,
                                            out_w, out_h, AV_PIX_FMT_BGR0,
                                            SWS_FAST_BILINEAR, NULL, NULL, NULL);
    if (!sws) {
        fprintf(stderr, "sws_getContext failed\n");
        return -1;
    }

    AVPacket *pkt = av_packet_alloc();
    AVFrame *frame = av_frame_alloc();
    double base = -1, play_start = now(), window_start = play_start;
    long frames = 0;
    double t_decode = 0, t_scale = 0, t_put = 0;

    for (;;) {
        int ret = av_read_frame(fmt, pkt);
        int draining = ret < 0;
        if (draining) {
            pkt->stream_index = -1;
        }
        if (!draining && pkt->stream_index != idx) {
            av_packet_unref(pkt);
            continue;
        }
        if (avcodec_send_packet(dec, draining ? NULL : pkt) < 0) {
            av_packet_unref(pkt);
            break;
        }
        av_packet_unref(pkt);
        for (;;) {
            double t0 = now();
            int r = avcodec_receive_frame(dec, frame);
            t_decode += now() - t0;
            if (r < 0) {
                break;
            }
            if (!untimed && frame->pts != AV_NOPTS_VALUE) {
                double pts = frame->pts * av_q2d(st->time_base);
                if (base < 0) {
                    base = pts;
                }
                double target = play_start + (pts - base);
                double wait = target - now();
                if (wait > 0.0005) {
                    usleep((useconds_t)(wait * 1e6));
                }
            }
            t0 = now();
            uint8_t *dst[4] = {s->shm.shmaddr, NULL, NULL, NULL};
            int dst_stride[4] = {s->image->bytes_per_line, 0, 0, 0};
            sws_scale(sws, (const uint8_t *const *)frame->data, frame->linesize, 0,
                      dec->height, dst, dst_stride);
            t_scale += now() - t0;
            t0 = now();
            XShmPutImage(s->dpy, s->win, s->gc, s->image, 0, 0, 0, 0, out_w, out_h,
                         True);
            XFlush(s->dpy);
            shm_wait_completion(s);
            t_put += now() - t0;
            frames++;
            double elapsed = now() - window_start;
            if (elapsed >= 2.0) {
                printf("frames=%ld fps=%.1f decode=%.1fms scale=%.1fms put=%.2fms\n",
                       frames, frames / elapsed, t_decode * 1e3 / frames,
                       t_scale * 1e3 / frames, t_put * 1e3 / frames);
                fflush(stdout);
                frames = 0;
                window_start = now();
                t_decode = t_scale = t_put = 0;
            }
            if (stop_requested) {
                break;
            }
            av_frame_unref(frame);
        }
        if (draining || stop_requested) {
            break;
        }
    }

    av_frame_free(&frame);
    av_packet_free(&pkt);
    sws_freeContext(sws);
    avcodec_free_context(&dec);
    avformat_close_input(&fmt);
    return 0;
}

int main(int argc, char **argv) {
    const char *path = NULL;
    int loop = 0, scale = 1, untimed = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--loop")) {
            loop = 1;
        } else if (!strcmp(argv[i], "--no-scale")) {
            scale = 0;
        } else if (!strcmp(argv[i], "--untimed")) {
            untimed = 1;
        } else {
            path = argv[i];
        }
    }
    if (!path) {
        fprintf(stderr, "usage: %s <file> [--loop] [--no-scale] [--untimed]\n", argv[0]);
        return 2;
    }
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    av_log_set_level(AV_LOG_ERROR);

    struct shm_screen screen;
    memset(&screen, 0, sizeof screen);
    if (shm_open_screen(&screen) < 0) {
        return 1;
    }
    printf("screen %dx%d\n", screen.width, screen.height);
    fflush(stdout);

    do {
        play_once(&screen, path, scale, untimed);
    } while (loop && !stop_requested);

    shm_close_screen(&screen);
    return 0;
}
