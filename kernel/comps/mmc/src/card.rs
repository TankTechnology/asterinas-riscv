// SPDX-License-Identifier: MPL-2.0

//! Bounded SDHC card discovery and read-only sector access.

use crate::sdhci::{Command, HostError, ResponseType};

const DISCOVERY_CLOCK_HZ: u32 = 400_000;
const DATA_CLOCK_HZ: u32 = 25_000_000;
const OCR_RETRIES: usize = 1000;
const OCR_BUSY: u32 = 1 << 31;
const OCR_CCS: u32 = 1 << 30;
const OCR_ARGUMENT: u32 = OCR_CCS | 0x00ff_8000;
const SECTOR_SIZE: usize = 512;
const MAX_BLOCKS_PER_COMMAND: usize = u16::MAX as usize;

/// A response returned by the SD host.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Response {
    None,
    Short(u32),
    /// A canonical big-endian 128-bit R2 response.
    Long([u32; 4]),
}

impl Response {
    fn short(self) -> Result<u32, HostError> {
        match self {
            Self::Short(value) => Ok(value),
            _ => Err(HostError::Unsupported),
        }
    }

    fn long(self) -> Result<u128, HostError> {
        let Self::Long(words) = self else {
            return Err(HostError::Unsupported);
        };
        Ok(words
            .into_iter()
            .fold(0, |value, word| (value << 32) | word as u128))
    }
}

/// Narrow host-controller contract used by both the model and real SDHCI adapter.
pub trait HostController {
    fn reset(&mut self) -> Result<(), HostError>;
    fn set_clock(&mut self, hz: u32) -> Result<(), HostError>;
    fn command(&mut self, command: Command) -> Result<Response, HostError>;
    fn set_bus_width_4(&mut self) -> Result<(), HostError>;
    fn wait_buffer_read_ready(&mut self) -> Result<(), HostError>;
    fn read_data_word(&mut self) -> Result<u32, HostError>;
    fn wait_buffer_write_ready(&mut self) -> Result<(), HostError>;
    fn write_data_word(&mut self, value: u32) -> Result<(), HostError>;
    fn wait_transfer_complete(&mut self) -> Result<(), HostError>;
    fn reset_data_line(&mut self);
}

/// Immutable SDHC identity learned during discovery.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Card {
    rca: u16,
    nr_sectors: u64,
}

impl Card {
    /// Discovers and selects one high-capacity SD card.
    pub fn discover(host: &mut impl HostController) -> Result<Self, HostError> {
        host.reset()?;
        host.set_clock(DISCOVERY_CLOCK_HZ)?;
        expect_none(host.command(Command::idle())?)?;
        if host.command(Command::send_if_cond(0x1aa))?.short()? & 0xfff != 0x1aa {
            return Err(HostError::Unsupported);
        }

        let mut ocr = None;
        for _ in 0..OCR_RETRIES {
            app_command(host, 0)?;
            let value = host.command(Command::app_op_cond(OCR_ARGUMENT))?.short()?;
            if value & OCR_BUSY != 0 {
                ocr = Some(value);
                break;
            }
        }
        let ocr = ocr.ok_or(HostError::Timeout)?;
        if ocr & OCR_CCS == 0 {
            return Err(HostError::Unsupported);
        }

        expect_long(host.command(Command::new(2, 0, ResponseType::Long, None))?)?;
        let rca = (host
            .command(Command::new(3, 0, ResponseType::Short, None))?
            .short()?
            >> 16) as u16;
        if rca == 0 {
            return Err(HostError::Unsupported);
        }
        let csd = host
            .command(Command::new(
                9,
                (rca as u32) << 16,
                ResponseType::Long,
                None,
            ))?
            .long()?;
        let nr_sectors = csd_v2_nr_sectors(csd)?;
        host.command(Command::new(
            7,
            (rca as u32) << 16,
            ResponseType::ShortBusy,
            None,
        ))?
        .short()?;
        app_command(host, rca)?;
        host.command(Command::new(6, 2, ResponseType::Short, None))?
            .short()?;
        host.set_bus_width_4()?;
        host.set_clock(DATA_CLOCK_HZ)?;
        Ok(Self { rca, nr_sectors })
    }

