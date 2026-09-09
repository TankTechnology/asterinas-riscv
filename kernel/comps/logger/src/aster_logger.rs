// SPDX-License-Identifier: MPL-2.0

use alloc::string::String;
use core::time::Duration;

use ostd::{
    log::{Level, LevelFilter, Record},
    timer::Jiffies,
};
use spin::Once;

use super::klog::LogRecord;

static CAPTURE_LEVEL: Once<String> = Once::new();

aster_cmdline::define_kv_param!("asterinas.klog_capture", CAPTURE_LEVEL);

/// The logger used for Asterinas.
struct AsterLogger;

static LOGGER: AsterLogger = AsterLogger;

impl ostd::log::Log for AsterLogger {
    fn log(&self, record: &Record) {
        let timestamp = Jiffies::elapsed().as_duration();
        let log = super::klog::klog();
        let prepared = log.append(record, &timestamp);
        if log.should_print(record.level()) {
            print_logs(record.level(), &prepared, &timestamp);
        }
    }
}

#[cfg(feature = "log_color")]
pub(super) fn print_logs(level: Level, record: &LogRecord, timestamp: &Duration) {
    use owo_colors::Style;

    let secs = timestamp.as_secs();
    let millis = timestamp.subsec_millis();

    let timestamp_style = Style::new().green();
    let record_style = Style::new().default_color();
    let level_style = match level {
        Level::Error => Style::new().red(),
        Level::Warning => Style::new().bright_yellow(),
        Level::Info => Style::new().blue(),
        Level::Debug => Style::new().bright_green(),
        Level::Notice => Style::new().cyan(),
        Level::Emerg | Level::Alert | Level::Crit => Style::new().red().bold(),
    };

    super::_print(format_args!(
        "{} {:<6}: {}\n",
        timestamp_style.style(format_args!("[{:>6}.{:03}]", secs, millis)),
        level_style.style(level),
        record_style.style(record.message())
    ));
}

#[cfg(not(feature = "log_color"))]
pub(super) fn print_logs(level: Level, record: &LogRecord, timestamp: &Duration) {
    let secs = timestamp.as_secs();
    let millis = timestamp.subsec_millis();

    super::_print(format_args!(
        "[{:>6}.{:03}] {:<6}: {}\n",
        secs,
        millis,
        level,
        record.message()
    ));
}

pub(super) fn init() {
    let boot_level = ostd::log::max_level();
    let requested_capture = CAPTURE_LEVEL.get().map(String::as_str);
    let capture_level = requested_capture.and_then(super::klog::parse_level_filter);
    let invalid_capture = requested_capture.is_some() && capture_level.is_none();
    let effective_level = super::klog::effective_level_filter(boot_level as u8, capture_level);

    super::klog::klog().set_console_level(boot_level as u8);
    ostd::log::inject_logger(&LOGGER);
    ostd::log::set_max_level(LevelFilter::from_u8(effective_level));

    if invalid_capture {
        ostd::warn!(
            "invalid asterinas.klog_capture value '{}'; using warning",
            requested_capture.unwrap()
        );
    }
}
