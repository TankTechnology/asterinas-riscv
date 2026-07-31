// SPDX-License-Identifier: MPL-2.0

//! RISC-V UART selection and initialization.
//!
//! The firmware-selected `stdout-path` takes precedence.
//! The first NS16550A is retained as a fallback for legacy device trees without that property.

use fdt::node::FdtNode;
use ostd::arch::boot;

mod dw_apb;
mod ns16550a;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum StdoutSelector<'a> {
    Selected(&'a str),
    LegacyFallback,
    Invalid,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum UartKind {
    Ns16550a,
    DwApb,
}

pub(super) fn init() {
    let device_tree = boot::DEVICE_TREE.get().unwrap();
    let stdout_path = match device_tree
        .find_node("/chosen")
        .and_then(|chosen| chosen.property("stdout-path"))
    {
        Some(property) => {
            let Some(path) = property.as_str() else {
                ostd::info!("invalid 'stdout-path' property");
                return;
            };
            Some(path)
        }
        None => None,
    };

    let Some(uart_node) = resolve_uart_node(
        stdout_path,
        |selector| {
            let selected_path =
                resolve_selected_path(selector, |alias| device_tree.aliases()?.resolve(alias))?;
            if !ancestor_buses_are_usable(selected_path, |path| {
                device_tree.find_node(path).is_some_and(|node| {
                    node_is_enabled(node)
                        && node
                            .property("ranges")
                            .is_some_and(|property| property.value.is_empty())
                })
            }) {
                return None;
            }
            device_tree.find_node(selected_path)
        },
        || {
            device_tree.all_nodes().find(|node| {
                node_is_enabled(*node)
                    && node.compatible().is_some_and(|compatibles| {
                        compatibles.all().any(|value| value == "ns16550a")
                    })
            })
        },
    ) else {
        if stdout_path.is_some() {
            ostd::info!("failed to resolve UART from 'stdout-path'");
        }
        return;
    };

    if !node_is_enabled(uart_node) {
        ostd::info!("selected UART is disabled or has an invalid 'status'");
        return;
    }

    let Some(uart_kind) = uart_node
        .compatible()
        .and_then(|compatibles| classify_compatibles(compatibles.all()))
    else {
        ostd::info!("unsupported UART selected by 'stdout-path'");
        return;
    };

    match uart_kind {
        UartKind::Ns16550a => ns16550a::init(uart_node),
        UartKind::DwApb => dw_apb::init(uart_node),
    }
}

fn node_is_enabled(node: FdtNode) -> bool {
    let Some(status) = node.property("status") else {
        return true;
    };

    status.as_str().is_some_and(explicit_status_is_enabled)
}

fn explicit_status_is_enabled(status: &str) -> bool {
    matches!(status, "ok" | "okay")
}

fn parse_stdout_path(stdout_path: Option<&str>) -> StdoutSelector<'_> {
    match stdout_path {
        None => StdoutSelector::LegacyFallback,
        Some(value) => match value
            .split_once(':')
            .map_or(value, |(selector, _)| selector)
        {
            "" => StdoutSelector::Invalid,
            selector => StdoutSelector::Selected(selector),
        },
    }
}

fn classify_compatibles<'a>(compatibles: impl IntoIterator<Item = &'a str>) -> Option<UartKind> {
    let mut has_ns16550a_compatible = false;

    for compatible in compatibles {
        if compatible == "snps,dw-apb-uart" {
            return Some(UartKind::DwApb);
        }
        has_ns16550a_compatible |= compatible == "ns16550a";
    }

    has_ns16550a_compatible.then_some(UartKind::Ns16550a)
}

fn resolve_uart_node<T>(
    stdout_path: Option<&str>,
    resolve_fn: impl FnOnce(&str) -> Option<T>,
    legacy_fallback_fn: impl FnOnce() -> Option<T>,
) -> Option<T> {
    match parse_stdout_path(stdout_path) {
        StdoutSelector::Selected(selector) => resolve_fn(selector),
        StdoutSelector::LegacyFallback => legacy_fallback_fn(),
        StdoutSelector::Invalid => None,
    }
}

fn resolve_selected_path<'a>(
    selector: &'a str,
    resolve_alias_fn: impl FnOnce(&str) -> Option<&'a str>,
) -> Option<&'a str> {
    let path = if selector.starts_with('/') {
        selector
    } else {
        resolve_alias_fn(selector)?
    };

    path.starts_with('/').then_some(path)
}