    pub const fn rca(self) -> u16 {
        self.rca
    }

    pub const fn nr_sectors(self) -> u64 {
        self.nr_sectors
    }

    /// Reads one 512-byte sector with CMD17 and bounded host waits.
    pub fn read_sector(
        self,
        host: &mut impl HostController,
        lba: u64,
        out: &mut [u8; SECTOR_SIZE],
    ) -> Result<(), HostError> {
        if lba >= self.nr_sectors || lba > u32::MAX as u64 {
            return Err(HostError::Unsupported);
        }
        let result = (|| {
            host.command(Command::read_single_block(lba as u32))?
                .short()?;
            host.wait_buffer_read_ready()?;
            for chunk in out.as_chunks_mut::<4>().0 {
                chunk.copy_from_slice(&host.read_data_word()?.to_le_bytes());
            }
            host.wait_transfer_complete()
        })();
        if result.is_err() {
            host.reset_data_line();
        }
        result
    }

    /// Reads a whole number of sectors using bounded repeated CMD17 transfers.
    pub fn read_sectors(
        self,
        host: &mut impl HostController,
        first_lba: u64,
        out: &mut [u8],
    ) -> Result<(), HostError> {
        if !out.len().is_multiple_of(SECTOR_SIZE) {
            return Err(HostError::Unsupported);
        }
        let count = (out.len() / SECTOR_SIZE) as u64;
        let end = first_lba.checked_add(count).ok_or(HostError::Unsupported)?;
        if end > self.nr_sectors {
            return Err(HostError::Unsupported);
        }
        if end > u32::MAX as u64 + 1 {
            return Err(HostError::Unsupported);
        }
        let mut lba = first_lba;
        for blocks in out.chunks_mut(MAX_BLOCKS_PER_COMMAND * SECTOR_SIZE) {
            let block_count = blocks.len() / SECTOR_SIZE;
            if block_count == 1 {
                let sector: &mut [u8; SECTOR_SIZE] =
                    blocks.try_into().map_err(|_| HostError::Unsupported)?;
                self.read_sector(host, lba, sector)?;
            } else if block_count != 0 {
                let result = (|| {
                    host.command(Command::read_multiple_blocks(
                        lba as u32,
                        block_count as u16,
                    ))?
                    .short()?;
                    for sector in blocks.as_chunks_mut::<SECTOR_SIZE>().0 {
                        host.wait_buffer_read_ready()?;
                        for word in sector.as_chunks_mut::<4>().0 {
                            word.copy_from_slice(&host.read_data_word()?.to_le_bytes());
                        }
                    }
                    host.wait_transfer_complete()
                })();
                if result.is_err() {
                    host.reset_data_line();
                }
                result?;
            }
            lba += block_count as u64;
        }
        Ok(())
    }

    /// Writes one 512-byte sector with CMD24 and bounded host waits.
    pub fn write_sector(
        self,
        host: &mut impl HostController,
        lba: u64,
        sector: &[u8; SECTOR_SIZE],
    ) -> Result<(), HostError> {
        if lba >= self.nr_sectors || lba > u32::MAX as u64 {
            return Err(HostError::Unsupported);
        }
        let result = (|| {
            host.command(Command::write_single_block(lba as u32))?
                .short()?;
            host.wait_buffer_write_ready()?;
            for chunk in sector.as_chunks::<4>().0 {
                host.write_data_word(u32::from_le_bytes(*chunk))?;
            }
            host.wait_transfer_complete()
        })();
        if result.is_err() {
            host.reset_data_line();
        }
        result
    }

