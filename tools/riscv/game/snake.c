// SPDX-License-Identifier: MPL-2.0
//
// Snake game for the Asterinas RISC-V framebuffer chain.
//
// Renders directly to /dev/fb0 (1280x1024 x8r8g8b8) on a 40x32 cell grid and
// reads arrow keys from the evdev keyboard device. Only the changed cells are
// redrawn each tick (dirty rectangle), so the 2D CPU rendering stays fast.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <linux/kd.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#define DISP_W 1280
#define DISP_H 1024
#define CELL 32
#define GRID_W (DISP_W / CELL) /* 40 */
#define GRID_H (DISP_H / CELL) /* 32 */
#define MAX_SNAKE (GRID_W * GRID_H)

#define COLOR_BG 0x00101830u    /* deep navy (x8r8g8b8 -> B,G,R,X) */
#define COLOR_SNAKE 0x0030ff10u /* green */
#define COLOR_HEAD 0x0060ff20u  /* brighter green */
#define COLOR_FOOD 0x000000ffu  /* red */

static unsigned char *fb;
static const int fb_stride = DISP_W * 4;

static int snake_x[MAX_SNAKE], snake_y[MAX_SNAKE];
static int snake_len;
static int dir_x, dir_y;
static int food_x, food_y;

static void tty_log(const char *s) {
    int fd = open("/dev/ttyS0", O_WRONLY);
    if (fd >= 0) {
        write(fd, s, strlen(s));
        write(fd, "\n", 1);
        close(fd);
    }
}

static void fill_cell(int gx, int gy, uint32_t color) {
    int px = gx * CELL;
    int py = gy * CELL;
    for (int y = py; y < py + CELL; y++) {
        for (int x = px; x < px + CELL; x++) {
            unsigned char *p = fb + (size_t)y * fb_stride + (size_t)x * 4;
            p[0] = (unsigned char)(color >> 16) & 0xff; /* B */
            p[1] = (unsigned char)(color >> 8) & 0xff;  /* G */
            p[2] = (unsigned char)color & 0xff;         /* R */
            p[3] = 0;
        }
    }
}

static int on_snake(int x, int y) {
    for (int i = 0; i < snake_len; i++) {
        if (snake_x[i] == x && snake_y[i] == y) {
            return 1;
        }
    }
    return 0;
}

static void spawn_food(void) {
    do {
        food_x = rand() % GRID_W;
        food_y = rand() % GRID_H;
    } while (on_snake(food_x, food_y));
}

static void init_game(void) {
    snake_len = 3;
    for (int i = 0; i < snake_len; i++) {
        snake_x[i] = GRID_W / 2 - i;
        snake_y[i] = GRID_H / 2;
    }
    dir_x = 1;
    dir_y = 0;
    spawn_food();

    /* Clear the screen. */
    for (int y = 0; y < GRID_H; y++) {
        for (int x = 0; x < GRID_W; x++) {
            fill_cell(x, y, COLOR_BG);
        }
    }
    for (int i = 0; i < snake_len; i++) {
        fill_cell(snake_x[i], snake_y[i], i == 0 ? COLOR_HEAD : COLOR_SNAKE);
    }
    fill_cell(food_x, food_y, COLOR_FOOD);
    tty_log("snake: ready");
}

static int step(void) {
    int new_x = snake_x[0] + dir_x;
    int new_y = snake_y[0] + dir_y;

    /* Wall collision. */
    if (new_x < 0 || new_x >= GRID_W || new_y < 0 || new_y >= GRID_H) {
        return 0;
    }

    int ate = (new_x == food_x && new_y == food_y);

    /* Move: shift the body, place the new head. */
    int old_tail_x = snake_x[snake_len - 1];
    int old_tail_y = snake_y[snake_len - 1];
    for (int i = snake_len - 1; i > 0; i--) {
        snake_x[i] = snake_x[i - 1];
        snake_y[i] = snake_y[i - 1];
    }
    snake_x[0] = new_x;
    snake_y[0] = new_y;

    /* Self collision (check after moving). */
    for (int i = 1; i < snake_len; i++) {
        if (snake_x[i] == new_x && snake_y[i] == new_y) {
            return 0;
        }
    }

    if (ate) {
        if (snake_len < MAX_SNAKE) {
            snake_x[snake_len] = old_tail_x;
            snake_y[snake_len] = old_tail_y;
            snake_len++;
        }
        spawn_food();
    }

    /* Dirty redraw: new head, previous head, removed tail, food. */
    fill_cell(snake_x[0], snake_y[0], COLOR_HEAD);
    fill_cell(snake_x[1], snake_y[1], COLOR_SNAKE);
    if (!ate) {
        fill_cell(old_tail_x, old_tail_y, COLOR_BG);
    }
    fill_cell(food_x, food_y, COLOR_FOOD);
    return 1;
}

