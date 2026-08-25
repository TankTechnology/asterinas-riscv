// SPDX-License-Identifier: MPL-2.0

typedef struct _XDisplay Display;

extern Display *XOpenDisplay(const char *display_name);
extern int XCloseDisplay(Display *display);
extern int puts(const char *string);

int main(void)
{
	Display *display = XOpenDisplay(0);

	if (display == 0)
		return 1;

	XCloseDisplay(display);
	puts("XFCE_DRM_X11_CONNECT_OK");
	return 0;
}
