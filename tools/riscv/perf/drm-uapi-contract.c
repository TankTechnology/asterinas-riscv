/*
 * Print the authoritative DRM uapi contract for the ioctls Asterinas implements.
 *
 * The point of this program is that it does not *state* the expected ioctl
 * numbers or struct sizes — it asks the real Linux uapi headers for them, by
 * including <drm/drm.h> and <drm/virtgpu_drm.h> and evaluating the
 * DRM_IOCTL_* / sizeof() expressions the headers define.
 *
 * That matters because of a defect class that has now cost this tree three
 * separate debugging cycles: `drm_prime_handle` declared with the wrong size,
 * `drm_virtgpu_execbuffer` likewise, and DRM_IOCTL_GET_MAGIC / AUTH_MAGIC with
 * the wrong direction bits. An ioctl command number is not a name — it is
 * direction | size | type | number packed into 32 bits, so a wrong size or
 * direction produces a *different ioctl*, which the kernel answers with ENOTTY.
 * That is indistinguishable from "not implemented", and it is what made the
 * second DRI3Open defect look like a missing feature for a whole cycle.
 *
 * Usage:  cc -o drm-uapi-contract drm-uapi-contract.c && ./drm-uapi-contract
 * Output: one line per ioctl, "<rust_type> <ioctl_number_hex> <struct_size>",
 *         where struct_size is 0 for ioctls that carry no data.
 *
 * The companion test tools/riscv/tests/test_drm_uapi_contract.py runs this and
 * compares the result against the numbers pinned in the kernel-side contract
 * test, so the pinned constants cannot silently drift from the uapi.
 */

#include <stdio.h>
#include <stdint.h>
#include <drm/drm.h>
#include <drm/virtgpu_drm.h>

/* One row per ioctl the driver declares. The name on the left is the Rust type
 * alias in kernel/src/device/dri.rs; everything on the right comes from the
 * headers above and from nothing else. */
#define ROW(rust_name, ioctl, type) \
	printf("%-28s 0x%08x %zu\n", rust_name, (unsigned)(ioctl), sizeof(type))

/* For ioctls that carry no data at all. */
#define ROW_NODATA(rust_name, ioctl) \
	printf("%-28s 0x%08x 0\n", rust_name, (unsigned)(ioctl))

int main(void)
{
	/* --- core drm.h ------------------------------------------------ */
	ROW("GetVersion",              DRM_IOCTL_VERSION,               struct drm_version);
	ROW("GetCap",                  DRM_IOCTL_GET_CAP,               struct drm_get_cap);
	ROW("GetMagic",                DRM_IOCTL_GET_MAGIC,             struct drm_auth);
	ROW("AuthMagic",               DRM_IOCTL_AUTH_MAGIC,            struct drm_auth);
	ROW("SetClientCap",            DRM_IOCTL_SET_CLIENT_CAP,        struct drm_set_client_cap);
	ROW("GemClose",                DRM_IOCTL_GEM_CLOSE,             struct drm_gem_close);
	ROW("GemFlink",                DRM_IOCTL_GEM_FLINK,             struct drm_gem_flink);
	ROW("GemOpen",                 DRM_IOCTL_GEM_OPEN,              struct drm_gem_open);
	ROW("PrimeHandleToFd",         DRM_IOCTL_PRIME_HANDLE_TO_FD,    struct drm_prime_handle);
	ROW("PrimeFdToHandle",         DRM_IOCTL_PRIME_FD_TO_HANDLE,    struct drm_prime_handle);
	ROW_NODATA("SetMaster",        DRM_IOCTL_SET_MASTER);
	ROW_NODATA("DropMaster",       DRM_IOCTL_DROP_MASTER);

	/* --- virtgpu_drm.h --------------------------------------------- */
	ROW("VirtgpuMap",              DRM_IOCTL_VIRTGPU_MAP,           struct drm_virtgpu_map);
	ROW("VirtgpuExecbuffer",       DRM_IOCTL_VIRTGPU_EXECBUFFER,    struct drm_virtgpu_execbuffer);
	ROW("VirtgpuGetparam",         DRM_IOCTL_VIRTGPU_GETPARAM,      struct drm_virtgpu_getparam);
	ROW("VirtgpuResourceCreate",   DRM_IOCTL_VIRTGPU_RESOURCE_CREATE, struct drm_virtgpu_resource_create);
	ROW("VirtgpuResourceInfo",     DRM_IOCTL_VIRTGPU_RESOURCE_INFO, struct drm_virtgpu_resource_info);
	ROW("VirtgpuGetCaps",          DRM_IOCTL_VIRTGPU_GET_CAPS,      struct drm_virtgpu_get_caps);
	ROW("VirtgpuTransferFromHost", DRM_IOCTL_VIRTGPU_TRANSFER_FROM_HOST, struct drm_virtgpu_3d_transfer_from_host);
	ROW("VirtgpuTransferToHost",   DRM_IOCTL_VIRTGPU_TRANSFER_TO_HOST,   struct drm_virtgpu_3d_transfer_to_host);
	ROW("VirtgpuWait",             DRM_IOCTL_VIRTGPU_WAIT,          struct drm_virtgpu_3d_wait);
	ROW("VirtgpuContextInit",      DRM_IOCTL_VIRTGPU_CONTEXT_INIT,  struct drm_virtgpu_context_init);

	/* --- mode setting, drm_mode.h via drm.h ------------------------ */
	ROW("ModeGetResources",        DRM_IOCTL_MODE_GETRESOURCES,     struct drm_mode_card_res);
	ROW("ModeGetCrtc",             DRM_IOCTL_MODE_GETCRTC,          struct drm_mode_crtc);
	ROW("ModeSetCrtc",             DRM_IOCTL_MODE_SETCRTC,          struct drm_mode_crtc);
	ROW("ModeCursor",              DRM_IOCTL_MODE_CURSOR,           struct drm_mode_cursor);
	ROW("ModeGetEncoder",          DRM_IOCTL_MODE_GETENCODER,       struct drm_mode_get_encoder);
	ROW("ModeGetConnector",        DRM_IOCTL_MODE_GETCONNECTOR,     struct drm_mode_get_connector);
	ROW("ModeAddFb",               DRM_IOCTL_MODE_ADDFB,            struct drm_mode_fb_cmd);
	ROW("ModePageFlip",            DRM_IOCTL_MODE_PAGE_FLIP,        struct drm_mode_crtc_page_flip);
	ROW("ModeDirtyFb",             DRM_IOCTL_MODE_DIRTYFB,          struct drm_mode_fb_dirty_cmd);
	ROW("ModeCreateDumb",          DRM_IOCTL_MODE_CREATE_DUMB,      struct drm_mode_create_dumb);
	ROW("ModeMapDumb",             DRM_IOCTL_MODE_MAP_DUMB,         struct drm_mode_map_dumb);
	ROW("ModeDestroyDumb",         DRM_IOCTL_MODE_DESTROY_DUMB,     struct drm_mode_destroy_dumb);
	ROW("ModeObjGetProperties",    DRM_IOCTL_MODE_OBJ_GETPROPERTIES, struct drm_mode_obj_get_properties);
	ROW("ModeGetPlaneResources",   DRM_IOCTL_MODE_GETPLANERESOURCES, struct drm_mode_get_plane_res);
	ROW("ModeGetPlane",            DRM_IOCTL_MODE_GETPLANE,         struct drm_mode_get_plane);
	ROW("ModeCursor2",             DRM_IOCTL_MODE_CURSOR2,          struct drm_mode_cursor2);

	return 0;
}
