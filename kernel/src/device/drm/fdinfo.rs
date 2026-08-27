// SPDX-License-Identifier: MPL-2.0

//! `/proc/<pid>/fdinfo/<fd>` diagnostics for DRM files.

use core::fmt::Formatter;

use super::{DRIVER_NAME, DriHandle};

pub(super) fn write(handle: &DriHandle, formatter: &mut Formatter<'_>) -> core::fmt::Result {
    let snapshot = handle.gpu_manager.resource_snapshot();
    writeln!(formatter, "drm-driver:\t{DRIVER_NAME}")?;
    writeln!(formatter, "drm-client-id:\t{}", handle.file_id)?;
    writeln!(
        formatter,
        "drm-device-dumb-pool-used-bytes:\t{}",
        snapshot.dumb_pool_used_bytes
    )?;
    writeln!(
        formatter,
        "drm-device-dumb-pool-high-water-bytes:\t{}",
        snapshot.dumb_pool_high_water_bytes
    )?;
    writeln!(
        formatter,
        "drm-device-dumb-pool-capacity-bytes:\t{}",
        snapshot.dumb_pool_capacity_bytes
    )?;
    writeln!(
        formatter,
        "drm-device-gem-objects:\t{}",
        snapshot.gem_objects
    )?;
    writeln!(
        formatter,
        "drm-device-gem-references:\t{}",
        snapshot.gem_references
    )?;
    writeln!(
        formatter,
        "drm-device-flink-names:\t{}",
        snapshot.flink_names
    )?;
    writeln!(
        formatter,
        "drm-device-host-resources:\t{}",
        snapshot.live_host_resources
    )?;
    writeln!(
        formatter,
        "drm-device-host-resources-cleanup-only:\t{}",
        snapshot.cleanup_only_host_resources
    )?;
    writeln!(
        formatter,
        "drm-device-resource-cleanup-pending:\t{}",
        snapshot.pending_resource_cleanup
    )?;
    writeln!(
        formatter,
        "drm-device-contexts:\t{}",
        snapshot.virgl_contexts
    )?;
    writeln!(
        formatter,
        "drm-device-context-attachments:\t{}",
        snapshot.context_attachments
    )?;
    writeln!(
        formatter,
        "drm-device-context-cleanup-pending:\t{}",
        snapshot.pending_context_cleanup
    )?;
    writeln!(
        formatter,
        "drm-device-fences-tracked:\t{}",
        snapshot.tracked_fences
    )?;
    writeln!(
        formatter,
        "drm-device-fence-associations:\t{}",
        snapshot.fence_associations
    )?;
    writeln!(
        formatter,
        "drm-device-backend-backing-owners:\t{}",
        snapshot.backend_backing_owners
    )?;
    writeln!(
        formatter,
        "drm-device-backend-cleanup-pending:\t{}",
        snapshot.backend_pending_cleanup
    )?;
    writeln!(
        formatter,
        "drm-device-scanout-resources:\t{}",
        snapshot.scanout_resources
    )?;
    writeln!(
        formatter,
        "drm-device-cursor-resources:\t{}",
        snapshot.cursor_resources
    )
}
