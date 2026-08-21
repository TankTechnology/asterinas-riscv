/* SPDX-License-Identifier: MPL-2.0 */
/* Minimal GTK3 smoke app for the Asterinas riscv64 guest: a titled window
 * with a big label. Used by XFCE-M3 to isolate "GTK3 renders at all" from
 * the full xfce4-session stack. */
#include <gtk/gtk.h>

int main(int argc, char **argv) {
    gtk_init(&argc, &argv);
    GtkWidget *win = gtk_window_new(GTK_WINDOW_TOPLEVEL);
    gtk_window_set_title(GTK_WINDOW(win), "gtk3-hello");
    gtk_window_set_default_size(GTK_WINDOW(win), 400, 200);
    gtk_window_set_position(GTK_WINDOW(win), GTK_WIN_POS_CENTER);
    GtkWidget *label = gtk_label_new("GTK3 works on Asterinas riscv64");
    gtk_container_add(GTK_CONTAINER(win), label);
    g_signal_connect(win, "destroy", G_CALLBACK(gtk_main_quit), NULL);
    gtk_widget_show_all(win);
    gtk_main();
    return 0;
}
