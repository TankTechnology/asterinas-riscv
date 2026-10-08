// SPDX-License-Identifier: MPL-2.0
/* Asterinas RISC-V interactive desktop demo.
 *
 * Runs as /init in the marker initramfs: initializes LVGL on /dev/fb0 and
 * presents a small keyboard-navigable desktop:
 *
 *   - Home screen with three app cards (System Info, Image Viewer, About)
 *   - Arrow keys move focus between widgets, Enter opens the focused app,
 *     ESC returns to Home.
 *
 * Built for riscv64, fully static. The image is embedded as raw BGRA8888 via
 * .incbin("asterinas.bgra") produced by build_lvgl_initramfs.sh.
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

/* Embedded BGRA8888 image (LV_COLOR_DEPTH 32 little-endian layout). */
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

static lv_indev_t *kb_indev;
static lv_group_t *home_group;
static lv_group_t *info_group;
static lv_group_t *image_group;
static lv_group_t *about_group;

static lv_obj_t *home_screen;
static lv_obj_t *info_screen;
static lv_obj_t *image_screen;
static lv_obj_t *about_screen;

/* -------------------------------------------------------------------------
 * Theme helpers: a small shared palette and common widgets.
 * ------------------------------------------------------------------------- */

#define COLOR_BG       lv_color_hex(0x101830)   /* deep navy background */
#define COLOR_ACCENT   lv_color_hex(0xff6f00)   /* orange accent */
#define COLOR_CARD     lv_color_hex(0x1a237e)   /* card indigo */
#define COLOR_TEXT     lv_color_hex(0xffffff)
#define COLOR_MUTED    lv_color_hex(0x90caf9)

static const lv_font_t *F_TITLE = &lv_font_montserrat_48;
static const lv_font_t *F_BTN   = &lv_font_montserrat_32;
static const lv_font_t *F_BODY  = &lv_font_montserrat_24;
static const lv_font_t *F_NOTE  = &lv_font_montserrat_16;

/* Highlight a focused card/button with an orange outline. */
static void apply_focus_style(lv_obj_t *obj) {
    static lv_style_t style_focus;
    lv_style_init(&style_focus);
    lv_style_set_border_width(&style_focus, 4);
    lv_style_set_border_color(&style_focus, COLOR_ACCENT);
    lv_style_set_border_opa(&style_focus, LV_OPA_COVER);
    lv_obj_add_style(obj, &style_focus, LV_STATE_FOCUSED);
}

/* A screen-wide title bar. Returns the content area below it. */
static lv_obj_t *add_title_bar(lv_obj_t *screen, const char *title, const char *subtitle) {
    lv_obj_t *bar = lv_obj_create(screen);
    lv_obj_set_size(bar, DISP_W, 120);
    lv_obj_set_pos(bar, 0, 0);
    lv_obj_set_style_bg_color(bar, COLOR_ACCENT, 0);
    lv_obj_set_style_radius(bar, 0, 0);
    lv_obj_set_style_border_width(bar, 0, 0);

    lv_obj_t *label = lv_label_create(bar);
    lv_label_set_text(label, title);
    lv_obj_set_style_text_font(label, F_TITLE, 0);
    lv_obj_set_style_text_color(label, COLOR_TEXT, 0);
    lv_obj_align(label, LV_ALIGN_LEFT_MID, 40, 0);

    if (subtitle) {
        lv_obj_t *sub = lv_label_create(bar);
        lv_label_set_text(sub, subtitle);
        lv_obj_set_style_text_font(sub, F_BODY, 0);
        lv_obj_set_style_text_color(sub, COLOR_MUTED, 0);
        lv_obj_align(sub, LV_ALIGN_LEFT_MID, 40, 40);
    }
    return bar;
}

