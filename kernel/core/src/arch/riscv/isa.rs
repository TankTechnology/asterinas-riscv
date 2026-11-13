// SPDX-License-Identifier: MPL-2.0

//! The platform's ISA string, the `mmu-type` it reports, and the `AT_HWCAP`
//! bits derived from the ISA.
//!
//! `AT_HWCAP` is how user space learns which single-letter extensions the harts
//! implement. glibc dispatches its ifunc variants on it, and language runtimes
//! (V8, the JITs) pick their code paths from it. A kernel that leaves it unset
//! sends every program down the baseline path — correct, but slower — and makes
//! CPU feature detection report nothing at all.

use alloc::string::String;

use spin::Once;

static ISA: Once<String> = Once::new();

/// Used when the device tree does not describe the CPUs. The kernel is built
/// for `riscv64gc`, which is the `imafdc` set.
const FALLBACK_ISA: &str = "rv64imafdc";

/// Returns the ISA string of the current platform, e.g. `rv64imafdc_zicsr_zifencei`.
///
/// The value comes from the device tree (`/cpus/cpu@N/riscv,isa`), which is what
/// the firmware reported for the harts actually present, and is cached because
/// both `/proc/cpuinfo` and every `exec` read it.
pub(crate) fn isa_string() -> &'static str {
    ISA.call_once(|| {
        ostd::arch::boot::DEVICE_TREE
            .get()
            .and_then(|fdt| fdt.cpus().next())
            .and_then(|cpu| cpu.property("riscv,isa"))
            .and_then(|property| property.as_str())
            .map(String::from)
            .unwrap_or_else(|| String::from(FALLBACK_ISA))
    })
}

/// Returns the MMU type reported by the device tree, without the vendor prefix
/// (`sv39` rather than `riscv,sv39`), or `None` when the device tree omits it.
pub(crate) fn mmu_type() -> Option<&'static str> {
    static MMU: Once<Option<String>> = Once::new();
    MMU.call_once(|| {
        ostd::arch::boot::DEVICE_TREE
            .get()
            .and_then(|fdt| fdt.cpus().find(|cpu| cpu.property("mmu-type").is_some()))
            .and_then(|cpu| cpu.property("mmu-type"))
            .and_then(|property| property.as_str())
            .map(|value| String::from(value.strip_prefix("riscv,").unwrap_or(value)))
    })
    .as_deref()
}

/// The single-letter extensions, i.e. the part between the `rv64` prefix and
/// the first multi-letter extension.
fn single_letter_extensions(isa: &str) -> &str {
    let letters = isa
        .strip_prefix("rv64")
        .or_else(|| isa.strip_prefix("rv32"))
        .unwrap_or(isa);
    match letters.find('_') {
        Some(end) => &letters[..end],
        None => letters,
    }
}

/// Returns the `(AT_HWCAP, AT_HWCAP2)` words for this platform.
///
/// RISC-V assigns one bit per single-letter extension: the bit index is the
/// uppercase letter's offset from `A` (so `A` is bit 0, `C` bit 2, `D` bit 3,
/// `F` bit 5, `I` bit 8, `M` bit 12, `V` bit 21), the same encoding Linux uses.
/// Multi-letter extensions are reported through `riscv_hwprobe`, not here.
pub(crate) fn hwcap() -> (u64, u64) {
    let mut hwcap = 0u64;
    for letter in single_letter_extensions(isa_string()).bytes() {
        if letter.is_ascii_alphabetic() {
            hwcap |= 1u64 << (letter.to_ascii_uppercase() - b'A');
        }
    }
    (hwcap, 0)
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn single_letter_part_stops_at_the_first_multi_letter_extension() {
        assert_eq!(
            single_letter_extensions("rv64imafdc_zicsr_zifencei"),
            "imafdc"
        );
        assert_eq!(single_letter_extensions("rv64imac"), "imac");
        assert_eq!(single_letter_extensions("imafdc"), "imafdc");
    }

    #[ktest]
    fn hwcap_uses_the_linux_bit_encoding() {
        // `rv64imafdc` must produce exactly the bits Linux sets on the same
        // hart: A=0, C=2, D=3, F=5, I=8, M=12.
        let isa = "rv64imafdc";
        let mut expected = 0u64;
        for letter in [b'A', b'C', b'D', b'F', b'I', b'M'] {
            expected |= 1u64 << (letter - b'A');
        }
        let mut actual = 0u64;
        for letter in single_letter_extensions(isa).bytes() {
            if letter.is_ascii_alphabetic() {
                actual |= 1u64 << (letter.to_ascii_uppercase() - b'A');
            }
        }
        assert_eq!(actual, expected);
        // The `rv` prefix and the digits must not leak into the mask.
        assert_eq!(actual & 0b11, 0b01, "only bit A (0) may be set among A/B");
    }
}
