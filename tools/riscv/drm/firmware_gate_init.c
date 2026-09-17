// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <linux/kd.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

/*
 * Minimal local subset of Linux v6.18 include/uapi/drm/drm.h and
 * include/uapi/drm/drm_mode.h. Keeping it local makes the static RISC-V probe
 * independent of target sysroot header packages; compile-time assertions below
 * pin the ABI layouts used by Asterinas.
 */
#define DRM_CAP_DUMB_BUFFER 1U
#define DRM_CAP_DUMB_PREFER_SHADOW 4U
#define DRM_CAP_CURSOR_WIDTH 8U
#define DRM_CAP_CURSOR_HEIGHT 9U
#define DRM_MODE_CONNECTED 1U
#define DRM_MODE_TYPE_PREFERRED 8U
#define DRM_FORMAT_XRGB8888 0x34325258U

#define MEGREZ_WIDTH 1920U
#define MEGREZ_HEIGHT 1080U
#define HDMI_OBSERVATION_SECONDS 2U

struct drm_version {
  int32_t version_major;
  int32_t version_minor;
  int32_t version_patchlevel;
  size_t name_len;
  uintptr_t name;
  size_t date_len;
  uintptr_t date;
  size_t desc_len;
  uintptr_t desc;
};

struct drm_get_cap {
  uint64_t capability;
  uint64_t value;
};

struct drm_mode_card_res {
  uint64_t fb_id_ptr;
  uint64_t crtc_id_ptr;
  uint64_t connector_id_ptr;
  uint64_t encoder_id_ptr;
  uint32_t count_fbs;
  uint32_t count_crtcs;
  uint32_t count_connectors;
  uint32_t count_encoders;
  uint32_t min_width;
  uint32_t max_width;
  uint32_t min_height;
  uint32_t max_height;
};

struct drm_mode_modeinfo {
  uint32_t clock;
  uint16_t hdisplay;
  uint16_t hsync_start;
  uint16_t hsync_end;
  uint16_t htotal;
  uint16_t hskew;
  uint16_t vdisplay;
  uint16_t vsync_start;
  uint16_t vsync_end;
  uint16_t vtotal;
  uint16_t vscan;
  uint32_t vrefresh;
  uint32_t flags;
  uint32_t type;
  char name[32];
};

struct drm_mode_get_connector {
  uint64_t encoders_ptr;
  uint64_t modes_ptr;
  uint64_t props_ptr;
  uint64_t prop_values_ptr;
  uint32_t count_modes;
  uint32_t count_props;
  uint32_t count_encoders;
  uint32_t encoder_id;
  uint32_t connector_id;
  uint32_t connector_type;
  uint32_t connector_type_id;
  uint32_t connection;
  uint32_t mm_width;
  uint32_t mm_height;
  uint32_t subpixel;
  uint32_t pad;
};

struct drm_mode_create_dumb {
  uint32_t height;
  uint32_t width;
  uint32_t bpp;
  uint32_t flags;
  uint32_t handle;
  uint32_t pitch;
  uint64_t size;
};

struct drm_mode_map_dumb {
  uint32_t handle;
  uint32_t pad;
  uint64_t offset;
};

struct drm_mode_destroy_dumb {
  uint32_t handle;
};

struct drm_mode_fb_cmd2 {
  uint32_t fb_id;
  uint32_t width;
  uint32_t height;
  uint32_t pixel_format;
  uint32_t flags;
  uint32_t handles[4];
  uint32_t pitches[4];
  uint32_t offsets[4];
  uint32_t pad;
  uint64_t modifier[4];
};

struct drm_mode_crtc {
  uint64_t set_connectors_ptr;
  uint32_t count_connectors;
  uint32_t crtc_id;
  uint32_t fb_id;
  uint32_t x;
  uint32_t y;
  uint32_t gamma_size;
  uint32_t mode_valid;
  struct drm_mode_modeinfo mode;
};

struct drm_mode_fb_dirty_cmd {
  uint32_t fb_id;
  uint32_t flags;
  uint32_t color;
  uint32_t num_clips;
  uint64_t clips_ptr;
};

