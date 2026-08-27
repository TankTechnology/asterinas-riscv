// SPDX-License-Identifier: MPL-2.0

//! Virgl resource invariants and transfer-boundary validation.
//!
//! Wire values follow Mesa's Gallium resource contract and virglrenderer's
//! fixed ABI definitions:
//! <https://docs.mesa3d.org/gallium/resources.html> and
//! <https://gitlab.freedesktop.org/virgl/virglrenderer/-/blob/main/src/virgl_hw.h>.

use aster_virtio::device::gpu::Resource3dCreateParams;

use crate::prelude::*;

const PIPE_BUFFER: u32 = 0;
const PIPE_TEXTURE_1D: u32 = 1;
const PIPE_TEXTURE_2D: u32 = 2;
const PIPE_TEXTURE_3D: u32 = 3;
const PIPE_TEXTURE_CUBE: u32 = 4;
const PIPE_TEXTURE_RECT: u32 = 5;
const PIPE_TEXTURE_1D_ARRAY: u32 = 6;
const PIPE_TEXTURE_2D_ARRAY: u32 = 7;
const PIPE_TEXTURE_CUBE_ARRAY: u32 = 8;
const PIPE_MAX_TEXTURE_TYPES: u32 = 9;
const VIRGL_FORMAT_B8G8R8A8_UNORM: u32 = 1;
const VIRGL_FORMAT_B8G8R8X8_UNORM: u32 = 2;
const VIRGL_FORMAT_A8R8G8B8_UNORM: u32 = 3;
const VIRGL_FORMAT_X8R8G8B8_UNORM: u32 = 4;
const VIRGL_FORMAT_MAX: u32 = 482;
const VIRGL_BIND_VALID_MASK: u32 = (1 << 0)
    | (1 << 1)
    | (1 << 3)
    | (1 << 4)
    | (1 << 5)
    | (1 << 6)
    | (1 << 7)
    | (1 << 8)
    | (1 << 11)
    | (1 << 14)
    | (1 << 15)
    | (1 << 16)
    | (1 << 17)
    | (1 << 18)
    | (1 << 19)
    | (1 << 20)
    | (1 << 21)
    | (1 << 22)
    | (0xff << 24);