static int read_keyboard(int fd) {
    struct input_event ev;
    ssize_t n = read(fd, &ev, sizeof(ev));
    if (n != (ssize_t)sizeof(ev)) {
        return 0;
    }
    if (ev.type != EV_KEY || ev.value == 0) {
        return 0;
    }
    switch (ev.code) {
    case KEY_UP:
        if (dir_y == 0) { dir_x = 0; dir_y = -1; }
        return 1;
    case KEY_DOWN:
        if (dir_y == 0) { dir_x = 0; dir_y = 1; }
        return 1;
    case KEY_LEFT:
        if (dir_x == 0) { dir_x = -1; dir_y = 0; }
        return 1;
    case KEY_RIGHT:
        if (dir_x == 0) { dir_x = 1; dir_y = 0; }
        return 1;
    default:
        return 0;
    }
}

int main(void) {
    int marker_fd = open("/dev/ttyS0", O_WRONLY);
    if (marker_fd >= 0) {
        const char *marker = ">>> Hello from RISC-V userspace on Asterinas! <<<\n";
        write(marker_fd, marker, strlen(marker));
        close(marker_fd);
    }

    int fbfd = open("/dev/fb0", O_RDWR);
    if (fbfd < 0) {
        tty_log("snake: open /dev/fb0 failed");
        return 1;
    }
    fb = mmap(NULL, (size_t)DISP_W * DISP_H * 4, PROT_READ | PROT_WRITE, MAP_SHARED, fbfd, 0);
    if (fb == MAP_FAILED) {
        tty_log("snake: mmap fb0 failed");
        return 1;
    }
    close(fbfd);

    srand((unsigned)time(NULL));

    /* Put the VT console into graphics mode so it stops rendering text over
     * our framebuffer. */
    int vtfd = open("/dev/tty0", O_RDWR);
    if (vtfd < 0) {
        vtfd = open("/dev/console", O_RDWR);
    }
    if (vtfd >= 0) {
        if (ioctl(vtfd, KDSETMODE, KD_GRAPHICS) != 0) {
            char b[64];
            snprintf(b, sizeof b, "snake: KDSETMODE errno=%d", errno);
            tty_log(b);
        } else {
            tty_log("snake: KD_GRAPHICS set");
        }
        close(vtfd);
    } else {
        tty_log("snake: cannot open tty0/console");
    }

    /* The keyboard is /dev/input/event1 (tablet is event0). */
    int kbd = open("/dev/input/event1", O_RDONLY | O_NONBLOCK);
    if (kbd < 0) {
        kbd = open("/dev/input/event0", O_RDONLY | O_NONBLOCK);
    }

    init_game();

    for (;;) {
        /* Drain pending keyboard input. */
        if (kbd >= 0) {
            for (int i = 0; i < 16; i++) {
                if (!read_keyboard(kbd)) {
                    break;
                }
            }
        }
        if (!step()) {
            tty_log("snake: game over");
            /* Signal game over, then wait for R to restart. */
            for (int y = 0; y < GRID_H; y++) {
                for (int x = 0; x < GRID_W; x++) {
                    fill_cell(x, y, 0x00002080u); /* dim red */
                }
            }
            int restart = 0;
            while (!restart) {
                if (kbd >= 0) {
                    struct input_event ev;
                    ssize_t n = read(kbd, &ev, sizeof(ev));
                    if (n == (ssize_t)sizeof(ev) && ev.type == EV_KEY &&
                        ev.value == 1 && ev.code == KEY_R) {
                        restart = 1;
                    }
                }
                usleep(50000);
            }
            init_game();
            tty_log("snake: restart");
        }
        usleep(120000); /* ~8 ticks/second */
    }
    return 0;
}