/* A "Back" button in the top-right; ESC also triggers it. */
static lv_obj_t *add_back_button(lv_obj_t *screen, lv_group_t *group, lv_event_cb_t cb) {
    lv_obj_t *btn = lv_btn_create(screen);
    lv_obj_set_size(btn, 180, 70);
    lv_obj_align(btn, LV_ALIGN_TOP_RIGHT, -30, 25);
    lv_obj_set_style_bg_color(btn, COLOR_CARD, 0);
    apply_focus_style(btn);

    lv_obj_t *label = lv_label_create(btn);
    lv_label_set_text(label, LV_SYMBOL_LEFT " Back");
    lv_obj_set_style_text_font(label, F_BODY, 0);
    lv_obj_center(label);

    lv_obj_add_event_cb(btn, cb, LV_EVENT_CLICKED, NULL);
    lv_group_add_obj(group, btn);
    return btn;
}

/* -------------------------------------------------------------------------
 * Screen switching.
 * ------------------------------------------------------------------------- */

static void show_screen(lv_obj_t *screen, lv_group_t *group) {
    lv_scr_load(screen);
    lv_indev_set_group(kb_indev, group);
    /* If the group has no focus yet, focus its first object. */
    if (lv_group_get_focused(group) == NULL) {
        lv_group_focus_next(group);
    }
}

/* -------------------------------------------------------------------------
 * App screens.
 * ------------------------------------------------------------------------- */

static void back_handler(lv_event_t *e) {
    (void)e;
    show_screen(home_screen, home_group);
}

static void open_info(lv_event_t *e) {
    (void)e;
    show_screen(info_screen, info_group);
}
static void open_image(lv_event_t *e) {
    (void)e;
    show_screen(image_screen, image_group);
}
static void open_about(lv_event_t *e) {
    (void)e;
    show_screen(about_screen, about_group);
}

static void create_home_screen(void) {
    home_screen = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(home_screen, COLOR_BG, 0);
    home_group = lv_group_create();

    add_title_bar(home_screen, "Asterinas", "RISC-V Desktop Demo");

    /* Three app cards laid out horizontally. */
    struct { const char *sym; const char *name; lv_event_cb_t cb; } apps[3] = {
        { LV_SYMBOL_HOME,     "System Info", open_info  },
        { LV_SYMBOL_IMAGE,    "Image Viewer", open_image },
        { LV_SYMBOL_SETTINGS, "About",       open_about },
    };
    const int card_w = 340, card_h = 520, gap = 60;
    int total = 3 * card_w + 2 * gap;
    int x0 = (DISP_W - total) / 2;

    for (int i = 0; i < 3; i++) {
        lv_obj_t *card = lv_btn_create(home_screen);
        lv_obj_set_size(card, card_w, card_h);
        lv_obj_set_pos(card, x0 + i * (card_w + gap), 220);
        lv_obj_set_style_bg_color(card, COLOR_CARD, 0);
        lv_obj_set_style_radius(card, 16, 0);
        apply_focus_style(card);

        lv_obj_t *icon = lv_label_create(card);
        lv_label_set_text(icon, apps[i].sym);
        lv_obj_set_style_text_font(icon, F_TITLE, 0);
        lv_obj_set_style_text_color(icon, COLOR_ACCENT, 0);
        lv_obj_align(icon, LV_ALIGN_TOP_MID, 0, 40);

        lv_obj_t *name = lv_label_create(card);
        lv_label_set_text(name, apps[i].name);
        lv_obj_set_style_text_font(name, F_BTN, 0);
        lv_obj_set_style_text_color(name, COLOR_TEXT, 0);
        lv_obj_align(name, LV_ALIGN_BOTTOM_MID, 0, -40);

        lv_obj_add_event_cb(card, apps[i].cb, LV_EVENT_CLICKED, NULL);
        lv_group_add_obj(home_group, card);
    }

    lv_obj_t *hint = lv_label_create(home_screen);
    lv_label_set_text(hint, "Arrow keys: move   Enter: open   ESC: back");
    lv_obj_set_style_text_font(hint, F_NOTE, 0);
    lv_obj_set_style_text_color(hint, COLOR_MUTED, 0);
    lv_obj_align(hint, LV_ALIGN_BOTTOM_MID, 0, -30);

    lv_group_set_default(home_group);
}

