// SPDX-License-Identifier: MPL-2.0

use alloc::vec;

use crate::{
    mm::{
        FrameAllocOptions, PAGE_SIZE, VmReader, VmWriter,
        dma::*,
        io::{VmIo, VmIoOnce, util::HasVmReaderWriter},
    },
    prelude::*,
};

mod dma_coherent {
    use super::*;

    #[ktest]
    fn alloc_with_coherent_device() {
        let dma_coherent = DmaCoherent::alloc(1, true).unwrap();
        assert_eq!(dma_coherent.size(), PAGE_SIZE);
    }

    #[ktest]
    fn read_write() {
        let dma_coherent = DmaCoherent::alloc(2, false).unwrap();
        assert_eq!(dma_coherent.size(), 2 * PAGE_SIZE);

        let buf_write = vec![1u8; 2 * PAGE_SIZE];
        dma_coherent.write_bytes(0, &buf_write).unwrap();
        let mut buf_read = vec![0u8; 2 * PAGE_SIZE];
        dma_coherent.read_bytes(0, &mut buf_read).unwrap();
        assert_eq!(buf_write, buf_read);
    }

    #[ktest]
    fn read_write_once() {
        let dma_coherent = DmaCoherent::alloc(2, false).unwrap();

        let buf_write = 1u64;
        dma_coherent.write_once(0, &buf_write).unwrap();
        let buf_read: u64 = dma_coherent.read_once(0).unwrap();
        assert_eq!(buf_read, buf_write);
    }

    #[ktest]
    fn reader_writer() {
        let dma_coherent = DmaCoherent::alloc(2, false).unwrap();

        let buf_write = vec![1u8; PAGE_SIZE];
        let mut writer = dma_coherent.writer();
        writer.write(&mut buf_write.as_slice().into());
        writer.write(&mut buf_write.as_slice().into());

        let mut buf_read = vec![0u8; 2 * PAGE_SIZE];
        let buf_write = vec![1u8; 2 * PAGE_SIZE];
        let mut reader = dma_coherent.reader();
        reader.read(&mut buf_read.as_mut_slice().into());
        assert_eq!(buf_read, buf_write);
    }

    #[ktest]
    fn zero_length_operations() {
        let dma_coherent = DmaCoherent::alloc(1, false).unwrap();

        // Zero-length read/write should succeed
        let empty_buf = [];
        dma_coherent.write_bytes(0, &empty_buf).unwrap();
        let mut empty_buf = [];
        dma_coherent.read_bytes(0, &mut empty_buf).unwrap();
    }

    #[ktest]
    fn complex_read_write_patterns() {
        let dma_coherent = DmaCoherent::alloc(4, false).unwrap();

        // Test alternating pattern
        let pattern1 = vec![0xAAu8; PAGE_SIZE];
        let pattern2 = vec![0x55u8; PAGE_SIZE];
        dma_coherent.write_bytes(0, &pattern1).unwrap();
        dma_coherent.write_bytes(PAGE_SIZE, &pattern2).unwrap();
        dma_coherent.write_bytes(2 * PAGE_SIZE, &pattern1).unwrap();
        dma_coherent.write_bytes(3 * PAGE_SIZE, &pattern2).unwrap();

        let mut read_buf = vec![0u8; 4 * PAGE_SIZE];
        dma_coherent.read_bytes(0, &mut read_buf).unwrap();
        assert_eq!(&read_buf[0..PAGE_SIZE], &pattern1[..]);
        assert_eq!(&read_buf[PAGE_SIZE..2 * PAGE_SIZE], &pattern2[..]);
        assert_eq!(&read_buf[2 * PAGE_SIZE..3 * PAGE_SIZE], &pattern1[..]);
        assert_eq!(&read_buf[3 * PAGE_SIZE..4 * PAGE_SIZE], &pattern2[..]);
    }

    #[ktest]
    fn alloc_uninit_dma_coherent() {
        let dma_coherent = DmaCoherent::alloc_uninit(1, false).unwrap();
        assert_eq!(dma_coherent.size(), PAGE_SIZE);

        let buf_write = vec![0xCDu8; PAGE_SIZE];
        dma_coherent.write_bytes(0, &buf_write).unwrap();

        let mut buf_read = vec![0u8; PAGE_SIZE];
        dma_coherent.read_bytes(0, &mut buf_read).unwrap();
        assert_eq!(buf_write, buf_read);
    }
}