struct drm_mode_crtc_page_flip {
  uint32_t crtc_id;
  uint32_t fb_id;
  uint32_t flags;
  uint32_t reserved;
  uint64_t user_data;
};

#define DRM_IOCTL_VERSION _IOWR('d', 0x00, struct drm_version)
#define DRM_IOCTL_GET_CAP _IOWR('d', 0x0c, struct drm_get_cap)
#define DRM_IOCTL_MODE_GETRESOURCES _IOWR('d', 0xa0, struct drm_mode_card_res)
#define DRM_IOCTL_MODE_SETCRTC _IOWR('d', 0xa2, struct drm_mode_crtc)
#define DRM_IOCTL_MODE_GETCONNECTOR                                            \
  _IOWR('d', 0xa7, struct drm_mode_get_connector)
#define DRM_IOCTL_MODE_RMFB _IOWR('d', 0xaf, uint32_t)
#define DRM_IOCTL_MODE_PAGE_FLIP                                               \
  _IOWR('d', 0xb0, struct drm_mode_crtc_page_flip)
#define DRM_IOCTL_MODE_DIRTYFB _IOWR('d', 0xb1, struct drm_mode_fb_dirty_cmd)
#define DRM_IOCTL_MODE_CREATE_DUMB _IOWR('d', 0xb2, struct drm_mode_create_dumb)
#define DRM_IOCTL_MODE_MAP_DUMB _IOWR('d', 0xb3, struct drm_mode_map_dumb)
#define DRM_IOCTL_MODE_DESTROY_DUMB                                            \
  _IOWR('d', 0xb4, struct drm_mode_destroy_dumb)
#define DRM_IOCTL_MODE_ADDFB2 _IOWR('d', 0xb8, struct drm_mode_fb_cmd2)

_Static_assert(sizeof(struct drm_version) == 64, "unexpected drm_version ABI");
_Static_assert(sizeof(struct drm_mode_card_res) == 64,
               "unexpected drm_mode_card_res ABI");
_Static_assert(sizeof(struct drm_mode_modeinfo) == 68,
               "unexpected drm_mode_modeinfo ABI");
_Static_assert(sizeof(struct drm_mode_fb_cmd2) == 104,
               "unexpected drm_mode_fb_cmd2 ABI");

typedef int (*gate_ioctl_fn)(void *context, unsigned long request,
                             void *argument);
typedef void *(*gate_map_fn)(void *context, size_t length, uint64_t offset);
typedef int (*gate_unmap_fn)(void *context, void *address, size_t length);
typedef int (*gate_render_node_fn)(void *context);

struct gate_operations {
  gate_ioctl_fn call;
  gate_map_fn map;
  gate_unmap_fn unmap;
  gate_render_node_fn render_node_exists;
};

struct dumb_buffer {
  uint32_t handle;
  uint32_t pitch;
  uint64_t size;
  uint64_t offset;
  uint32_t fb_id;
  uint8_t *pixels;
};

enum gate_stage {
  GATE_OK = 0,
  GATE_RENDER_NODE,
  GATE_VERSION,
  GATE_CAPABILITY,
  GATE_RESOURCES,
  GATE_CONNECTOR,
  GATE_CREATE_DUMB,
  GATE_MAP_DUMB,
  GATE_ADD_FB,
  GATE_VT_GRAPHICS,
  GATE_SET_CRTC,
  GATE_DIRTY_FB,
  GATE_PAGE_FLIP,
  GATE_CLEANUP,
};

enum gate_mode {
  GATE_MODE_SELF_TEST,
  GATE_MODE_PHYSICAL,
};

static const char *const stage_names[] = {
    [GATE_OK] = "ok",
    [GATE_RENDER_NODE] = "render-node",
    [GATE_VERSION] = "version",
    [GATE_CAPABILITY] = "capability",
    [GATE_RESOURCES] = "resources",
    [GATE_CONNECTOR] = "connector",
    [GATE_CREATE_DUMB] = "create-dumb",
    [GATE_MAP_DUMB] = "map-dumb",
    [GATE_ADD_FB] = "add-fb",
    [GATE_VT_GRAPHICS] = "vt-graphics",
    [GATE_SET_CRTC] = "set-crtc",
    [GATE_DIRTY_FB] = "dirty-fb",
    [GATE_PAGE_FLIP] = "page-flip",
    [GATE_CLEANUP] = "cleanup",
};