const VIRGL_RESOURCE_Y_0_TOP: u32 = 1 << 0;
const VIRGL_RESOURCE_FLAG_MAP_PERSISTENT: u32 = 1 << 1;
const VIRGL_RESOURCE_FLAG_MAP_COHERENT: u32 = 1 << 2;
const VIRGL_RESOURCE_VALID_FLAGS: u32 =
    VIRGL_RESOURCE_Y_0_TOP | VIRGL_RESOURCE_FLAG_MAP_PERSISTENT | VIRGL_RESOURCE_FLAG_MAP_COHERENT;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct LiveGemResource {
    pub(super) create: Resource3dCreateParams,
    pub(super) backing_size: u32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct Transfer3d {
    pub(super) x: u32,
    pub(super) y: u32,
    pub(super) z: u32,
    pub(super) width: u32,
    pub(super) height: u32,
    pub(super) depth: u32,
    pub(super) level: u32,
    pub(super) offset: u32,
}

pub(super) fn validate_create(params: &Resource3dCreateParams) -> Result<()> {
    if params.target >= PIPE_MAX_TEXTURE_TYPES {
        return_errno_with_message!(Errno::EINVAL, "unknown Gallium resource target");
    }
    if params.format == 0 || params.format >= VIRGL_FORMAT_MAX {
        return_errno_with_message!(Errno::EINVAL, "unknown virgl resource format");
    }
    if params.bind & !VIRGL_BIND_VALID_MASK != 0 {
        return_errno_with_message!(Errno::EINVAL, "unknown virgl resource bind flags");
    }
    if params.width == 0 || params.height == 0 || params.depth == 0 || params.array_size == 0 {
        return_errno_with_message!(Errno::EINVAL, "resource dimensions must be nonzero");
    }
    if params.height > u16::MAX.into()
        || params.depth > u16::MAX.into()
        || params.array_size > u16::MAX.into()
    {
        return_errno_with_message!(Errno::EINVAL, "resource dimensions exceed Gallium limits");
    }
    if params.flags & !VIRGL_RESOURCE_VALID_FLAGS != 0 {
        return_errno_with_message!(Errno::EINVAL, "unknown virtio-gpu resource flags");
    }
    if params.flags & VIRGL_RESOURCE_Y_0_TOP != 0
        && !matches!(params.target, PIPE_TEXTURE_2D | PIPE_TEXTURE_RECT)
    {
        return_errno_with_message!(Errno::EINVAL, "Y_0_TOP requires a 2D or rectangle texture");
    }
    if params.flags & VIRGL_RESOURCE_FLAG_MAP_COHERENT != 0
        && params.flags & VIRGL_RESOURCE_FLAG_MAP_PERSISTENT == 0
    {
        return_errno_with_message!(
            Errno::EINVAL,
            "coherent mapping requires persistent mapping"
        );
    }
    if params.last_level >= u32::BITS {
        return_errno_with_message!(Errno::EINVAL, "resource mip level is too large");
    }
    if params.nr_samples > 32 {
        return_errno_with_message!(Errno::EINVAL, "resource sample count is too large");
    }
    if params.nr_samples > 1 && params.last_level != 0 {
        return_errno_with_message!(Errno::EINVAL, "multisample resources cannot have mipmaps");
    }
    if params.nr_samples > 1 && !matches!(params.target, PIPE_TEXTURE_2D | PIPE_TEXTURE_2D_ARRAY) {
        return_errno_with_message!(Errno::EINVAL, "multisampling requires a 2D texture target");
    }

    match params.target {
        PIPE_BUFFER => {
            if params.height != 1
                || params.depth != 1
                || params.array_size != 1
                || params.last_level != 0
            {
                return_errno_with_message!(Errno::EINVAL, "invalid buffer resource geometry");
            }
        }
        PIPE_TEXTURE_1D => {
            if params.height != 1 || params.depth != 1 || params.array_size != 1 {
                return_errno_with_message!(Errno::EINVAL, "invalid 1D texture geometry");
            }
        }
        PIPE_TEXTURE_2D => {
            if params.depth != 1 || params.array_size != 1 {
                return_errno_with_message!(Errno::EINVAL, "invalid 2D texture geometry");
            }
        }
        PIPE_TEXTURE_3D => {
            if params.array_size != 1 {
                return_errno_with_message!(Errno::EINVAL, "invalid 3D texture array size");
            }
        }
        PIPE_TEXTURE_CUBE => {
            if params.width != params.height || params.depth != 1 || params.array_size != 6 {
                return_errno_with_message!(Errno::EINVAL, "invalid cube texture geometry");
            }
        }
        PIPE_TEXTURE_RECT => {
            if params.depth != 1 || params.array_size != 1 || params.last_level != 0 {
                return_errno_with_message!(Errno::EINVAL, "invalid rectangle texture geometry");
            }
        }
        PIPE_TEXTURE_1D_ARRAY => {
            if params.height != 1 || params.depth != 1 {
                return_errno_with_message!(Errno::EINVAL, "invalid 1D array texture geometry");
            }
        }
        PIPE_TEXTURE_2D_ARRAY => {
            if params.depth != 1 {
                return_errno_with_message!(Errno::EINVAL, "invalid 2D array texture geometry");
            }
        }
        PIPE_TEXTURE_CUBE_ARRAY => {
            if params.width != params.height
                || params.depth != 1
                || !params.array_size.is_multiple_of(6)
            {
                return_errno_with_message!(Errno::EINVAL, "invalid cube-array texture geometry");
            }
        }
        _ => unreachable!(),
    }

    let max_mip_dimension = match params.target {
        PIPE_TEXTURE_1D | PIPE_TEXTURE_1D_ARRAY => params.width,
        PIPE_TEXTURE_3D => params.width.max(params.height).max(params.depth),
        _ => params.width.max(params.height),
    };
    if params.last_level > max_mip_dimension.ilog2() {
        return_errno_with_message!(Errno::EINVAL, "mipmap chain exceeds resource dimensions");
    }
    Ok(())
}

impl LiveGemResource {
    pub(super) fn validate_transfer(self, transfer: Transfer3d) -> Result<()> {
        let params = self.create;
        if transfer.level > params.last_level {
            return_errno_with_message!(Errno::EINVAL, "transfer mip level is not present");
        }
        if transfer.offset >= self.backing_size {
            return_errno_with_message!(Errno::EINVAL, "transfer offset exceeds GEM backing");
        }

        let width = mip_dimension(params.width, transfer.level);
        let height = mip_dimension(params.height, transfer.level);
        validate_axis(
            transfer.x,
            transfer.width,
            width,
            "transfer exceeds resource width",
        )?;

        match params.target {
            PIPE_BUFFER => self.validate_buffer_transfer(transfer)?,
            PIPE_TEXTURE_1D => {
                if transfer.y != 0 || transfer.height != 1 || transfer.z != 0 || transfer.depth != 1
                {
                    return_errno_with_message!(Errno::EINVAL, "invalid 1D texture transfer box");
                }
            }
            PIPE_TEXTURE_1D_ARRAY => {
                if transfer.y != 0 || transfer.height != 1 {
                    return_errno_with_message!(Errno::EINVAL, "invalid 1D array transfer box");
                }
                validate_axis(
                    transfer.z,
                    transfer.depth,
                    params.array_size,
                    "transfer exceeds resource layers",
                )?;
            }
            PIPE_TEXTURE_2D | PIPE_TEXTURE_RECT => {
                validate_axis(
                    transfer.y,
                    transfer.height,
                    height,
                    "transfer exceeds resource height",
                )?;
                if transfer.z != 0 || transfer.depth != 1 {
                    return_errno_with_message!(Errno::EINVAL, "invalid 2D texture transfer box");
                }
            }
            PIPE_TEXTURE_3D => {
                validate_axis(
                    transfer.y,
                    transfer.height,
                    height,
                    "transfer exceeds resource height",
                )?;
                validate_axis(
                    transfer.z,
                    transfer.depth,
                    mip_dimension(params.depth, transfer.level),
                    "transfer exceeds resource depth",
                )?;
            }
            PIPE_TEXTURE_CUBE | PIPE_TEXTURE_2D_ARRAY | PIPE_TEXTURE_CUBE_ARRAY => {
                validate_axis(
                    transfer.y,
                    transfer.height,
                    height,
                    "transfer exceeds resource height",
                )?;
                validate_axis(
                    transfer.z,
                    transfer.depth,
                    params.array_size,
                    "transfer exceeds resource layers",
                )?;
            }
            _ => unreachable!(),
        }

        if params.target != PIPE_BUFFER
            && params.nr_samples <= 1
            && let Some(bytes_per_pixel) = linear_bytes_per_pixel(params.format)
        {
            self.validate_linear_transfer(transfer, bytes_per_pixel)?;
        }
        Ok(())
    }

    fn validate_buffer_transfer(self, transfer: Transfer3d) -> Result<()> {
        if transfer.y != 0 || transfer.height != 1 || transfer.z != 0 || transfer.depth != 1 {
            return_errno_with_message!(Errno::EINVAL, "invalid buffer transfer box");
        }
        let end = transfer
            .offset
            .checked_add(transfer.x)
            .and_then(|start| start.checked_add(transfer.width))
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "buffer transfer overflows"))?;
        if end > self.backing_size {
            return_errno_with_message!(Errno::EINVAL, "buffer transfer exceeds GEM backing");
        }
        Ok(())
    }

    /// Proves the final DMA byte for a known, tightly packed linear format.
    fn validate_linear_transfer(self, transfer: Transfer3d, bytes_per_pixel: u32) -> Result<()> {
        let level_width = mip_dimension(self.create.width, transfer.level);
        let level_height = mip_dimension(self.create.height, transfer.level);
        let row_stride = level_width
            .checked_mul(bytes_per_pixel)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "resource row stride overflows"))?;
        let layer_stride = row_stride
            .checked_mul(level_height)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "resource layer stride overflows"))?;
        let last_layer = transfer
            .z
            .checked_add(transfer.depth - 1)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "transfer layer range overflows"))?;
        let byte_end = last_layer
            .checked_mul(layer_stride)
            .and_then(|bytes| transfer.y.checked_mul(row_stride)?.checked_add(bytes))
            .and_then(|bytes| transfer.x.checked_mul(bytes_per_pixel)?.checked_add(bytes))
            .and_then(|bytes| {
                (transfer.height - 1)
                    .checked_mul(row_stride)?
                    .checked_add(bytes)
            })
            .and_then(|bytes| {
                transfer
                    .width
                    .checked_mul(bytes_per_pixel)?
                    .checked_add(bytes)
            })
            .and_then(|bytes| transfer.offset.checked_add(bytes))
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "transfer byte range overflows"))?;
        if byte_end > self.backing_size {
            return_errno_with_message!(Errno::EINVAL, "transfer exceeds GEM backing");
        }
        Ok(())
    }
}

