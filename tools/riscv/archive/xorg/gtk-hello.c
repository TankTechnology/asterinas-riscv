// SPDX-License-Identifier: MPL-2.0
//
// Minimal GTK2 application to smoke-test the cross-compiled GTK2 stack on the
// Asterinas RISC-V framebuffer. Retries X connection (Xorg takes a while to
// come up), then shows a titled window with a label and stays in gtk_main().
//
// Static link (from repo root):
//   export PKG_CONFIG_LIBDIR="$PWD/target/riscv-cross/usr/lib/pkgconfig:$PWD/target/riscv-cross/usr/share/pkgconfig"
//   riscv64-linux-gnu-gcc -static -O2 -o gtk-hello gtk-hello.c \
//       $(pkg-config --static --cflags --libs gtk+-2.0)

#include <gtk/gtk.h>
#include <unistd.h>

static void log_serial(const char *s) {
    /* best-effort; if /dev/ttyS0 is unavailable we silently skip */
    FILE *f = fopen("/dev/ttyS0", "w");
    if (f) {
        fprintf(f, "%s\n", s);
        fclose(f);
    }
}

int main(int argc, char *argv[]) {
    int attempts = 0;
    while (!gtk_init_check(&argc, &argv)) {
        if (++attempts > 120) {
            log_serial("gtk-hello: X never came up, giving up");
            return 1;
        }
        usleep(500000); /* 0.5 s */
    }
    log_serial("gtk-hello: connected to X, building window");

    GtkWidget *window = gtk_window_new(GTK_WINDOW_TOPLEVEL);
    gtk_window_set_title(GTK_WINDOW(window), "GTK2 on RISC-V");
    gtk_window_set_default_size(GTK_WINDOW(window), 520, 220);
    gtk_window_set_position(GTK_WINDOW(window), GTK_WIN_POS_CENTER);

    GtkWidget *vbox = gtk_vbox_new(FALSE, 12);
    gtk_container_set_border_width(GTK_CONTAINER(window), 16);
    gtk_container_add(GTK_CONTAINER(window), vbox);

    GtkWidget *label = gtk_label_new("Hello, RISC-V GTK2 desktop!");
    gtk_box_pack_start(GTK_BOX(vbox), label, TRUE, TRUE, 0);

    GtkWidget *button = gtk_button_new_with_label("A GTK2 button (click me)");
    gtk_box_pack_start(GTK_BOX(vbox), button, FALSE, FALSE, 0);

    gtk_widget_show_all(window);

    log_serial("gtk-hello: window mapped, entering main loop");
    gtk_main();
    return 0;
}
