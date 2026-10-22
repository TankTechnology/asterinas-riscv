// SPDX-License-Identifier: MPL-2.0

//! A DRM driver for the 2D command set of a virtio-gpu device.
//!
//! The driver implements the shared `aster-drm` device traits: dumb buffers
//! come from the DRM shmem GEM backend, and scanout is presented by creating a
//! virtio-gpu 2D resource whose backing is the GEM object's frames.

#![no_std]
#![deny(unsafe_code)]

extern crate alloc;

// Set this crate's log prefix for `ostd::log`.
macro_rules! __log_prefix {
    () => {
        "virtio-gpu: "
    };
}

use alloc::{collections::BTreeMap, sync::Arc, vec::Vec};

use aster_core::prelude::*;
use aster_drm::{
    device::{DrmDevice, DrmFeatures},
    gem::{object::DrmGemObject, shmem::DrmGemShmemBackend, DrmGemOps},
    kms::{
        display_info::{DrmDisplayInfo, SubpixelOrder},
        display_mode::DrmDisplayMode,
        objects::{
            builder::DrmKmsObjectStoreBuilder,
            connector::{
                DrmConnector, DrmConnectorProbeState, DrmConnectorStatus, DrmConnectorType,
            },
            encoder::DrmEncoderType,
            framebuffer::DrmFramebuffer,
            plane::DrmPlaneType,
            KmsObjectIndex,
        },
        pixel_format::DrmPixelFormat,
        DrmKmsOps, DrmModeConfig,
    },
    utils::{DrmRect, DrmSize},
};
use aster_virtio::device::gpu::{
    device::GpuDevice, VirtioGpuMemEntry, VirtioGpuRect, VIRTIO_GPU_FORMAT_B8G8R8X8_UNORM,
};
use component::{init_component, ComponentInitError};
use ostd::{
    mm::{HasPaddr, PAGE_SIZE},
    sync::Mutex,
};

// Mesa selects its DRI driver for a non-PCI device by `strcmp` against the
// name `DRM_IOCTL_VERSION` reports; the spelling must match its `virtio_gpu`
// descriptor (underscore), or the loader falls through to kmsro and GBM
// fails to create a device.
const DRIVER_NAME: &str = "virtio_gpu";
const DRIVER_DESC: &str = "DRM driver for the 2D command set of virtio-gpu";
const MODE_VREFRESH_HZ: u32 = 60;
const ASSUMED_DPI: u32 = 96;

/// A DRM device backed by one virtio-gpu device.
struct VirtioGpuDrm {
    gpu: Arc<GpuDevice>,
    features: DrmFeatures,
    mode_config: DrmModeConfig,
    /// Resource ids keyed by the address of the GEM object they back.
    resources: Mutex<BTreeMap<usize, u32>>,
    format: DrmPixelFormat,
}

#[init_component(process)]
fn init() -> Result<(), ComponentInitError> {
    let Some(gpu) = aster_virtio::device::gpu::first_device() else {
        ostd::debug!("no device found");
        return Ok(());
    };

    let (width, height) = gpu.dimensions();
    let driver = match VirtioGpuDrm::new(gpu, width, height) {
        Ok(driver) => driver,
        Err(err) => {
            ostd::warn!("failed to create the DRM device: {:?}", err);
            return Ok(());
        }
    };

    if let Err(err) = aster_drm::register_device(Arc::new(driver)) {
        ostd::warn!("failed to register the DRM device: {:?}", err);
    }

    Ok(())
}

impl core::fmt::Debug for VirtioGpuDrm {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("VirtioGpuDrm")
            .field("name", &DRIVER_NAME)
            .finish_non_exhaustive()
    }
}

impl VirtioGpuDrm {
    fn new(gpu: Arc<GpuDevice>, width: u32, height: u32) -> Result<Self> {
        let mut builder = DrmKmsObjectStoreBuilder::default();
        let pixel_format = DrmPixelFormat::XRGB8888;
        let primary = builder.add_plane(DrmPlaneType::Primary, alloc::vec![pixel_format])?;
        let crtc = builder.add_crtc(0, primary, None)?;
        let encoder = builder.add_encoder(DrmEncoderType::Virtual)?;
        let connector = builder.add_connector(DrmConnectorType::Virtual)?;
        builder.attach_plane_to_crtc(primary, crtc)?;
        builder.attach_encoder_to_crtc(encoder, crtc)?;
        builder.attach_connector_to_encoder(connector, encoder)?;
        let object_store = builder.build()?;

        let mode_config = DrmModeConfig::new(
            DrmSize::new(1, 1),
            DrmSize::new(width.max(1), height.max(1)),
            object_store,
        )?
        .set_preferred_dumb_buffer_depth(24);

        Ok(Self {
            gpu,
            features: DrmFeatures::empty(),
            mode_config,
            resources: Mutex::new(BTreeMap::new()),
            format: DrmPixelFormat::XRGB8888,
        })
    }

    fn shmem_backend(gem: &DrmGemObject) -> Result<&DrmGemShmemBackend> {
        gem.backend()
            .as_any()
            .downcast_ref::<DrmGemShmemBackend>()
            .ok_or_else(|| Error::with_message(Errno::EOPNOTSUPP, "unsupported GEM backend"))
    }