    /// Writes a whole number of sectors using bounded CMD24/CMD25 transfers.
    pub fn write_sectors(
        self,
        host: &mut impl HostController,
        first_lba: u64,
        data: &[u8],
    ) -> Result<(), HostError> {
        if !data.len().is_multiple_of(SECTOR_SIZE) {
            return Err(HostError::Unsupported);
        }
        let count = (data.len() / SECTOR_SIZE) as u64;
        let end = first_lba.checked_add(count).ok_or(HostError::Unsupported)?;
        if end > self.nr_sectors || end > u32::MAX as u64 + 1 {
            return Err(HostError::Unsupported);
        }
        let mut lba = first_lba;
        for blocks in data.chunks(MAX_BLOCKS_PER_COMMAND * SECTOR_SIZE) {
            let block_count = blocks.len() / SECTOR_SIZE;
            if block_count == 1 {
                let sector: &[u8; SECTOR_SIZE] =
                    blocks.try_into().map_err(|_| HostError::Unsupported)?;
                self.write_sector(host, lba, sector)?;
            } else if block_count != 0 {
                let result = (|| {
                    host.command(Command::write_multiple_blocks(
                        lba as u32,
                        block_count as u16,
                    ))?
                    .short()?;
                    for sector in blocks.as_chunks::<SECTOR_SIZE>().0 {
                        host.wait_buffer_write_ready()?;
                        for word in sector.as_chunks::<4>().0 {
                            host.write_data_word(u32::from_le_bytes(*word))?;
                        }
                    }
                    host.wait_transfer_complete()
                })();
                if result.is_err() {
                    host.reset_data_line();
                }
                result?;
            }
            lba += block_count as u64;
        }
        Ok(())
    }
}

fn app_command(host: &mut impl HostController, rca: u16) -> Result<(), HostError> {
    host.command(Command::app_prefix(rca))?.short()?;
    Ok(())
}

fn expect_none(response: Response) -> Result<(), HostError> {
    matches!(response, Response::None)
        .then_some(())
        .ok_or(HostError::Unsupported)
}

fn expect_long(response: Response) -> Result<(), HostError> {
    matches!(response, Response::Long(_))
        .then_some(())
        .ok_or(HostError::Unsupported)
}

fn csd_v2_nr_sectors(csd: u128) -> Result<u64, HostError> {
    if (csd >> 126) & 0b11 != 1 {
        return Err(HostError::Unsupported);
    }
    let c_size = ((csd >> 48) & 0x3f_ffff) as u64;
    c_size
        .checked_add(1)
        .and_then(|size| size.checked_mul(1024))
        .ok_or(HostError::Unsupported)
}

#[cfg(ktest)]
mod tests {
    use alloc::{collections::VecDeque, vec, vec::Vec};

    use ostd::prelude::ktest;

    use super::*;

    #[derive(Debug)]
    enum Step {
        Reset,
        Clock(u32),
        Command(u8, u32, Response),
        DataCommand(u8, u32, u16, Response),
        Width4,
    }

    struct FakeHost {
        steps: VecDeque<Step>,
        words: VecDeque<u32>,
        buffer_ready: Result<(), HostError>,
        write_ready: Result<(), HostError>,
        written_words: Vec<u32>,
        transfer_complete: Result<(), HostError>,
        data_resets: usize,
    }

    impl FakeHost {
        fn discovery(csd: u128) -> Self {
            let words = [
                (csd >> 96) as u32,
                (csd >> 64) as u32,
                (csd >> 32) as u32,
                csd as u32,
            ];
            Self {
                steps: vec![
                    Step::Reset,
                    Step::Clock(DISCOVERY_CLOCK_HZ),
                    Step::Command(0, 0, Response::None),
                    Step::Command(8, 0x1aa, Response::Short(0x1aa)),
                    Step::Command(55, 0, Response::Short(0)),
                    Step::Command(41, OCR_ARGUMENT, Response::Short(OCR_CCS)),
                    Step::Command(55, 0, Response::Short(0)),
                    Step::Command(41, OCR_ARGUMENT, Response::Short(OCR_BUSY | OCR_CCS)),
                    Step::Command(2, 0, Response::Long([0; 4])),
                    Step::Command(3, 0, Response::Short(7 << 16)),
                    Step::Command(9, 7 << 16, Response::Long(words)),
                    Step::Command(7, 7 << 16, Response::Short(0)),
                    Step::Command(55, 7 << 16, Response::Short(0)),
                    Step::Command(6, 2, Response::Short(0)),
                    Step::Width4,
                    Step::Clock(DATA_CLOCK_HZ),
                ]
                .into(),
                words: VecDeque::new(),
                buffer_ready: Ok(()),
                write_ready: Ok(()),
                written_words: Vec::new(),
                transfer_complete: Ok(()),
                data_resets: 0,
            }
        }

