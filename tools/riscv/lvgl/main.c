/* Asterinas RISC-V framebuffer image demo.
 *
 * Runs as /init in the marker initramfs: initializes LVGL on /dev/fb0,
 * displays an embedded 1280x1024 image, and prints the userspace marker to
 * /dev/ttyS0 so the U-Boot booti smoke test recognizes readiness.
 *
 * Built for riscv64, fully static (the initramfs has no shared libraries).
 *
 * The image is embedded as raw BGRA8888 via .incbin("asterinas.bgra"),
 * produced by build_lvgl_initramfs.sh from the input PNG.
 */
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include "lvgl.h"
#include "lv_drivers/display/fbdev.h"
#include "lv_drivers/indev/evdev.h"

#define DISP_W 1280
#define DISP_H 1024
#define IMG_W 1280
#define IMG_H 1024

/* Embedded BGRA8888 image (matches LV_COLOR_DEPTH 32 little-endian layout:
 * byte0=B, byte1=G, byte2=R, byte3=A) via .incbin. */
__asm__(
    ".section .rodata\n"
    ".balign 4\n"
    ".global asterinas_image_bin\n"
    "asterinas_image_bin:\n"
    ".incbin \"asterinas.bgra\"\n"
    ".global asterinas_image_bin_end\n"
    "asterinas_image_bin_end:\n"
    ".text\n");
extern const unsigned char asterinas_image_bin[];
extern const unsigned char asterinas_image_bin_end[];

/* Print the smoke-test marker so the booti flow reports userspace readiness. */
static void write_marker(void) {
    const char *marker = ">>> Hello from RISC-V userspace on Asterinas! <<<\n";
    int fd = open("/dev/ttyS0", O_WRONLY);
    if (fd >= 0) {
        ssize_t n = write(fd, marker, strlen(marker));
        (void)n;
        close(fd);
    }
}

int main(void) {
    write_marker();

    lv_init();

    /* Display driver: /dev/fb0 via lv_drivers' fbdev. */
    fbdev_init();

    /* Partial draw buffer sized for the DISPLAY, not the image. */
    static lv_disp_draw_buf_t draw_buf;
    static lv_color_t buf1[DISP_W * 200];
    lv_disp_draw_buf_init(&draw_buf, buf1, NULL, DISP_W * 200);

    static lv_disp_drv_t disp_drv;
    lv_disp_drv_init(&disp_drv);
    disp_drv.hor_res = DISP_W;
    disp_drv.ver_res = DISP_H;
    disp_drv.flush_cb = fbdev_flush;
    disp_drv.draw_buf = &draw_buf;
    lv_disp_drv_register(&disp_drv);

    /* Input device: evdev keyboard (virtio-keyboard -> /dev/input/eventN). */
    evdev_init();
    lv_indev_drv_t indev_drv;
    lv_indev_drv_init(&indev_drv);
    indev_drv.type = LV_INDEV_TYPE_KEYPAD;
    indev_drv.read_cb = evdev_read;
    lv_indev_drv_register(&indev_drv);

    /* Full-screen image. */
    static lv_img_dsc_t img_dsc;
    img_dsc.header.always_zero = 0;
    img_dsc.header.w = IMG_W;
    img_dsc.header.h = IMG_H;
    img_dsc.header.cf = LV_IMG_CF_TRUE_COLOR;
    img_dsc.data_size = IMG_W * IMG_H * sizeof(lv_color_t);
    img_dsc.data = asterinas_image_bin;

    lv_obj_t *scr = lv_scr_act();
    lv_obj_t *img = lv_img_create(scr);
    lv_img_set_src(img, &img_dsc);
    lv_obj_set_size(img, IMG_W, IMG_H);
    lv_obj_set_pos(img, 0, 0);

    while (1) {
        lv_timer_handler();
        usleep(5000);
    }
    return 0;
}
