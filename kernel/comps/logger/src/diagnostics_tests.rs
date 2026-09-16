// SPDX-License-Identifier: MPL-2.0

//! Host tests of the actual allocation-free measurement accumulator.

#[path = "diagnostics_stats.rs"]
mod stats;

use stats::DurationStats;

#[test]
fn empty_is_not_a_zero_latency_measurement() {
    let stats = DurationStats::new();
    assert_eq!(stats.count, 0);
    assert_eq!(stats.total_ticks, 0);
    assert_eq!(stats.max_ticks, 0);
    assert_eq!(stats.invalid, 0);
}

#[test]
fn accumulates_count_total_and_maximum() {
    let mut stats = DurationStats::new();
    stats.observe(Some(10), Some(14));
    stats.observe(Some(20), Some(29));
    stats.observe(Some(30), Some(30));
    assert_eq!(stats.count, 3);
    assert_eq!(stats.total_ticks, 13);
    assert_eq!(stats.max_ticks, 9);
    assert_eq!(stats.invalid, 0);
}

#[test]
fn unavailable_or_reversed_clock_is_not_accepted() {
    let mut stats = DurationStats::new();
    stats.observe(None, Some(14));
    stats.observe(Some(10), None);
    stats.observe(Some(20), Some(19));
    assert_eq!(stats.count, 0);
    assert_eq!(stats.total_ticks, 0);
    assert_eq!(stats.invalid, 3);
}

#[test]
fn overflow_does_not_partially_update_a_sample() {
    let mut stats = DurationStats::new();
    stats.observe(Some(0), Some(u64::MAX));
    stats.observe(Some(0), Some(1));
    assert_eq!(stats.count, 1);
    assert_eq!(stats.total_ticks, u64::MAX);
    assert_eq!(stats.max_ticks, u64::MAX);
    assert_eq!(stats.invalid, 1);
    stats.count = u64::MAX;
    stats.observe(Some(5), Some(5));
    assert_eq!(stats.count, u64::MAX);
    assert_eq!(stats.invalid, 2);
}

#[test]
fn independent_thread_owned_measurements_do_not_mix() {
    let handles: Vec<_> = (1..=4)
        .map(|duration| {
            std::thread::spawn(move || {
                let mut stats = DurationStats::new();
                for _ in 0..1000 {
                    stats.observe(Some(0), Some(duration));
                }
                stats
            })
        })
        .collect();
    for (index, handle) in handles.into_iter().enumerate() {
        let stats = handle.join().unwrap();
        assert_eq!(stats.count, 1000);
        assert_eq!(stats.total_ticks, (index as u64 + 1) * 1000);
        assert_eq!(stats.invalid, 0);
    }
}
