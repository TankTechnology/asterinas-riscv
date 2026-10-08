// SPDX-License-Identifier: MPL-2.0

//! Standalone tests of the actual kernel log store, without an OS boot.

#[path = "store.rs"]
mod store;

use store::{ConsolePolicy, LogRecord, MESSAGE_CAPACITY, ReadError, RecordRing};

fn record(text: &[u8]) -> LogRecord {
    LogRecord::new(6, 1234567, text)
}

#[test]
fn empty_and_independent_readers() {
    let mut ring = RecordRing::<2>::new();
    assert_eq!(ring.bounds(), (0, 0, 0));
    assert!(matches!(ring.record(0), Err(ReadError::Empty)));
    assert!(ring.push(record(b"first")));
    let mut a = String::new();
    let mut b = String::new();
    ring.record(0).unwrap().format_kmsg(&mut a).unwrap();
    ring.record(0).unwrap().format_kmsg(&mut b).unwrap();
    assert_eq!(a, "6,0,1234567,-;first\n");
    assert_eq!(a, b);
    assert_eq!(ring.bounds(), (0, 1, 0));
    assert_eq!(ring.record(0).unwrap().sequence(), 0);
}

#[test]
fn overwrite_preserves_whole_records_and_reports_gap() {
    let mut ring = RecordRing::<2>::new();
    ring.push(record(b"first"));
    ring.push(record(b"second"));
    ring.push(record(b"third"));
    assert_eq!(ring.bounds(), (1, 3, 0));
    assert!(matches!(ring.record(0), Err(ReadError::Overrun(1))));
    let mut out = String::new();
    ring.record(1).unwrap().format_kmsg(&mut out).unwrap();
    assert_eq!(out, "6,1,1234567,-;second\n");
    assert!(matches!(ring.record(3), Err(ReadError::Empty)));
}

#[test]
fn clear_only_moves_marker_and_does_not_hide_later_records() {
    let mut ring = RecordRing::<2>::new();
    ring.push(record(b"before"));
    let snapshot_end = ring.bounds().1;
    ring.push(record(b"after"));
    ring.clear_to(snapshot_end);
    assert_eq!(ring.bounds(), (0, 2, 1));
    assert!(ring.record(0).is_ok());
    ring.clear_to(0);
    assert_eq!(ring.bounds().2, 1);
    ring.clear_to(u64::MAX);
    assert_eq!(ring.bounds().2, 2);
}

#[test]
fn device_output_escapes_untrusted_bytes_and_removes_one_trailing_newline() {
    let mut out = String::new();
    LogRecord::new(14, 9, b"a\n\x00\\\xff\n")
        .format_kmsg(&mut out)
        .unwrap();
    assert_eq!(out, "14,0,9,-;a\\x0a\\x00\\x5c\\xff\n");
}

#[test]
fn syslog_text_has_priority_timestamp_and_multiline_prefixes() {
    let mut out = String::new();
    record(b"one\ntwo\n").format_syslog(&mut out).unwrap();
    assert_eq!(out, "<6>[    1.234567] one\n<6>[    1.234567] two\n");
}

#[test]
fn oversized_records_are_explicitly_truncated() {
    let mut out = String::new();
    record(&vec![b'x'; MESSAGE_CAPACITY + 5])
        .format_kmsg(&mut out)
        .unwrap();
    assert!(out.ends_with("[truncated]\n"));
    assert_eq!(out.matches("[truncated]").count(), 1);
    assert!(out.len() < MESSAGE_CAPACITY + 64);
}

#[test]
fn bounded_formatter_stops_accepting_after_overflow() {
    use std::fmt::Write;
    let mut text = store::Message::new();
    assert!(text.write_str(&"x".repeat(MESSAGE_CAPACITY - 2)).is_ok());
    assert!(text.write_str("too long").is_err());
    assert!(text.write_str("later").is_err());
    let mut out = String::new();
    LogRecord::from_message(4, 0, text)
        .format_kmsg(&mut out)
        .unwrap();
    assert!(out.ends_with("[truncated]\n"));
    assert!(!out.contains("later"));
}

#[test]
fn console_off_on_and_override_do_not_touch_records() {
    let mut policy = ConsolePolicy::new(7);
    policy.disable();
    assert_eq!(policy.level(), 1);
    policy.disable();
    policy.enable();
    assert_eq!(policy.level(), 7);
    policy.disable();
    policy.set_level(4);
    policy.enable();
    assert_eq!(policy.level(), 4);
    assert!(policy.should_print(3));
    assert!(!policy.should_print(4));
    policy.set_level(0);
    assert!(!policy.should_print(0));
}

#[test]
fn only_one_final_newline_is_removed() {
    let mut out = String::new();
    record(b"one\n\n").format_kmsg(&mut out).unwrap();
    assert_eq!(out, "6,0,1234567,-;one\\x0a\n");
}

#[test]
fn syslog_bytes_preserve_non_utf8_user_records() {
    let mut out = Vec::new();
    record(b"\xff\x00\n").write_syslog(|bytes| out.extend_from_slice(bytes));
    assert_eq!(out, b"<6>[    1.234567] \xff\x00\n");
}

#[test]
fn multiple_rollovers_match_a_reference_queue() {
    let mut ring = RecordRing::<7>::new();
    let mut expected = std::collections::VecDeque::new();
    for sequence in 0..10000u64 {
        let text = sequence.to_string();
        ring.push(record(text.as_bytes()));
        expected.push_back((sequence, text));
        if expected.len() > 7 {
            expected.pop_front();
        }
        for (seq, text) in &expected {
            let mut out = String::new();
            ring.record(*seq).unwrap().format_kmsg(&mut out).unwrap();
            assert_eq!(out, format!("6,{seq},1234567,-;{text}\n"));
        }
    }
}

#[test]
fn log_arguments_are_evaluated_once_for_all_outputs() {
    struct Stateful(std::cell::Cell<usize>);
    impl std::fmt::Display for Stateful {
        fn fmt(&self, writer: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            let value = self.0.get();
            self.0.set(value + 1);
            write!(writer, "value={value}")
        }
    }
    let value = Stateful(std::cell::Cell::new(0));
    let prepared = LogRecord::formatted(4, 10, format_args!("module: {value}"));
    let mut device = String::new();
    prepared.format_kmsg(&mut device).unwrap();
    assert_eq!(prepared.message().to_string(), "module: value=0");
    assert!(device.ends_with(";module: value=0\n"));
    assert_eq!(value.0.get(), 1);
}

#[test]
fn capture_level_accepts_linux_spelling_and_rejects_invalid_values() {
    for (text, expected) in [
        ("0", 0),
        ("8", 8),
        ("off", 0),
        ("warn", 5),
        ("warning", 5),
        ("info", 7),
        ("debug", 8),
    ] {
        assert_eq!(store::parse_level_filter(text), Some(expected));
    }
    for text in ["", "9", "Info", "6x", "-1"] {
        assert_eq!(store::parse_level_filter(text), None);
    }
}

#[test]
fn capture_threshold_never_reduces_console_threshold() {
    assert_eq!(store::effective_level_filter(0, None), 5);
    assert_eq!(store::effective_level_filter(0, Some(7)), 7);
    assert_eq!(store::effective_level_filter(8, Some(5)), 8);
}