        fn assert_done(&self) {
            assert!(self.steps.is_empty(), "unconsumed steps: {:?}", self.steps);
        }
    }

    impl HostController for FakeHost {
        fn reset(&mut self) -> Result<(), HostError> {
            assert!(matches!(self.steps.pop_front(), Some(Step::Reset)));
            Ok(())
        }

        fn set_clock(&mut self, hz: u32) -> Result<(), HostError> {
            assert!(matches!(self.steps.pop_front(), Some(Step::Clock(value)) if value == hz));
            Ok(())
        }

        fn command(&mut self, command: Command) -> Result<Response, HostError> {
            match self.steps.pop_front() {
                Some(Step::Command(index, argument, response)) => {
                    assert_eq!((command.index, command.argument), (index, argument));
                    assert_eq!(command.block_count(), usize::from(command.data.is_some()));
                    Ok(response)
                }
                Some(Step::DataCommand(index, argument, blocks, response)) => {
                    assert_eq!((command.index, command.argument), (index, argument));
                    assert_eq!(command.block_count(), blocks as usize);
                    Ok(response)
                }
                step => panic!("unexpected command {command:?}, expected {step:?}"),
            }
        }

        fn set_bus_width_4(&mut self) -> Result<(), HostError> {
            assert!(matches!(self.steps.pop_front(), Some(Step::Width4)));
            Ok(())
        }

        fn wait_buffer_read_ready(&mut self) -> Result<(), HostError> {
            self.buffer_ready
        }

        fn read_data_word(&mut self) -> Result<u32, HostError> {
            self.words.pop_front().ok_or(HostError::Timeout)
        }

        fn wait_buffer_write_ready(&mut self) -> Result<(), HostError> {
            self.write_ready
        }

        fn write_data_word(&mut self, value: u32) -> Result<(), HostError> {
            self.written_words.push(value);
            Ok(())
        }

        fn wait_transfer_complete(&mut self) -> Result<(), HostError> {
            self.transfer_complete
        }

        fn reset_data_line(&mut self) {
            self.data_resets += 1;
        }
    }

    #[ktest]
    fn discovers_sdhc_with_exact_command_order() {
        let c_size = 0x1234u128;
        let csd = (1u128 << 126) | (c_size << 48);
        let mut host = FakeHost::discovery(csd);
        let card = Card::discover(&mut host).unwrap();
        assert_eq!(card.rca(), 7);
        assert_eq!(card.nr_sectors(), (c_size as u64 + 1) * 1024);
        host.assert_done();
    }

    #[ktest]
    fn rejects_non_sdhc_and_invalid_csd() {
        let mut host = FakeHost::discovery(0);
        if let Step::Command(_, _, response) = &mut host.steps[7] {
            *response = Response::Short(OCR_BUSY);
        }
        assert_eq!(Card::discover(&mut host), Err(HostError::Unsupported));

        let mut host = FakeHost::discovery(0);
        assert_eq!(Card::discover(&mut host), Err(HostError::Unsupported));
    }

    #[ktest]
    fn reads_one_sector_as_little_endian_words() {
        let card = Card {
            rca: 1,
            nr_sectors: 8,
        };
        let mut host = FakeHost::discovery(1u128 << 126);
        host.steps = vec![Step::Command(17, 3, Response::Short(0))].into();
        host.words = (0..128).collect();
        let mut sector = [0u8; SECTOR_SIZE];
        card.read_sector(&mut host, 3, &mut sector).unwrap();
        assert_eq!(&sector[0..8], &[0, 0, 0, 0, 1, 0, 0, 0]);
        assert_eq!(host.data_resets, 0);
        host.assert_done();
    }