mod dma_window {
    use super::*;

    #[ktest]
    fn translates_megrez_cpu_addresses() {
        let window = DmaWindow::new(0, 0xc000_0000, 0x200_0000_0000).unwrap();

        assert_eq!(window.translate(0xc000_0000..0xc000_1000), Some(0..0x1000));
        assert_eq!(
            window.translate(0x1_c000_0000..0x1_c000_2000),
            Some(0x1_0000_0000..0x1_0000_2000)
        );
    }

    #[ktest]
    fn rejects_invalid_dma_windows() {
        assert_eq!(DmaWindow::new(0, 0xc000_0000, 0), None);
        assert_eq!(DmaWindow::new(usize::MAX, 0, 1), None);
        assert_eq!(DmaWindow::new(0, usize::MAX, 1), None);
    }

    #[ktest]
    fn rejects_ranges_outside_dma_window() {
        let window = DmaWindow::new(0, 0xc000_0000, 0x2000).unwrap();

        assert_eq!(window.translate(0xbfff_f000..0xc000_0000), None);
        assert_eq!(window.translate(0xc000_0000..0xc000_0000), None);
        assert_eq!(window.translate(0xc000_1000..0xc000_3000), None);
        assert_eq!(window.translate(0xc000_1000..0xc000_0800), None);
    }
}

#[cfg(target_arch = "riscv64")]
mod usb_kernel_op {
    use core::{alloc::Layout, num::NonZeroUsize, ops::Range, ptr::NonNull};

    use dma_api::{DmaConstraints, DmaDirection as ApiDirection, DmaOp};

    use super::*;
    use crate::mm::dma::usb_kernel_op::eic7700_uncached_alias_range;

    fn identity_window() -> DmaWindow {
        DmaWindow::new(0, 0, usize::MAX).unwrap()
    }

    #[ktest]
    fn derives_eic7700_uncached_alias_from_cpu_address() {
        let window = DmaWindow::new(0, 0xc000_0000, 0x200_0000_0000).unwrap();

        assert_eq!(
            eic7700_uncached_alias_range(&window, 0x2_a287_f000..0x2_a288_0000,),
            Some(0xc2_2287_f000..0xc2_2288_0000)
        );

        let identity = DmaWindow::new(0x8000_0000, 0x8000_0000, 0x4_0000_0000).unwrap();
        assert_eq!(
            eic7700_uncached_alias_range(&identity, 0x2_a287_f000..0x2_a288_0000,),
            Some(0xc2_2287_f000..0xc2_2288_0000)
        );
    }

    #[ktest]
    fn rejects_dma_windows_and_ranges_without_an_eic7700_alias() {
        let identity = identity_window();
        assert_eq!(
            eic7700_uncached_alias_range(&identity, 0x1000..0x2000),
            None
        );

        let window = DmaWindow::new(0, 0xc000_0000, 0x200_0000_0000).unwrap();
        for range in [
            Range { start: 1, end: 0 },
            Range {
                start: 0x4000_0000,
                end: 0x4000_1000,
            },
            Range {
                start: 0x10_8000_0000,
                end: 0x10_8000_1000,
            },
            Range {
                start: usize::MAX - 0xfff,
                end: usize::MAX,
            },
        ] {
            assert_eq!(eic7700_uncached_alias_range(&window, range), None);
        }
    }

    #[ktest]
    fn allocates_and_releases_uncached_dma_memory() {
        let op = UsbKernelOp::new(identity_window());
        let layout = Layout::from_size_align(128, 64).unwrap();
        let handle = unsafe {
            op.alloc_coherent(DmaConstraints::new(u64::MAX), layout)
                .unwrap()
        };

        assert_eq!(handle.size(), 128);
        assert_eq!(handle.dma_addr().as_u64() as usize % 64, 0);
        assert_eq!(op.allocation_count(), 1);

        unsafe { let _ = op.dealloc_coherent(handle); };
        assert_eq!(op.allocation_count(), 0);
    }