static bool graphics_owned;

static void serial_line(const char *line) {
  int fd = open("/dev/ttyS0", O_WRONLY | O_NOCTTY | O_CLOEXEC);
  if (fd >= 0) {
    dprintf(fd, "%s\n", line);
    close(fd);
  }
}

static void publish_marker(const char *marker) {
  if (!graphics_owned) {
    puts(marker);
    fflush(stdout);
  }
  serial_line(marker);
}

static int take_graphics_ownership(void) {
  int fd = open("/dev/tty0", O_RDWR | O_NOCTTY | O_CLOEXEC);
  if (fd < 0)
    return -1;
  int result = ioctl(fd, KDSETMODE, KD_GRAPHICS);
  int saved_errno = errno;
  close(fd);
  errno = saved_errno;
  if (result == 0)
    graphics_owned = true;
  return result;
}

static void pause_for_hdmi_observation(enum gate_mode gate_mode) {
  if (gate_mode == GATE_MODE_PHYSICAL)
    sleep(HDMI_OBSERVATION_SECONDS);
}

static int get_capability(const struct gate_operations *operations,
                          void *context, uint64_t capability,
                          uint64_t expected) {
  struct drm_get_cap request = {.capability = capability};
  return operations->call(context, DRM_IOCTL_GET_CAP, &request) == 0 &&
                 request.value == expected
             ? 0
             : -1;
}

static int create_buffer(const struct gate_operations *operations,
                         void *context, struct dumb_buffer *buffer) {
  struct drm_mode_create_dumb create = {
      .height = MEGREZ_HEIGHT,
      .width = MEGREZ_WIDTH,
      .bpp = 32,
  };
  if (operations->call(context, DRM_IOCTL_MODE_CREATE_DUMB, &create) != 0 ||
      create.handle == 0 || create.pitch < MEGREZ_WIDTH * 4U ||
      create.size < (uint64_t)create.pitch * MEGREZ_HEIGHT ||
      create.size > SIZE_MAX)
    return GATE_CREATE_DUMB;

  struct drm_mode_map_dumb map = {.handle = create.handle};
  if (operations->call(context, DRM_IOCTL_MODE_MAP_DUMB, &map) != 0)
    return GATE_MAP_DUMB;
  void *pixels = operations->map(context, (size_t)create.size, map.offset);
  if (pixels == MAP_FAILED || pixels == NULL)
    return GATE_MAP_DUMB;

  *buffer = (struct dumb_buffer){
      .handle = create.handle,
      .pitch = create.pitch,
      .size = create.size,
      .offset = map.offset,
      .pixels = pixels,
  };
  return GATE_OK;
}

static void paint_pattern(struct dumb_buffer *buffer, unsigned variant) {
  for (uint32_t y = 0; y < MEGREZ_HEIGHT; ++y) {
    uint32_t *row = (uint32_t *)(buffer->pixels + (size_t)y * buffer->pitch);
    for (uint32_t x = 0; x < MEGREZ_WIDTH; ++x) {
      uint32_t red = variant == 0 ? x * 255U / (MEGREZ_WIDTH - 1U)
                                  : y * 255U / (MEGREZ_HEIGHT - 1U);
      uint32_t green = variant == 0 ? y * 255U / (MEGREZ_HEIGHT - 1U)
                                    : x * 255U / (MEGREZ_WIDTH - 1U);
      uint32_t blue = ((x / 120U) ^ (y / 90U) ^ variant) & 1U ? 0x30U : 0xd0U;
      row[x] = (red << 16) | (green << 8) | blue;
    }
  }
}

