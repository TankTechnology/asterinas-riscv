// SPDX-License-Identifier: MPL-2.0

//! Fixed-mode EIC7700 scanout address contract, kept independent of MMIO.

use core::ops::Range;

const DC_ADDRESS_ALIGNMENT: usize = 64;
const DC_PITCH_ALIGNMENT: usize = 128;

pub(super) fn checked_scanout_span(
    address: usize,
    object_size: usize,
    width: u32,
    height: u32,
    pitch: usize,
    mode_width: u32,
    mode_height: u32,
) -> Option<Range<usize>> {
    if width == 0
        || height == 0
        || (width, height) != (mode_width, mode_height)
        || address % DC_ADDRESS_ALIGNMENT != 0
        || pitch % DC_PITCH_ALIGNMENT != 0
        || pitch != (width as usize).checked_mul(4)?
    {
        return None;
    }
    let size = pitch.checked_mul(height as usize)?;
    let end = address.checked_add(size)?;
    if size > object_size || u32::try_from(address).is_err() || u32::try_from(end - 1).is_err() {
        return None;
    }
    Some(address..end)
}

#[cfg(any(test, ktest))]
mod tests {
    #[cfg(ktest)]
    use ostd::prelude::ktest;

    use super::*;

    #[cfg_attr(test, test)]
    #[cfg_attr(ktest, ktest)]
    fn accepts_the_megrez_mode_and_contiguous_dma_span() {
        assert_eq!(
            checked_scanout_span(0xf800_0000, 8_294_400, 1920, 1080, 7680, 1920, 1080),
            Some(0xf800_0000..0xf87e_9000)
        );
    }

    #[cfg_attr(test, test)]
    #[cfg_attr(ktest, ktest)]
    fn rejects_bad_mode_stride_extent_and_address() {
        assert!(
            checked_scanout_span(0xf800_0000, 8_294_400, 1280, 1080, 7680, 1920, 1080).is_none()
        );
        assert!(
            checked_scanout_span(0xf800_0000, 8_294_400, 1920, 1080, 5120, 1920, 1080).is_none()
        );
        assert!(
            checked_scanout_span(0xf800_0000, 8_294_399, 1920, 1080, 7680, 1920, 1080).is_none()
        );
        assert!(
            checked_scanout_span(0xffff_ffc0, 8_294_400, 1920, 1080, 7680, 1920, 1080).is_none()
        );
        assert!(
            checked_scanout_span(0xf800_0004, 8_294_400, 1920, 1080, 7680, 1920, 1080).is_none()
        );
    }
}
