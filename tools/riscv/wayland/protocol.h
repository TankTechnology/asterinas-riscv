/* SPDX-License-Identifier: MPL-2.0
 *
 * Shared constants for the minimal Wayland demo.
 */
#ifndef WAYLAND_PROTOCOL_H
#define WAYLAND_PROTOCOL_H

/* Interface names as sent on the wire. */
#define WL_IFACE_DISPLAY "wl_display"
#define WL_IFACE_REGISTRY "wl_registry"
#define WL_IFACE_CALLBACK "wl_callback"
#define WL_IFACE_COMPOSITOR "wl_compositor"
#define WL_IFACE_SHM "wl_shm"
#define WL_IFACE_SHM_POOL "wl_shm_pool"
#define WL_IFACE_SURFACE "wl_surface"
#define WL_IFACE_BUFFER "wl_buffer"

/* wl_shm pixel formats. */
#define WL_SHM_FORMAT_XRGB8888 0
#define WL_SHM_FORMAT_ARGB8888 1

/* Object ids. wl_display is always id 1; the rest are demo-fixed. */
#define WL_DISPLAY_ID 1
#define OBJ_REGISTRY 2
#define OBJ_COMPOSITOR 3
#define OBJ_SHM 4
#define OBJ_SURFACE 5
#define OBJ_SHM_POOL 6
#define OBJ_BUFFER 7
#define OBJ_CALLBACK 8

/* Request opcodes. */
#define REQ_DISPLAY_SYNC 0
#define REQ_DISPLAY_GET_REGISTRY 1
#define REQ_REGISTRY_BIND 0
#define REQ_COMPOSITOR_CREATE_SURFACE 0
#define REQ_SHM_CREATE_POOL 0
#define REQ_SHM_POOL_CREATE_BUFFER 0
#define REQ_SURFACE_ATTACH 1
#define REQ_SURFACE_COMMIT 4

/* Event opcodes. */
#define EVT_CALLBACK_DONE 0
#define EVT_REGISTRY_GLOBAL 0

/* Display resolution (matches the bochs-display framebuffer). */
#define DISP_W 1280
#define DISP_H 1024

#endif /* WAYLAND_PROTOCOL_H */