static int add_framebuffer(const struct gate_operations *operations,
                           void *context, struct dumb_buffer *buffer) {
  struct drm_mode_fb_cmd2 request = {
      .width = MEGREZ_WIDTH,
      .height = MEGREZ_HEIGHT,
      .pixel_format = DRM_FORMAT_XRGB8888,
  };
  request.handles[0] = buffer->handle;
  request.pitches[0] = buffer->pitch;
  if (operations->call(context, DRM_IOCTL_MODE_ADDFB2, &request) != 0 ||
      request.fb_id == 0)
    return -1;
  buffer->fb_id = request.fb_id;
  return 0;
}

static int cleanup_probe(const struct gate_operations *operations,
                         void *context, uint32_t crtc_id,
                         struct dumb_buffer buffers[2]) {
  struct drm_mode_crtc disable = {.crtc_id = crtc_id};
  if (operations->call(context, DRM_IOCTL_MODE_SETCRTC, &disable) != 0)
    return -1;
  for (size_t index = 0; index < 2; ++index) {
    if (operations->call(context, DRM_IOCTL_MODE_RMFB,
                         (void *)(uintptr_t)buffers[index].fb_id) != 0 ||
        operations->unmap(context, buffers[index].pixels,
                          (size_t)buffers[index].size) != 0) {
      return -1;
    }
    struct drm_mode_destroy_dumb destroy = {.handle = buffers[index].handle};
    if (operations->call(context, DRM_IOCTL_MODE_DESTROY_DUMB, &destroy) != 0)
      return -1;
  }
  return 0;
}