fn linear_bytes_per_pixel(format: u32) -> Option<u32> {
    match format {
        VIRGL_FORMAT_B8G8R8A8_UNORM
        | VIRGL_FORMAT_B8G8R8X8_UNORM
        | VIRGL_FORMAT_A8R8G8B8_UNORM
        | VIRGL_FORMAT_X8R8G8B8_UNORM => Some(4),
        _ => None,
    }
}

fn mip_dimension(base: u32, level: u32) -> u32 {
    base.checked_shr(level).unwrap_or(0).max(1)
}

fn validate_axis(start: u32, length: u32, limit: u32, message: &'static str) -> Result<()> {
    if length == 0 || start.checked_add(length).is_none_or(|end| end > limit) {
        return_errno_with_message!(Errno::EINVAL, message);
    }
    Ok(())
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    fn texture_resource() -> LiveGemResource {
        LiveGemResource {
            create: Resource3dCreateParams {
                resource_id: 7,
                target: PIPE_TEXTURE_2D,
                format: 1,
                bind: 2,
                width: 64,
                height: 64,
                depth: 1,
                array_size: 1,
                last_level: 0,
                nr_samples: 0,
                flags: 0,
            },
            backing_size: 64 * 64 * 4,
        }
    }

    fn full_texture_transfer() -> Transfer3d {
        Transfer3d {
            x: 0,
            y: 0,
            z: 0,
            width: 64,
            height: 64,
            depth: 1,
            level: 0,
            offset: 0,
        }
    }

    #[ktest]
    fn drm_validation_resource_create_rejects_invalid_gallium_geometry() {
        let valid = texture_resource().create;
        assert!(validate_create(&valid).is_ok());

        let mut invalid = valid;
        invalid.target = PIPE_MAX_TEXTURE_TYPES;
        assert!(validate_create(&invalid).is_err());

        invalid = valid;
        invalid.width = 0;
        assert!(validate_create(&invalid).is_err());

        invalid = valid;
        invalid.format = VIRGL_FORMAT_MAX;
        assert!(validate_create(&invalid).is_err());

        invalid = valid;
        invalid.bind = 1 << 23;
        assert!(validate_create(&invalid).is_err());

        invalid = valid;
        invalid.last_level = 7;
        assert!(validate_create(&invalid).is_err());

        invalid = valid;
        invalid.target = PIPE_TEXTURE_CUBE;
        invalid.array_size = 5;
        assert!(validate_create(&invalid).is_err());
    }

    #[ktest]
    fn drm_validation_texture_transfer_is_bounded_by_geometry_and_backing() {
        let mut resource = texture_resource();
        resource.create.last_level = 1;
        let full = full_texture_transfer();
        assert!(resource.validate_transfer(full).is_ok());
        assert!(
            resource
                .validate_transfer(Transfer3d { offset: 1, ..full })
                .is_err()
        );
        assert!(
            resource
                .validate_transfer(Transfer3d {
                    x: 63,
                    width: 2,
                    ..full
                })
                .is_err()
        );
        assert!(
            resource
                .validate_transfer(Transfer3d {
                    width: 32,
                    height: 32,
                    level: 1,
                    ..full
                })
                .is_ok()
        );
        resource.backing_size = 1;
        assert!(
            resource
                .validate_transfer(Transfer3d {
                    width: 32,
                    height: 32,
                    level: 1,
                    ..full
                })
                .is_err()
        );
        assert!(
            resource
                .validate_transfer(Transfer3d { level: 2, ..full })
                .is_err()
        );
    }

    #[ktest]
    fn drm_validation_buffer_transfer_uses_byte_bounds() {
        let resource = LiveGemResource {
            create: Resource3dCreateParams {
                resource_id: 8,
                target: PIPE_BUFFER,
                format: 64,
                bind: 0,
                width: 4096,
                height: 1,
                depth: 1,
                array_size: 1,
                last_level: 0,
                nr_samples: 0,
                flags: 0,
            },
            backing_size: 4096,
        };
        let at_end = Transfer3d {
            x: 4000,
            y: 0,
            z: 0,
            width: 96,
            height: 1,
            depth: 1,
            level: 0,
            offset: 0,
        };
        assert!(resource.validate_transfer(at_end).is_ok());
        assert!(
            resource
                .validate_transfer(Transfer3d {
                    width: 97,
                    ..at_end
                })
                .is_err()
        );
    }
}