    fn vm_entries(gem: &DrmGemObject) -> Result<Vec<VirtioGpuMemEntry>> {
        let frames = Self::shmem_backend(gem)?.frames();
        let mut entries = Vec::with_capacity(frames.len());
        for frame in frames {
            entries.push(VirtioGpuMemEntry {
                addr: frame.paddr() as u64,
                length: PAGE_SIZE as u32,
                padding: 0,
            });
        }
        Ok(entries)
    }

    fn resource_for(&self, framebuffer: &DrmFramebuffer) -> Result<u32> {
        let gem = framebuffer.gem_object();
        let key = gem as *const DrmGemObject as usize;
        {
            let resources = self.resources.lock();
            if let Some(resource_id) = resources.get(&key) {
                return Ok(*resource_id);
            }
        }

        let size = framebuffer.size();
        let resource_id = self.gpu.reserve_resource_id();
        if self
            .gpu
            .resource_create_2d(
                resource_id,
                virtio_format(self.format),
                size.width(),
                size.height(),
            )
            .is_err()
        {
            return Err(Error::with_message(
                Errno::EIO,
                "virtio-gpu resource creation failed",
            ));
        }
        let entries = Self::vm_entries(gem)?;
        if self.gpu.attach_backing(resource_id, &entries).is_err() {
            let _ = self.gpu.resource_unref(resource_id);
            return Err(Error::with_message(
                Errno::EIO,
                "failed to attach the GEM pages to the virtio-gpu resource",
            ));
        }

        self.resources.lock().insert(key, resource_id);
        Ok(resource_id)
    }

    fn present_rect(
        &self,
        framebuffer: &DrmFramebuffer,
        resource_id: u32,
        source_rect: DrmRect,
    ) -> Result<()> {
        let bytes_per_pixel = framebuffer.pixel_format().bytes_per_pixel() as u64;
        let offset = framebuffer.offset() as u64
            + u64::from(source_rect.y()) * u64::from(framebuffer.pitch())
            + u64::from(source_rect.x()) * bytes_per_pixel;
        let rect = VirtioGpuRect {
            x: source_rect.x(),
            y: source_rect.y(),
            width: source_rect.width(),
            height: source_rect.height(),
        };
        self.gpu
            .transfer_to_host_2d(resource_id, rect, offset)
            .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu transfer failed"))?;
        self.gpu
            .flush(resource_id, rect)
            .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu flush failed"))
    }
}

fn virtio_format(_format: DrmPixelFormat) -> u32 {
    // The plane advertises only XRGB8888, which maps to the X8R8G8B8
    // (B8G8R8X8 in memory) 2D format.
    VIRTIO_GPU_FORMAT_B8G8R8X8_UNORM
}

impl DrmDevice for VirtioGpuDrm {
    fn name(&self) -> &str {
        DRIVER_NAME
    }

    fn desc(&self) -> &str {
        DRIVER_DESC
    }

    fn features(&self) -> DrmFeatures {
        self.features
    }

    fn as_gem_ops(&self) -> Option<&dyn DrmGemOps> {
        Some(self)
    }

    fn as_kms_ops(&self) -> Option<&dyn DrmKmsOps> {
        Some(self)
    }
}

impl DrmGemOps for VirtioGpuDrm {
    fn create_dumb(&self, size: usize) -> Result<Arc<DrmGemObject>> {
        DrmGemShmemBackend::new_object(size)
    }
}

impl DrmKmsOps for VirtioGpuDrm {
    fn mode_config(&self) -> &DrmModeConfig {
        &self.mode_config
    }

    fn probe_connector(&self, _connector: &DrmConnector) -> Result<DrmConnectorProbeState> {
        let (width, height) = self.gpu.dimensions();
        let resolution = DrmSize::new(width.max(1), height.max(1));
        let display_mode = DrmDisplayMode::from_size(resolution, MODE_VREFRESH_HZ)?;
        let display_info =
            DrmDisplayInfo::from_dpi(resolution, ASSUMED_DPI, SubpixelOrder::Unknown)?;
        Ok(DrmConnectorProbeState::new(
            DrmConnectorStatus::Connected,
            alloc::vec![display_mode],
            display_info,
        ))
    }

    fn set_crtc(
        &self,
        _crtc_index: KmsObjectIndex,
        framebuffer: Option<&DrmFramebuffer>,
        source_rect: DrmRect,
    ) -> Result<()> {
        let Some(framebuffer) = framebuffer else {
            return self
                .gpu
                .disable_scanout()
                .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu disable scanout failed"));
        };

        let size = framebuffer.size();
        let resource_id = self.resource_for(framebuffer)?;
        let scanout_rect = VirtioGpuRect {
            x: 0,
            y: 0,
            width: size.width(),
            height: size.height(),
        };
        self.gpu
            .set_scanout(resource_id, scanout_rect)
            .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu set scanout failed"))?;
        self.present_rect(framebuffer, resource_id, source_rect)
    }

    fn refresh_dirty_fb(
        &self,
        framebuffer: &DrmFramebuffer,
        source_rects: &[DrmRect],
    ) -> Result<()> {
        let resource_id = self.resource_for(framebuffer)?;
        for source_rect in source_rects {
            self.present_rect(framebuffer, resource_id, *source_rect)?;
        }
        Ok(())
    }
}