static enum gate_stage run_probe(const struct gate_operations *operations,
                                 void *context, enum gate_mode gate_mode) {
  const bool physical = gate_mode == GATE_MODE_PHYSICAL;
  if (operations->render_node_exists(context) != 0)
    return GATE_RENDER_NODE;

  char driver[32] = {0};
  struct drm_version version = {0};
  if (operations->call(context, DRM_IOCTL_VERSION, &version) != 0 ||
      version.name_len != strlen("simpledrm"))
    return GATE_VERSION;
  version.name = (uintptr_t)driver;
  version.name_len = sizeof(driver) - 1;
  if (operations->call(context, DRM_IOCTL_VERSION, &version) != 0 ||
      version.name_len != strlen("simpledrm") ||
      strcmp(driver, "simpledrm") != 0)
    return GATE_VERSION;
  if (physical)
    publish_marker("DRM_FIRMWARE_VERSION driver=simpledrm render-node=absent");

  if (get_capability(operations, context, DRM_CAP_DUMB_BUFFER, 1) != 0 ||
      get_capability(operations, context, DRM_CAP_DUMB_PREFER_SHADOW, 1) != 0 ||
      get_capability(operations, context, DRM_CAP_CURSOR_WIDTH, 0) != 0 ||
      get_capability(operations, context, DRM_CAP_CURSOR_HEIGHT, 0) != 0)
    return GATE_CAPABILITY;
  if (physical)
    publish_marker("DRM_FIRMWARE_CAPS dumb=1 prefer-shadow=1 cursor=0x0");

  struct drm_mode_card_res resources = {0};
  if (operations->call(context, DRM_IOCTL_MODE_GETRESOURCES, &resources) != 0 ||
      resources.count_crtcs != 1 || resources.count_connectors != 1 ||
      resources.count_encoders != 1)
    return GATE_RESOURCES;
  uint32_t crtc_id = 0;
  uint32_t connector_id = 0;
  uint32_t encoder_id = 0;
  resources.crtc_id_ptr = (uintptr_t)&crtc_id;
  resources.connector_id_ptr = (uintptr_t)&connector_id;
  resources.encoder_id_ptr = (uintptr_t)&encoder_id;
  if (operations->call(context, DRM_IOCTL_MODE_GETRESOURCES, &resources) != 0 ||
      crtc_id == 0 || connector_id == 0 || encoder_id == 0)
    return GATE_RESOURCES;

  struct drm_mode_get_connector connector = {.connector_id = connector_id};
  if (operations->call(context, DRM_IOCTL_MODE_GETCONNECTOR, &connector) != 0 ||
      connector.count_modes != 1 || connector.count_encoders != 1 ||
      connector.connection != DRM_MODE_CONNECTED)
    return GATE_CONNECTOR;
  struct drm_mode_modeinfo mode = {0};
  uint32_t connector_encoder = 0;
  connector.modes_ptr = (uintptr_t)&mode;
  connector.encoders_ptr = (uintptr_t)&connector_encoder;
  if (operations->call(context, DRM_IOCTL_MODE_GETCONNECTOR, &connector) != 0 ||
      connector_encoder != encoder_id || mode.hdisplay != MEGREZ_WIDTH ||
      mode.vdisplay != MEGREZ_HEIGHT ||
      (mode.type & DRM_MODE_TYPE_PREFERRED) == 0)
    return GATE_CONNECTOR;
  if (physical)
    publish_marker(
        "DRM_FIRMWARE_MODE connector=connected mode=1920x1080 preferred=1");

  struct dumb_buffer buffers[2] = {0};
  for (size_t index = 0; index < 2; ++index) {
    enum gate_stage stage = create_buffer(operations, context, &buffers[index]);
    if (stage != GATE_OK)
      return stage;
    paint_pattern(&buffers[index], (unsigned)index);
    if (add_framebuffer(operations, context, &buffers[index]) != 0)
      return GATE_ADD_FB;
  }
  if (physical)
    publish_marker("DRM_FIRMWARE_DUMB buffers=2 format=XRGB8888 mmap=pass");

  if (physical && take_graphics_ownership() != 0)
    return GATE_VT_GRAPHICS;

  struct drm_mode_crtc set = {
      .set_connectors_ptr = (uintptr_t)&connector_id,
      .count_connectors = 1,
      .crtc_id = crtc_id,
      .fb_id = buffers[0].fb_id,
      .mode_valid = 1,
      .mode = mode,
  };
  if (operations->call(context, DRM_IOCTL_MODE_SETCRTC, &set) != 0)
    return GATE_SET_CRTC;
  if (physical)
    publish_marker("DRM_FIRMWARE_PRESENT stage=setcrtc pattern=A ioctl=pass");
  pause_for_hdmi_observation(gate_mode);

  struct drm_mode_crtc_page_flip flip = {
      .crtc_id = crtc_id,
      .fb_id = buffers[1].fb_id,
  };
  if (operations->call(context, DRM_IOCTL_MODE_PAGE_FLIP, &flip) != 0)
    return GATE_PAGE_FLIP;
  if (physical)
    publish_marker("DRM_FIRMWARE_PRESENT stage=page-flip pattern=B ioctl=pass");
  pause_for_hdmi_observation(gate_mode);

  paint_pattern(&buffers[1], 2);
  struct drm_mode_fb_dirty_cmd dirty = {.fb_id = buffers[1].fb_id};
  if (operations->call(context, DRM_IOCTL_MODE_DIRTYFB, &dirty) != 0)
    return GATE_DIRTY_FB;
  if (physical)
    publish_marker("DRM_FIRMWARE_PRESENT stage=dirtyfb pattern=C ioctl=pass");

  if (!physical && cleanup_probe(operations, context, crtc_id, buffers) != 0)
    return GATE_CLEANUP;
  return GATE_OK;
}

#ifdef DRM_FIRMWARE_GATE_SELF_TEST

enum fake_case {
  FAKE_VALID,
  FAKE_BAD_DRIVER,
  FAKE_RENDER_NODE,
  FAKE_BAD_CAP,
  FAKE_BAD_MODE,
  FAKE_IOCTL_ERROR,
};

struct fake_context {
  enum fake_case test_case;
  uint32_t next_handle;
  uint32_t next_fb;
  unsigned presentation_step;
};

static void fake_mode(struct drm_mode_modeinfo *mode, bool bad) {
  *mode = (struct drm_mode_modeinfo){
      .hdisplay = bad ? 1280 : MEGREZ_WIDTH,
      .vdisplay = bad ? 800 : MEGREZ_HEIGHT,
      .type = DRM_MODE_TYPE_PREFERRED,
  };
  strcpy(mode->name, bad ? "1280x800" : "1920x1080");
}