    #[ktest]
    fn bounces_streaming_dma_in_both_directions() {
        let op = UsbKernelOp::new(identity_window());
        let constraints = DmaConstraints::new(u64::MAX).with_align(64);
        let size = NonZeroUsize::new(64).unwrap();

        let mut to_device = [0x5au8; 64];
        let to_device_ptr = NonNull::new(to_device.as_mut_ptr()).unwrap();
        let to_device_map = unsafe {
            op.map_streaming(constraints, to_device_ptr, size, ApiDirection::ToDevice)
                .unwrap()
        };
        op.sync_map_for_device(&to_device_map, 0, size.get(), ApiDirection::ToDevice);
        let bounce = unsafe {
            core::slice::from_raw_parts(to_device_map.bounce_ptr().unwrap().as_ptr(), size.get())
        };
        assert_eq!(bounce, &to_device);
        unsafe { op.unmap_streaming(to_device_map) };

        let mut from_device = [0u8; 64];
        let from_device_ptr = NonNull::new(from_device.as_mut_ptr()).unwrap();
        let from_device_map = unsafe {
            op.map_streaming(constraints, from_device_ptr, size, ApiDirection::FromDevice)
                .unwrap()
        };
        unsafe {
            from_device_map
                .bounce_ptr()
                .unwrap()
                .as_ptr()
                .write_bytes(0xa5, size.get());
        }
        op.sync_map_for_cpu(&from_device_map, 0, size.get(), ApiDirection::FromDevice);
        assert_eq!(from_device, [0xa5; 64]);
        unsafe { op.unmap_streaming(from_device_map) };
    }
}

mod dma_stream {
    use super::*;

    #[ktest]
    fn streaming_map() {
        let segment = FrameAllocOptions::new()
            .alloc_segment_with(1, |_| ())
            .unwrap();
        let dma_stream = DmaStream::<FromAndToDevice>::map(segment.clone().into(), true).unwrap();
        assert_eq!(dma_stream.paddr(), segment.paddr());
        assert_eq!(dma_stream.size(), PAGE_SIZE);
    }

    #[ktest]
    fn read_write() {
        let segment = FrameAllocOptions::new()
            .alloc_segment_with(2, |_| ())
            .unwrap();
        let dma_stream = DmaStream::<FromAndToDevice>::map(segment.into(), false).unwrap();

        let buf_write = vec![1u8; 2 * PAGE_SIZE];
        dma_stream.write_bytes(0, &buf_write).unwrap();
        dma_stream.sync_to_device(0..2 * PAGE_SIZE).unwrap();
        let mut buf_read = vec![0u8; 2 * PAGE_SIZE];
        dma_stream.sync_from_device(0..2 * PAGE_SIZE).unwrap();
        dma_stream.read_bytes(0, &mut buf_read).unwrap();
        assert_eq!(buf_write, buf_read);
    }

    #[ktest]
    fn reader_writer() {
        let segment = FrameAllocOptions::new()
            .alloc_segment_with(2, |_| ())
            .unwrap();
        let dma_stream = DmaStream::<FromAndToDevice>::map(segment.into(), false).unwrap();

        let buf_write = vec![1u8; PAGE_SIZE];
        let mut writer = dma_stream.writer().unwrap();
        writer.write(&mut buf_write.as_slice().into());
        writer.write(&mut buf_write.as_slice().into());
        dma_stream.sync_to_device(0..2 * PAGE_SIZE).unwrap();
        let mut buf_read = vec![0u8; 2 * PAGE_SIZE];
        let buf_write = vec![1u8; 2 * PAGE_SIZE];
        let mut reader = dma_stream.reader().unwrap();
        dma_stream.sync_from_device(0..2 * PAGE_SIZE).unwrap();
        reader.read(&mut buf_read.as_mut_slice().into());
        assert_eq!(buf_read, buf_write);
    }

