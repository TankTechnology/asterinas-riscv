// SPDX-License-Identifier: MPL-2.0

fn main() {
    // The Kani proof harnesses are only compiled under `cargo kani`, which sets
    // `cfg(kani)`; declare it so a normal build does not warn about it.
    println!("cargo::rustc-check-cfg=cfg(kani)");
}