static int fake_ioctl(void *opaque, unsigned long request, void *argument) {
  struct fake_context *context = opaque;
  if (context->test_case == FAKE_IOCTL_ERROR &&
      request == DRM_IOCTL_MODE_SETCRTC)
    return -1;
  if (request == DRM_IOCTL_VERSION) {
    struct drm_version *version = argument;
    const char *name =
        context->test_case == FAKE_BAD_DRIVER ? "virtio_gpu" : "simpledrm";
    size_t capacity = version->name_len;
    version->name_len = strlen(name);
    if (version->name != 0 && capacity != 0) {
      size_t length = strlen(name) < capacity ? strlen(name) : capacity;
      memcpy((void *)version->name, name, length);
    }
    return 0;
  }
  if (request == DRM_IOCTL_GET_CAP) {
    struct drm_get_cap *capability = argument;
    if (capability->capability == DRM_CAP_DUMB_BUFFER)
      capability->value = 1;
    else if (capability->capability == DRM_CAP_DUMB_PREFER_SHADOW)
      capability->value = context->test_case == FAKE_BAD_CAP ? 0 : 1;
    else
      capability->value = 0;
    return 0;
  }
  if (request == DRM_IOCTL_MODE_GETRESOURCES) {
    struct drm_mode_card_res *resources = argument;
    resources->count_crtcs = 1;
    resources->count_connectors = 1;
    resources->count_encoders = 1;
    if (resources->crtc_id_ptr != 0)
      *(uint32_t *)(uintptr_t)resources->crtc_id_ptr = 1;
    if (resources->connector_id_ptr != 0)
      *(uint32_t *)(uintptr_t)resources->connector_id_ptr = 2;
    if (resources->encoder_id_ptr != 0)
      *(uint32_t *)(uintptr_t)resources->encoder_id_ptr = 3;
    return 0;
  }
  if (request == DRM_IOCTL_MODE_GETCONNECTOR) {
    struct drm_mode_get_connector *connector = argument;
    connector->count_modes = 1;
    connector->count_encoders = 1;
    connector->connection = DRM_MODE_CONNECTED;
    if (connector->modes_ptr != 0)
      fake_mode((void *)(uintptr_t)connector->modes_ptr,
                context->test_case == FAKE_BAD_MODE);
    if (connector->encoders_ptr != 0)
      *(uint32_t *)(uintptr_t)connector->encoders_ptr = 3;
    return 0;
  }
  if (request == DRM_IOCTL_MODE_CREATE_DUMB) {
    struct drm_mode_create_dumb *create = argument;
    create->handle = ++context->next_handle;
    create->pitch = MEGREZ_WIDTH * 4U;
    create->size = (uint64_t)create->pitch * MEGREZ_HEIGHT;
    return 0;
  }
  if (request == DRM_IOCTL_MODE_MAP_DUMB) {
    struct drm_mode_map_dumb *map = argument;
    map->offset = (uint64_t)map->handle << 24;
    return 0;
  }
  if (request == DRM_IOCTL_MODE_ADDFB2) {
    struct drm_mode_fb_cmd2 *framebuffer = argument;
    framebuffer->fb_id = ++context->next_fb;
    return 0;
  }
  if (request == DRM_IOCTL_MODE_SETCRTC) {
    const struct drm_mode_crtc *set = argument;
    if (set->fb_id == 0)
      return context->presentation_step == 3 ? 0 : -1;
    if (context->presentation_step != 0)
      return -1;
    context->presentation_step = 1;
    return 0;
  }
  if (request == DRM_IOCTL_MODE_PAGE_FLIP) {
    if (context->presentation_step != 1)
      return -1;
    context->presentation_step = 2;
    return 0;
  }
  if (request == DRM_IOCTL_MODE_DIRTYFB) {
    if (context->presentation_step != 2)
      return -1;
    context->presentation_step = 3;
    return 0;
  }
  if (request == DRM_IOCTL_MODE_RMFB || request == DRM_IOCTL_MODE_DESTROY_DUMB)
    return 0;
  return -1;
}