    #[ktest]
    fn to_device() {
        let segment = FrameAllocOptions::new()
            .alloc_segment_with(1, |_| ())
            .unwrap();
        let dma_stream = DmaStream::<ToDevice>::map(segment.clone().into(), false).unwrap();
        assert_eq!(dma_stream.paddr(), segment.paddr());
        assert_eq!(dma_stream.size(), PAGE_SIZE);

        let mut buffer = [0u8; 8];
        let mut writer_fallible = VmWriter::from(&mut buffer[..]).to_fallible();
        let result = dma_stream.read(0, &mut writer_fallible);
        assert!(result.is_err());

        let buffer = [0u8; 8];
        let mut reader_fallible = VmReader::from(&buffer[..]).to_fallible();
        let result = dma_stream.write(0, &mut reader_fallible);
        assert!(result.is_ok());

        assert!(dma_stream.reader().is_err());
        assert!(dma_stream.writer().is_ok());
    }

    #[ktest]
    fn from_device() {
        let segment = FrameAllocOptions::new()
            .alloc_segment_with(1, |_| ())
            .unwrap();
        let dma_stream = DmaStream::<FromDevice>::map(segment.clone().into(), false).unwrap();
        assert_eq!(dma_stream.paddr(), segment.paddr());
        assert_eq!(dma_stream.size(), PAGE_SIZE);

        let mut buffer = [0u8; 8];
        let mut writer_fallible = VmWriter::from(&mut buffer[..]).to_fallible();
        let result = dma_stream.read(0, &mut writer_fallible);
        assert!(result.is_ok());

        let buffer = [0u8; 8];
        let mut reader_fallible = VmReader::from(&buffer[..]).to_fallible();
        let result = dma_stream.write(0, &mut reader_fallible);
        assert!(result.is_err());

        assert!(dma_stream.reader().is_ok());
        assert!(dma_stream.writer().is_err());
    }

    #[ktest]
    fn streaming_boundary_conditions() {
        let segment = FrameAllocOptions::new()
            .alloc_segment_with(2, |_| ())
            .unwrap();
        let dma_stream = DmaStream::<FromAndToDevice>::map(segment.into(), false).unwrap();

        // Test partial page operations
        let small_buf = [0xAAu8; 128];
        dma_stream.write_bytes(PAGE_SIZE - 64, &small_buf).unwrap();
        dma_stream
            .sync_to_device(PAGE_SIZE - 64..PAGE_SIZE + 64)
            .unwrap();
        let mut read_buf = [0u8; 128];
        dma_stream
            .sync_from_device(PAGE_SIZE - 64..PAGE_SIZE + 64)
            .unwrap();
        dma_stream
            .read_bytes(PAGE_SIZE - 64, &mut read_buf)
            .unwrap();
        assert_eq!(read_buf, small_buf);
    }

    #[ktest]
    fn alloc_dma_stream() {
        let dma_stream = DmaStream::<FromAndToDevice>::alloc(2, false).unwrap();
        assert_eq!(dma_stream.size(), 2 * PAGE_SIZE);

        // Verify allocated memory is zeroed
        let mut buf_read = vec![1u8; 2 * PAGE_SIZE];
        dma_stream.sync_from_device(0..2 * PAGE_SIZE).unwrap();
        dma_stream.read_bytes(0, &mut buf_read).unwrap();
        assert_eq!(buf_read, vec![0u8; 2 * PAGE_SIZE]);

        let buf_write = vec![0xABu8; 2 * PAGE_SIZE];
        dma_stream.write_bytes(0, &buf_write).unwrap();
        dma_stream.sync_to_device(0..2 * PAGE_SIZE).unwrap();

        let mut buf_read = vec![0u8; 2 * PAGE_SIZE];
        dma_stream.sync_from_device(0..2 * PAGE_SIZE).unwrap();
        dma_stream.read_bytes(0, &mut buf_read).unwrap();
        assert_eq!(buf_write, buf_read);
    }

    #[ktest]
    fn alloc_uninit_dma_stream() {
        let dma_stream = DmaStream::<FromAndToDevice>::alloc_uninit(1, false).unwrap();
        assert_eq!(dma_stream.size(), PAGE_SIZE);

        let buf_write = vec![0xCDu8; PAGE_SIZE];
        dma_stream.write_bytes(0, &buf_write).unwrap();
        dma_stream.sync_to_device(0..PAGE_SIZE).unwrap();

        let mut buf_read = vec![0u8; PAGE_SIZE];
        dma_stream.sync_from_device(0..PAGE_SIZE).unwrap();
        dma_stream.read_bytes(0, &mut buf_read).unwrap();
        assert_eq!(buf_write, buf_read);
    }
}