fn ancestor_buses_are_usable(
    selected_path: &str,
    mut is_usable_fn: impl FnMut(&str) -> bool,
) -> bool {
    if !selected_path.starts_with('/') || selected_path[1..].split('/').any(str::is_empty) {
        return false;
    }

    selected_path
        .match_indices('/')
        .skip(1)
        .all(|(index, _)| is_usable_fn(&selected_path[..index]))
}

#[cfg(ktest)]
mod tests {
    use core::cell::Cell;

    use ostd::prelude::*;

    use super::*;

    #[ktest]
    fn uart_selection_strips_serial_options() {
        assert_eq!(
            parse_stdout_path(Some("serial1:115200n8")),
            StdoutSelector::Selected("serial1")
        );
    }

    #[ktest]
    fn uart_selection_keeps_absolute_path() {
        assert_eq!(
            parse_stdout_path(Some("/soc/serial@10000000")),
            StdoutSelector::Selected("/soc/serial@10000000")
        );
    }

    #[ktest]
    fn uart_selection_rejects_present_empty_selector() {
        assert_eq!(
            parse_stdout_path(Some(":115200n8")),
            StdoutSelector::Invalid
        );
    }

    #[ktest]
    fn uart_selection_uses_fallback_only_when_property_is_absent() {
        assert_eq!(parse_stdout_path(None), StdoutSelector::LegacyFallback);
    }

    #[ktest]
    fn uart_selection_accepts_only_enabled_explicit_status_values() {
        assert!(explicit_status_is_enabled("ok"));
        assert!(explicit_status_is_enabled("okay"));
        assert!(!explicit_status_is_enabled("disabled"));
        assert!(!explicit_status_is_enabled("invalid"));
    }

    #[ktest]
    fn uart_selection_checks_the_complete_compatible_list() {
        assert_eq!(
            classify_compatibles(["vendor,soc-uart", "snps,dw-apb-uart"]),
            Some(UartKind::DwApb)
        );
        assert_eq!(classify_compatibles(["ns16550a"]), Some(UartKind::Ns16550a));
        assert_eq!(classify_compatibles(["vendor,unknown"]), None);
    }

    #[ktest]
    fn uart_selection_chooses_the_named_uart_among_multiple_nodes() {
        let nodes = [
            ("serial0", 0x5090_0000),
            ("serial1", 0x5091_0000),
            ("serial2", 0x5092_0000),
        ];

        let selected = resolve_uart_node(
            Some("serial1:115200n8"),
            |selector| {
                nodes
                    .iter()
                    .find(|(name, _)| *name == selector)
                    .map(|(_, base)| *base)
            },
            || Some(nodes[0].1),
        );

        assert_eq!(selected, Some(0x5091_0000));
    }

    #[ktest]
    fn uart_selection_does_not_fallback_when_selected_node_is_missing() {
        let fallback_called = Cell::new(false);

        let selected = resolve_uart_node(
            Some("serial9:115200n8"),
            |_| None::<usize>,
            || {
                fallback_called.set(true);
                Some(0x1000_0000)
            },
        );

        assert_eq!(selected, None);
        assert!(!fallback_called.get());
    }

    #[ktest]
    fn uart_selection_does_not_fallback_for_an_empty_selector() {
        let fallback_called = Cell::new(false);

        let selected = resolve_uart_node(
            Some(":115200n8"),
            |_| None::<usize>,
            || {
                fallback_called.set(true);
                Some(0x1000_0000)
            },
        );

        assert_eq!(selected, None);
        assert!(!fallback_called.get());
    }

    #[ktest]
    fn uart_selection_resolves_aliases_to_absolute_paths() {
        assert_eq!(
            resolve_selected_path("serial1", |alias| {
                (alias == "serial1").then_some("/soc/serial@50900000")
            }),
            Some("/soc/serial@50900000")
        );
        assert_eq!(
            resolve_selected_path("/soc/serial@50900000", |_| None),
            Some("/soc/serial@50900000")
        );
        assert_eq!(resolve_selected_path("missing", |_| None), None);
        assert_eq!(
            resolve_selected_path("serial1", |_| Some("soc/serial@50900000")),
            None
        );
    }

    #[ktest]
    fn uart_selection_accepts_only_usable_ancestors() {
        assert!(ancestor_buses_are_usable(
            "/soc/serial@50900000",
            |path| path == "/soc"
        ));
        assert!(ancestor_buses_are_usable(
            "/soc/apb/serial@50900000",
            |path| matches!(path, "/soc" | "/soc/apb")
        ));
        assert!(!ancestor_buses_are_usable("/soc/serial@50900000", |_| {
            false
        }));
    }
}