static void *fake_map(void *context, size_t length, uint64_t offset) {
  (void)context;
  (void)offset;
  return calloc(1, length);
}

static int fake_unmap(void *context, void *address, size_t length) {
  (void)context;
  (void)length;
  free(address);
  return 0;
}

static int fake_render_node_exists(void *opaque) {
  const struct fake_context *context = opaque;
  return context->test_case == FAKE_RENDER_NODE;
}

int main(int argc, char **argv) {
  if (argc != 2)
    return 2;
  static const struct {
    const char *name;
    enum fake_case test_case;
    enum gate_stage expected;
  } cases[] = {
      {"valid", FAKE_VALID, GATE_OK},
      {"bad-driver", FAKE_BAD_DRIVER, GATE_VERSION},
      {"render-node", FAKE_RENDER_NODE, GATE_RENDER_NODE},
      {"bad-cap", FAKE_BAD_CAP, GATE_CAPABILITY},
      {"bad-mode", FAKE_BAD_MODE, GATE_CONNECTOR},
      {"ioctl-error", FAKE_IOCTL_ERROR, GATE_SET_CRTC},
  };
  for (size_t index = 0; index < sizeof(cases) / sizeof(cases[0]); ++index) {
    if (strcmp(argv[1], cases[index].name) != 0)
      continue;
    struct fake_context context = {.test_case = cases[index].test_case};
    const struct gate_operations operations = {
        .call = fake_ioctl,
        .map = fake_map,
        .unmap = fake_unmap,
        .render_node_exists = fake_render_node_exists,
    };
    enum gate_stage stage =
        run_probe(&operations, &context, GATE_MODE_SELF_TEST);
    if (stage != cases[index].expected)
      return 1;
    printf("DRM_FIRMWARE_SELF_TEST PASS case=%s stage=%s\n", argv[1],
           stage_names[stage]);
    return 0;
  }
  return 2;
}

#else

struct real_context {
  int fd;
};

static int real_ioctl(void *opaque, unsigned long request, void *argument) {
  const struct real_context *context = opaque;
  return ioctl(context->fd, request, argument);
}

static void *real_map(void *opaque, size_t length, uint64_t offset) {
  const struct real_context *context = opaque;
  if (offset > INT64_MAX) {
    errno = EOVERFLOW;
    return MAP_FAILED;
  }
  return mmap(NULL, length, PROT_READ | PROT_WRITE, MAP_SHARED, context->fd,
              (off_t)offset);
}

static int real_unmap(void *context, void *address, size_t length) {
  (void)context;
  return munmap(address, length);
}

static int real_render_node_exists(void *context) {
  (void)context;
  struct stat metadata;
  if (stat("/dev/dri/renderD128", &metadata) == 0)
    return 1;
  return errno == ENOENT ? 0 : 1;
}

static _Noreturn void hold_forever(void) {
  for (;;) {
    if (pause() < 0 && errno == EINTR)
      continue;
  }
}

int main(void) {
  struct real_context context = {
      .fd = open("/dev/dri/card0", O_RDWR | O_CLOEXEC),
  };
  if (context.fd < 0) {
    char message[96];
    snprintf(message, sizeof(message),
             "DRM_FIRMWARE_FAIL stage=open-card0 errno=%d", errno);
    publish_marker(message);
    hold_forever();
  }
  const struct gate_operations operations = {
      .call = real_ioctl,
      .map = real_map,
      .unmap = real_unmap,
      .render_node_exists = real_render_node_exists,
  };
  enum gate_stage stage = run_probe(&operations, &context, GATE_MODE_PHYSICAL);
  if (stage != GATE_OK) {
    char message[96];
    snprintf(message, sizeof(message), "DRM_FIRMWARE_FAIL stage=%s errno=%d",
             stage_names[stage], errno);
    publish_marker(message);
    hold_forever();
  }
  publish_marker("ASTERINAS_DRM_FIRMWARE_R1_READY");
  hold_forever();
}

#endif