static void create_info_screen(void) {
    info_screen = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(info_screen, COLOR_BG, 0);
    info_group = lv_group_create();

    add_title_bar(info_screen, "System Information", NULL);
    add_back_button(info_screen, info_group, back_handler);

    /* A card of key-value rows. */
    lv_obj_t *card = lv_obj_create(info_screen);
    lv_obj_set_size(card, 900, 620);
    lv_obj_align(card, LV_ALIGN_CENTER, 0, 80);
    lv_obj_set_style_bg_color(card, COLOR_CARD, 0);
    lv_obj_set_style_radius(card, 16, 0);
    lv_obj_set_style_border_width(card, 0, 0);

    static const char *rows[] = {
        "OS        Asterinas (RISC-V)",
        "Arch      riscv64, Sv39 paging",
        "Display   1280 x 1024, 32bpp",
        "Renderer  LVGL 8.3.9",
        "Input     virtio-keyboard (evdev)",
        "Backend   /dev/fb0 (simple-framebuffer)",
    };
    for (int i = 0; i < 6; i++) {
        lv_obj_t *row = lv_label_create(card);
        lv_label_set_text(row, rows[i]);
        lv_obj_set_style_text_font(row, F_BODY, 0);
        lv_obj_set_style_text_color(row, i % 2 ? COLOR_TEXT : COLOR_MUTED, 0);
        lv_obj_align(row, LV_ALIGN_TOP_LEFT, 40, 30 + i * 90);
    }
}

static void create_image_screen(void) {
    image_screen = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(image_screen, COLOR_BG, 0);
    image_group = lv_group_create();

    add_title_bar(image_screen, "Image Viewer", NULL);
    add_back_button(image_screen, image_group, back_handler);

    static lv_img_dsc_t img_dsc;
    img_dsc.header.always_zero = 0;
    img_dsc.header.w = IMG_W;
    img_dsc.header.h = IMG_H;
    img_dsc.header.cf = LV_IMG_CF_TRUE_COLOR;
    img_dsc.data_size = IMG_W * IMG_H * sizeof(lv_color_t);
    img_dsc.data = asterinas_image_bin;

    lv_obj_t *img = lv_img_create(image_screen);
    lv_img_set_src(img, &img_dsc);
    /* Scale the 1280x1024 image into a ~1000x780 viewport. */
    lv_img_set_zoom(img, 200);   /* 200/256 = 78% */
    lv_obj_center(img);
}

static void create_about_screen(void) {
    about_screen = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(about_screen, COLOR_BG, 0);
    about_group = lv_group_create();

    add_title_bar(about_screen, "About", NULL);
    add_back_button(about_screen, about_group, back_handler);

    lv_obj_t *card = lv_obj_create(about_screen);
    lv_obj_set_size(card, 900, 500);
    lv_obj_align(card, LV_ALIGN_CENTER, 0, 80);
    lv_obj_set_style_bg_color(card, COLOR_CARD, 0);
    lv_obj_set_style_radius(card, 16, 0);
    lv_obj_set_style_border_width(card, 0, 0);

    lv_obj_t *txt = lv_label_create(card);
    lv_label_set_text(txt,
        "Asterinas is a Linux-compatible OS kernel\n"
        "written in Rust using the framekernel\n"
        "architecture.\n\n"
        "This is a RISC-V desktop demo running on\n"
        "QEMU virt: bochs framebuffer ->\n"
        "simple-framebuffer -> LVGL on /dev/fb0.");
    lv_obj_set_style_text_font(txt, F_BODY, 0);
    lv_obj_set_style_text_color(txt, COLOR_TEXT, 0);
    lv_obj_align(txt, LV_ALIGN_TOP_LEFT, 40, 30);
}

/* -------------------------------------------------------------------------
 * Marker + main.
 * ------------------------------------------------------------------------- */

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

    fbdev_init();
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

    evdev_init();
    lv_indev_drv_t indev_drv;
    lv_indev_drv_init(&indev_drv);
    indev_drv.type = LV_INDEV_TYPE_POINTER;
    indev_drv.read_cb = evdev_read;
    kb_indev = lv_indev_drv_register(&indev_drv);

    create_home_screen();
    create_info_screen();
    create_image_screen();
    create_about_screen();

    show_screen(home_screen, home_group);

    while (1) {
        lv_timer_handler();
        usleep(5000);
    }
    return 0;
}
