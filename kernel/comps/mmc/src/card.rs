// SPDX-License-Identifier: MPL-2.0

//! Bounded SDHC card discovery and read-only sector access.

use crate::sdhci::{Command, HostError, ResponseType};

const DISCOVERY_CLOCK_HZ: u32 = 400_000;
const DATA_CLOCK_HZ: u32 = 25_000_000;
const HIGH_SPEED_CLOCK_HZ: u32 = 50_000_000;
const SWITCH_CHECK_HIGH_SPEED: u32 = 0x00ff_fff1;
const SWITCH_SET_HIGH_SPEED: u32 = 0x80ff_fff1;
const OCR_RETRIES: usize = 1000;
const OCR_BUSY: u32 = 1 << 31;
const OCR_CCS: u32 = 1 << 30;
const OCR_ARGUMENT: u32 = OCR_CCS | 0x00ff_8000;
const SECTOR_SIZE: usize = 512;
const MAX_BLOCKS_PER_COMMAND: usize = u16::MAX as usize;
const CSD_COMMAND_CLASS_SWITCH: u16 = 1 << 10;
const SWITCH_STATUS_HIGH_SPEED: u8 = 1 << 1;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SdSpec {
    V1_0,
    V1_10,
    V2OrLater,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct Csd {
    nr_sectors: u64,
    command_classes: u16,
}

impl Csd {
    fn parse(raw: u128) -> Result<Self, HostError> {
        if (raw >> 126) & 0b11 != 1 {
            return Err(HostError::Unsupported);
        }
        let c_size = ((raw >> 48) & 0x3f_ffff) as u64;
        let nr_sectors = c_size
            .checked_add(1)
            .and_then(|size| size.checked_mul(1024))
            .ok_or(HostError::Unsupported)?;
        Ok(Self {
            nr_sectors,
            command_classes: ((raw >> 84) & 0x0fff) as u16,
        })
    }

    const fn nr_sectors(self) -> u64 {
        self.nr_sectors
    }

    const fn supports_switch(self) -> bool {
        self.command_classes & CSD_COMMAND_CLASS_SWITCH != 0
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct Scr {
    spec: SdSpec,
}

impl Scr {
    fn parse(bytes: [u8; 8]) -> Result<Self, HostError> {
        let raw = u64::from_be_bytes(bytes);
        if raw >> 60 != 0 {
            return Err(HostError::Unsupported);
        }
        let spec = match (raw >> 56) & 0x0f {
            0 => SdSpec::V1_0,
            1 => SdSpec::V1_10,
            2 => SdSpec::V2OrLater,
            _ => return Err(HostError::Unsupported),
        };
        Ok(Self { spec })
    }

    #[cfg(ktest)]
    const fn spec(self) -> SdSpec {
        self.spec
    }

    const fn supports_switch(self) -> bool {
        !matches!(self.spec, SdSpec::V1_0)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct SwitchStatus<'a> {
    bytes: &'a [u8; 64],
}

impl<'a> SwitchStatus<'a> {
    fn parse(bytes: &'a [u8]) -> Result<Self, HostError> {
        let bytes = bytes.try_into().map_err(|_| HostError::Unsupported)?;
        Ok(Self { bytes })
    }

    const fn supports_high_speed(self) -> bool {
        self.bytes[13] & SWITCH_STATUS_HIGH_SPEED != 0
    }

    const fn selected_access_mode(self) -> u8 {
        self.bytes[16] & 0x0f
    }
}

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
    fn set_timing(&mut self, timing: CardTiming) -> Result<(), HostError>;
    fn command(&mut self, command: Command) -> Result<Response, HostError>;
    fn set_bus_width_4(&mut self) -> Result<(), HostError>;
    fn wait_buffer_read_ready(&mut self) -> Result<(), HostError>;
    fn read_data_word(&mut self) -> Result<u32, HostError>;
    fn wait_buffer_write_ready(&mut self) -> Result<(), HostError>;
    fn write_data_word(&mut self, value: u32) -> Result<(), HostError>;
    fn wait_transfer_complete(&mut self) -> Result<(), HostError>;
    fn reset_data_line(&mut self);

    fn max_blocks_per_command(&self) -> usize {
        MAX_BLOCKS_PER_COMMAND
    }

    fn try_read_blocks(
        &mut self,
        _command: Command,
        _output: &mut [u8],
    ) -> Result<bool, HostError> {
        Ok(false)
    }

    fn try_write_blocks(&mut self, _command: Command, _data: &[u8]) -> Result<bool, HostError> {
        Ok(false)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CardTiming {
    DefaultSpeed,
    HighSpeed,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SpeedSelection {
    HighSpeed,
    DefaultSpeedUnsupported,
    DefaultSpeedForced,
    DefaultSpeedRecovered,
}

impl SpeedSelection {
    pub const fn label(self) -> &'static str {
        match self {
            Self::HighSpeed => "high-speed",
            Self::DefaultSpeedUnsupported => "default-speed-unsupported",
            Self::DefaultSpeedForced => "default-speed-forced",
            Self::DefaultSpeedRecovered => "default-speed-recovered",
        }
    }
}

/// Immutable SDHC identity learned during discovery.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Card {
    rca: u16,
    nr_sectors: u64,
    timing: CardTiming,
    speed_selection: SpeedSelection,
}

impl Card {
    /// Discovers and selects one high-capacity SD card.
    pub fn discover(host: &mut impl HostController) -> Result<Self, HostError> {
        Self::discover_with_policy(host, false)
    }

    pub fn discover_with_policy(
        host: &mut impl HostController,
        force_default_speed: bool,
    ) -> Result<Self, HostError> {
        let (mut card, csd) = Self::discover_default_speed(host)?;
        if force_default_speed {
            card.speed_selection = SpeedSelection::DefaultSpeedForced;
            return Ok(card);
        }
        if !csd.supports_switch() {
            return Ok(card);
        }
        match card.try_enable_high_speed(host)? {
            Promotion::Selected => {
                card.timing = CardTiming::HighSpeed;
                card.speed_selection = SpeedSelection::HighSpeed;
                Ok(card)
            }
            Promotion::Unsupported => Ok(card),
            Promotion::Ambiguous => Self::discover_default_speed(host).map(|(mut card, _)| {
                card.speed_selection = SpeedSelection::DefaultSpeedRecovered;
                card
            }),
        }
    }

    fn discover_default_speed(host: &mut impl HostController) -> Result<(Self, Csd), HostError> {
        host.reset()?;
        host.set_timing(CardTiming::DefaultSpeed)?;
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
        let csd = Csd::parse(csd)?;
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
        Ok((
            Self {
                rca,
                nr_sectors: csd.nr_sectors(),
                timing: CardTiming::DefaultSpeed,
                speed_selection: SpeedSelection::DefaultSpeedUnsupported,
            },
            csd,
        ))
    }

    fn try_enable_high_speed(
        &self,
        host: &mut impl HostController,
    ) -> Result<Promotion, HostError> {
        if app_command(host, self.rca).is_err() {
            return Ok(Promotion::Unsupported);
        }
        let mut scr_bytes = [0u8; 8];
        if read_register(host, Command::send_scr(), &mut scr_bytes).is_err() {
            return Ok(Promotion::Unsupported);
        }
        if !Scr::parse(scr_bytes)?.supports_switch() {
            return Ok(Promotion::Unsupported);
        }

        let mut check_bytes = [0u8; 64];
        if read_register(
            host,
            Command::switch_function(SWITCH_CHECK_HIGH_SPEED),
            &mut check_bytes,
        )
        .is_err()
        {
            return Ok(Promotion::Unsupported);
        }
        if !SwitchStatus::parse(&check_bytes)?.supports_high_speed() {
            return Ok(Promotion::Unsupported);
        }

        let mut switch_bytes = [0u8; 64];
        if read_register(
            host,
            Command::switch_function(SWITCH_SET_HIGH_SPEED),
            &mut switch_bytes,
        )
        .is_err()
        {
            return Ok(Promotion::Ambiguous);
        }
        if SwitchStatus::parse(&switch_bytes)?.selected_access_mode() != 1 {
            return Ok(Promotion::Unsupported);
        }
        if host.set_timing(CardTiming::HighSpeed).is_err()
            || host.set_clock(HIGH_SPEED_CLOCK_HZ).is_err()
        {
            return Ok(Promotion::Ambiguous);
        }
        Ok(Promotion::Selected)
    }

    pub const fn rca(self) -> u16 {
        self.rca
    }

    pub const fn nr_sectors(self) -> u64 {
        self.nr_sectors
    }

    pub const fn timing(self) -> CardTiming {
        self.timing
    }

    pub const fn speed_selection(self) -> SpeedSelection {
        self.speed_selection
    }

    pub const fn data_clock_hz(self) -> u32 {
        match self.timing {
            CardTiming::DefaultSpeed => DATA_CLOCK_HZ,
            CardTiming::HighSpeed => HIGH_SPEED_CLOCK_HZ,
        }
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
        let max_blocks = host
            .max_blocks_per_command()
            .clamp(1, MAX_BLOCKS_PER_COMMAND);
        for blocks in out.chunks_mut(max_blocks * SECTOR_SIZE) {
            let block_count = blocks.len() / SECTOR_SIZE;
            if block_count == 1 {
                let sector: &mut [u8; SECTOR_SIZE] =
                    blocks.try_into().map_err(|_| HostError::Unsupported)?;
                self.read_sector(host, lba, sector)?;
            } else if block_count != 0 {
                let command = Command::read_multiple_blocks(lba as u32, block_count as u16);
                let result = (|| {
                    if host.try_read_blocks(command, blocks)? {
                        return Ok(());
                    }
                    host.command(command)?.short()?;
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
        let max_blocks = host
            .max_blocks_per_command()
            .clamp(1, MAX_BLOCKS_PER_COMMAND);
        for blocks in data.chunks(max_blocks * SECTOR_SIZE) {
            let block_count = blocks.len() / SECTOR_SIZE;
            if block_count == 1 {
                let sector: &[u8; SECTOR_SIZE] =
                    blocks.try_into().map_err(|_| HostError::Unsupported)?;
                self.write_sector(host, lba, sector)?;
            } else if block_count != 0 {
                let command = Command::write_multiple_blocks(lba as u32, block_count as u16);
                let result = (|| {
                    if host.try_write_blocks(command, blocks)? {
                        return Ok(());
                    }
                    host.command(command)?.short()?;
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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Promotion {
    Selected,
    Unsupported,
    Ambiguous,
}

fn read_register(
    host: &mut impl HostController,
    command: Command,
    output: &mut [u8],
) -> Result<(), HostError> {
    if output.len() != command.block_size() || command.block_count() != 1 {
        return Err(HostError::Unsupported);
    }
    let result = (|| {
        host.command(command)?.short()?;
        host.wait_buffer_read_ready()?;
        for word in output.as_chunks_mut::<4>().0 {
            word.copy_from_slice(&host.read_data_word()?.to_le_bytes());
        }
        host.wait_transfer_complete()
    })();
    if result.is_err() {
        host.reset_data_line();
    }
    result
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

#[cfg(ktest)]
mod tests {
    use alloc::{collections::VecDeque, vec, vec::Vec};

    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn parses_only_the_sd_capabilities_needed_for_high_speed() {
        let c_size = 0x1234u128;
        let csd = Csd::parse((1u128 << 126) | ((1u128 << 10) << 84) | (c_size << 48)).unwrap();
        assert_eq!(csd.nr_sectors(), (c_size as u64 + 1) * 1024);
        assert!(csd.supports_switch());
        assert!(
            !Csd::parse((1u128 << 126) | (c_size << 48))
                .unwrap()
                .supports_switch()
        );
        assert_eq!(Csd::parse(0), Err(HostError::Unsupported));

        let scr = Scr::parse((1u64 << 56).to_be_bytes()).unwrap();
        assert_eq!(scr.spec(), SdSpec::V1_10);
        assert!(scr.supports_switch());
        assert!(!Scr::parse(0u64.to_be_bytes()).unwrap().supports_switch());
        assert_eq!(
            Scr::parse((1u64 << 60).to_be_bytes()),
            Err(HostError::Unsupported)
        );

        let mut status = [0u8; 64];
        status[13] = 1 << 1;
        status[16] = 1;
        let status = SwitchStatus::parse(&status).unwrap();
        assert!(status.supports_high_speed());
        assert_eq!(status.selected_access_mode(), 1);

        let mut unsupported = [0u8; 64];
        unsupported[16] = 0x0f;
        let unsupported = SwitchStatus::parse(&unsupported).unwrap();
        assert!(!unsupported.supports_high_speed());
        assert_eq!(unsupported.selected_access_mode(), 0x0f);
    }

    #[derive(Debug)]
    enum Step {
        Reset(Result<(), HostError>),
        Clock(u32),
        Command(u8, u32, Response),
        CommandError(u8, u32, HostError),
        DataCommand(u8, u32, u16, u16, Response),
        Timing(CardTiming),
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
                    Step::Reset(Ok(())),
                    Step::Timing(CardTiming::DefaultSpeed),
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
            match self.steps.pop_front() {
                Some(Step::Reset(result)) => result,
                step => panic!("unexpected reset, expected {step:?}"),
            }
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
                Some(Step::CommandError(index, argument, error)) => {
                    assert_eq!((command.index, command.argument), (index, argument));
                    Err(error)
                }
                Some(Step::DataCommand(index, argument, block_size, blocks, response)) => {
                    assert_eq!((command.index, command.argument), (index, argument));
                    assert_eq!(command.block_size(), block_size as usize);
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

        fn set_timing(&mut self, timing: CardTiming) -> Result<(), HostError> {
            assert!(matches!(self.steps.pop_front(), Some(Step::Timing(value)) if value == timing));
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

    struct FastHost {
        reads: Vec<Command>,
        writes: Vec<u8>,
        write_commands: Vec<Command>,
        resets: usize,
    }

    impl HostController for FastHost {
        fn reset(&mut self) -> Result<(), HostError> {
            unreachable!()
        }

        fn set_clock(&mut self, _hz: u32) -> Result<(), HostError> {
            unreachable!()
        }

        fn command(&mut self, _command: Command) -> Result<Response, HostError> {
            unreachable!("PIO command must not run after a fast transfer")
        }

        fn set_bus_width_4(&mut self) -> Result<(), HostError> {
            unreachable!()
        }

        fn set_timing(&mut self, _timing: CardTiming) -> Result<(), HostError> {
            unreachable!()
        }

        fn wait_buffer_read_ready(&mut self) -> Result<(), HostError> {
            unreachable!()
        }

        fn read_data_word(&mut self) -> Result<u32, HostError> {
            unreachable!()
        }

        fn wait_buffer_write_ready(&mut self) -> Result<(), HostError> {
            unreachable!()
        }

        fn write_data_word(&mut self, _value: u32) -> Result<(), HostError> {
            unreachable!()
        }

        fn wait_transfer_complete(&mut self) -> Result<(), HostError> {
            unreachable!()
        }

        fn reset_data_line(&mut self) {
            self.resets += 1;
        }

        fn max_blocks_per_command(&self) -> usize {
            2
        }

        fn try_read_blocks(
            &mut self,
            command: Command,
            output: &mut [u8],
        ) -> Result<bool, HostError> {
            self.reads.push(command);
            output.fill(0x5a);
            Ok(true)
        }

        fn try_write_blocks(&mut self, command: Command, data: &[u8]) -> Result<bool, HostError> {
            self.write_commands.push(command);
            self.writes.extend_from_slice(data);
            Ok(true)
        }
    }

    #[ktest]
    fn megrez_sdma_multi_sector_transfers_prefer_the_host_fast_path() {
        let card = Card {
            rca: 1,
            nr_sectors: 8,
            timing: CardTiming::DefaultSpeed,
            speed_selection: SpeedSelection::DefaultSpeedUnsupported,
        };
        let mut host = FastHost {
            reads: Vec::new(),
            writes: Vec::new(),
            write_commands: Vec::new(),
            resets: 0,
        };
        let mut read = [0u8; 4 * SECTOR_SIZE];
        card.read_sectors(&mut host, 2, &mut read).unwrap();
        assert_eq!(read, [0x5a; 4 * SECTOR_SIZE]);
        assert_eq!(
            host.reads,
            [
                Command::read_multiple_blocks(2, 2),
                Command::read_multiple_blocks(4, 2),
            ]
        );

        let write = [0xa5; 4 * SECTOR_SIZE];
        card.write_sectors(&mut host, 2, &write).unwrap();
        assert_eq!(host.writes, write);
        assert_eq!(
            host.write_commands,
            [
                Command::write_multiple_blocks(2, 2),
                Command::write_multiple_blocks(4, 2),
            ]
        );
        assert_eq!(host.resets, 0);
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
    fn discovery_negotiates_high_speed_only_after_verified_switch_status() {
        let c_size = 0x1234u128;
        let csd = (1u128 << 126) | ((CSD_COMMAND_CLASS_SWITCH as u128) << 84) | (c_size << 48);
        let mut host = FakeHost::discovery(csd);
        host.steps.extend([
            Step::Command(55, 7 << 16, Response::Short(0)),
            Step::DataCommand(51, 0, 8, 1, Response::Short(0)),
            Step::DataCommand(6, 0x00ff_fff1, 64, 1, Response::Short(0)),
            Step::DataCommand(6, 0x80ff_fff1, 64, 1, Response::Short(0)),
            Step::Timing(CardTiming::HighSpeed),
            Step::Clock(HIGH_SPEED_CLOCK_HZ),
        ]);

        let scr = (1u64 << 56).to_be_bytes();
        let mut check = [0u8; 64];
        check[13] = SWITCH_STATUS_HIGH_SPEED;
        let mut selected = check;
        selected[16] = 1;
        for bytes in [scr.as_slice(), check.as_slice(), selected.as_slice()] {
            host.words.extend(
                bytes
                    .as_chunks::<4>()
                    .0
                    .iter()
                    .copied()
                    .map(u32::from_le_bytes),
            );
        }

        let card = Card::discover(&mut host).unwrap();
        assert_eq!(card.timing(), CardTiming::HighSpeed);
        assert_eq!(card.speed_selection(), SpeedSelection::HighSpeed);
        assert_eq!(card.data_clock_hz(), HIGH_SPEED_CLOCK_HZ);
        host.assert_done();
    }

    fn high_speed_words(selected_mode: u8) -> VecDeque<u32> {
        let scr = (1u64 << 56).to_be_bytes();
        let mut check = [0u8; 64];
        check[13] = SWITCH_STATUS_HIGH_SPEED;
        let mut selected = check;
        selected[16] = selected_mode;
        [scr.as_slice(), check.as_slice(), selected.as_slice()]
            .into_iter()
            .flat_map(|bytes| {
                bytes
                    .as_chunks::<4>()
                    .0
                    .iter()
                    .copied()
                    .map(u32::from_le_bytes)
            })
            .collect()
    }

    #[ktest]
    fn rejected_high_speed_selection_remains_at_default_speed() {
        let csd = (1u128 << 126) | ((CSD_COMMAND_CLASS_SWITCH as u128) << 84);
        let mut host = FakeHost::discovery(csd);
        host.steps.extend([
            Step::Command(55, 7 << 16, Response::Short(0)),
            Step::DataCommand(51, 0, 8, 1, Response::Short(0)),
            Step::DataCommand(6, SWITCH_CHECK_HIGH_SPEED, 64, 1, Response::Short(0)),
            Step::DataCommand(6, SWITCH_SET_HIGH_SPEED, 64, 1, Response::Short(0)),
        ]);
        host.words = high_speed_words(0);

        let card = Card::discover(&mut host).unwrap();
        assert_eq!(card.timing(), CardTiming::DefaultSpeed);
        assert_eq!(
            card.speed_selection(),
            SpeedSelection::DefaultSpeedUnsupported
        );
        assert_eq!(card.data_clock_hz(), DATA_CLOCK_HZ);
        assert_eq!(host.data_resets, 0);
        host.assert_done();
    }

    #[ktest]
    fn pre_switch_transport_error_falls_back_without_reinitializing() {
        let csd = (1u128 << 126) | ((CSD_COMMAND_CLASS_SWITCH as u128) << 84);
        let mut host = FakeHost::discovery(csd);
        host.steps.extend([
            Step::Command(55, 7 << 16, Response::Short(0)),
            Step::DataCommand(51, 0, 8, 1, Response::Short(0)),
            Step::DataCommand(6, SWITCH_CHECK_HIGH_SPEED, 64, 1, Response::Short(0)),
        ]);
        let scr = (1u64 << 56).to_be_bytes();
        host.words.extend(
            scr.as_chunks::<4>()
                .0
                .iter()
                .copied()
                .map(u32::from_le_bytes),
        );

        let card = Card::discover(&mut host).unwrap();
        assert_eq!(card.timing(), CardTiming::DefaultSpeed);
        assert_eq!(
            card.speed_selection(),
            SpeedSelection::DefaultSpeedUnsupported
        );
        assert_eq!(host.data_resets, 1);
        host.assert_done();
    }

    #[ktest]
    fn scr_prefix_transport_error_falls_back_before_card_state_changes() {
        let csd = (1u128 << 126) | ((CSD_COMMAND_CLASS_SWITCH as u128) << 84);
        let mut host = FakeHost::discovery(csd);
        host.steps
            .push_back(Step::CommandError(55, 7 << 16, HostError::CommandCrc));

        let card = Card::discover(&mut host).unwrap();
        assert_eq!(card.timing(), CardTiming::DefaultSpeed);
        assert_eq!(
            card.speed_selection(),
            SpeedSelection::DefaultSpeedUnsupported
        );
        assert_eq!(host.data_resets, 0);
        host.assert_done();
    }

    #[ktest]
    fn ambiguous_switch_error_reinitializes_exactly_once() {
        let csd = (1u128 << 126) | ((CSD_COMMAND_CLASS_SWITCH as u128) << 84);
        let mut host = FakeHost::discovery(csd);
        host.steps.extend([
            Step::Command(55, 7 << 16, Response::Short(0)),
            Step::DataCommand(51, 0, 8, 1, Response::Short(0)),
            Step::DataCommand(6, SWITCH_CHECK_HIGH_SPEED, 64, 1, Response::Short(0)),
            Step::DataCommand(6, SWITCH_SET_HIGH_SPEED, 64, 1, Response::Short(0)),
        ]);
        host.steps.extend(FakeHost::discovery(csd).steps);
        let words = high_speed_words(1);
        host.words.extend(words.into_iter().take(2 + 16));

        let card = Card::discover(&mut host).unwrap();
        assert_eq!(card.timing(), CardTiming::DefaultSpeed);
        assert_eq!(
            card.speed_selection(),
            SpeedSelection::DefaultSpeedRecovered
        );
        assert_eq!(host.data_resets, 1);
        host.assert_done();
    }

    #[ktest]
    fn forced_default_speed_skips_all_optional_card_commands() {
        let csd = (1u128 << 126) | ((CSD_COMMAND_CLASS_SWITCH as u128) << 84);
        let mut host = FakeHost::discovery(csd);

        let card = Card::discover_with_policy(&mut host, true).unwrap();

        assert_eq!(card.timing(), CardTiming::DefaultSpeed);
        assert_eq!(card.speed_selection(), SpeedSelection::DefaultSpeedForced);
        assert_eq!(card.data_clock_hz(), DATA_CLOCK_HZ);
        host.assert_done();
    }

    #[ktest]
    fn failed_ambiguous_recovery_is_returned_without_another_retry() {
        let csd = (1u128 << 126) | ((CSD_COMMAND_CLASS_SWITCH as u128) << 84);
        let mut host = FakeHost::discovery(csd);
        host.steps.extend([
            Step::Command(55, 7 << 16, Response::Short(0)),
            Step::DataCommand(51, 0, 8, 1, Response::Short(0)),
            Step::DataCommand(6, SWITCH_CHECK_HIGH_SPEED, 64, 1, Response::Short(0)),
            Step::DataCommand(6, SWITCH_SET_HIGH_SPEED, 64, 1, Response::Short(0)),
            Step::Reset(Err(HostError::Timeout)),
        ]);
        let words = high_speed_words(1);
        host.words.extend(words.into_iter().take(2 + 16));

        assert_eq!(Card::discover(&mut host), Err(HostError::Timeout));
        assert_eq!(host.data_resets, 1);
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
            timing: CardTiming::DefaultSpeed,
            speed_selection: SpeedSelection::DefaultSpeedUnsupported,
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
            timing: CardTiming::DefaultSpeed,
            speed_selection: SpeedSelection::DefaultSpeedUnsupported,
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
            timing: CardTiming::DefaultSpeed,
            speed_selection: SpeedSelection::DefaultSpeedUnsupported,
        };
        let mut host = FakeHost::discovery(1u128 << 126);
        host.steps = vec![Step::DataCommand(18, 2, 512, 2, Response::Short(0))].into();
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
            timing: CardTiming::DefaultSpeed,
            speed_selection: SpeedSelection::DefaultSpeedUnsupported,
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
            timing: CardTiming::DefaultSpeed,
            speed_selection: SpeedSelection::DefaultSpeedUnsupported,
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
            timing: CardTiming::DefaultSpeed,
            speed_selection: SpeedSelection::DefaultSpeedUnsupported,
        };
        let mut host = FakeHost::discovery(1u128 << 126);
        host.steps = vec![Step::DataCommand(25, 2, 512, 2, Response::Short(0))].into();
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

    #[ktest]
    fn writes_one_standard_sdhci_request_with_one_command() {
        const BLOCKS: usize = 1024;
        let card = Card {
            rca: 1,
            nr_sectors: 2048,
            timing: CardTiming::DefaultSpeed,
            speed_selection: SpeedSelection::DefaultSpeedUnsupported,
        };
        let mut host = FakeHost::discovery(1u128 << 126);
        host.steps = vec![Step::DataCommand(
            25,
            2,
            512,
            BLOCKS as u16,
            Response::Short(0),
        )]
        .into();
        let mut sectors = vec![0u8; BLOCKS * SECTOR_SIZE];
        sectors[0..4].copy_from_slice(&1u32.to_le_bytes());
        sectors[(BLOCKS - 1) * SECTOR_SIZE..(BLOCKS - 1) * SECTOR_SIZE + 4]
            .copy_from_slice(&2u32.to_le_bytes());

        card.write_sectors(&mut host, 2, &sectors).unwrap();

        assert_eq!(host.written_words.len(), BLOCKS * SECTOR_SIZE / 4);
        assert_eq!(host.written_words[0], 1);
        assert_eq!(host.written_words[(BLOCKS - 1) * SECTOR_SIZE / 4], 2);
        assert_eq!(host.data_resets, 0);
        host.assert_done();
    }
}