    #[ktest]
    fn read_failure_resets_data_and_bounds_before_commands() {
        let card = Card {
            rca: 1,
            nr_sectors: 2,
        };
        let mut host = FakeHost::discovery(1u128 << 126);
        host.steps = vec![Step::Command(17, 1, Response::Short(0))].into();
        host.buffer_ready = Err(HostError::Timeout);
        let mut sector = [0u8; SECTOR_SIZE];
        assert_eq!(
            card.read_sector(&mut host, 1, &mut sector),
            Err(HostError::Timeout)
        );
        assert_eq!(host.data_resets, 1);
        host.assert_done();

        host.steps.clear();
        assert_eq!(
            card.read_sector(&mut host, 2, &mut sector),
            Err(HostError::Unsupported)
        );
        assert_eq!(
            card.read_sectors(&mut host, 1, &mut [0u8; 1024]),
            Err(HostError::Unsupported)
        );
    }

    #[ktest]
    fn reads_multiple_sectors_with_one_bounded_command() {
        let card = Card {
            rca: 1,
            nr_sectors: 8,
        };
        let mut host = FakeHost::discovery(1u128 << 126);
        host.steps = vec![Step::DataCommand(18, 2, 2, Response::Short(0))].into();
        host.words = (0..256).collect();
        let mut sectors = [0u8; 2 * SECTOR_SIZE];
        card.read_sectors(&mut host, 2, &mut sectors).unwrap();
        assert_eq!(&sectors[0..8], &[0, 0, 0, 0, 1, 0, 0, 0]);
        assert_eq!(&sectors[SECTOR_SIZE..SECTOR_SIZE + 4], &[128, 0, 0, 0]);
        assert_eq!(host.data_resets, 0);
        host.assert_done();
    }

    #[ktest]
    fn writes_one_sector_as_little_endian_words() {
        let card = Card {
            rca: 1,
            nr_sectors: 8,
        };
        let mut host = FakeHost::discovery(1u128 << 126);
        host.steps = vec![Step::Command(24, 3, Response::Short(0))].into();
        let mut sector = [0u8; SECTOR_SIZE];
        sector[0..8].copy_from_slice(&[0, 0, 0, 0, 1, 0, 0, 0]);
        card.write_sector(&mut host, 3, &sector).unwrap();
        assert_eq!(host.written_words.len(), 128);
        assert_eq!(&host.written_words[0..2], &[0, 1]);
        assert_eq!(host.data_resets, 0);
        host.assert_done();
    }

    #[ktest]
    fn write_failure_resets_data_and_bounds_before_commands() {
        let card = Card {
            rca: 1,
            nr_sectors: 2,
        };
        let mut host = FakeHost::discovery(1u128 << 126);
        host.steps = vec![Step::Command(24, 1, Response::Short(0))].into();
        host.write_ready = Err(HostError::Timeout);
        let sector = [0u8; SECTOR_SIZE];
        assert_eq!(
            card.write_sector(&mut host, 1, &sector),
            Err(HostError::Timeout)
        );
        assert_eq!(host.data_resets, 1);
        host.assert_done();

        host.steps.clear();
        assert_eq!(
            card.write_sector(&mut host, 2, &sector),
            Err(HostError::Unsupported)
        );
    }

    #[ktest]
    fn writes_multiple_sectors_with_one_bounded_command() {
        let card = Card {
            rca: 1,
            nr_sectors: 8,
        };
        let mut host = FakeHost::discovery(1u128 << 126);
        host.steps = vec![Step::DataCommand(25, 2, 2, Response::Short(0))].into();
        let mut sectors = [0u8; 2 * SECTOR_SIZE];
        sectors[0..4].copy_from_slice(&1u32.to_le_bytes());
        sectors[SECTOR_SIZE..SECTOR_SIZE + 4].copy_from_slice(&2u32.to_le_bytes());
        card.write_sectors(&mut host, 2, &sectors).unwrap();
        assert_eq!(host.written_words.len(), 256);
        assert_eq!(host.written_words[0], 1);
        assert_eq!(host.written_words[128], 2);
        assert_eq!(host.data_resets, 0);
        host.assert_done();
    }
}
