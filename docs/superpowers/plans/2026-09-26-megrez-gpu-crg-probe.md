# Megrez GPU CRG probe implementation plan

1. Add pure clock/reset binding validation tests, including one changed clock
   ID and one changed reset mask. Confirm the red test before implementation.
2. Add the independently gated, read-only CRG snapshot in the RISC-V DRM
   diagnostic module. Validate the exact prepared DTB provider/aperture and
   bind tuple before `IoMem::acquire`; map only offsets `0x12c..0x138` and
   `0x404..0x408`.
3. Run the focused QEMU kernel tests, cached RISC-V kernel build, formatting,
   and `git diff --check`. Commit and push the code on `main`.
4. Stage the exact Image, prepared DTB, and Stage1 as an immutable candidate.
   Record host/board hashes, U-Boot length and CRC checks, the serial CRG line,
   fresh root UID/boot ID, desktop readiness, and close/reopen serial control.
   Retain the software recovery timer until checks pass.

This plan stops at a read-only clock/reset status. A later implementation must
add controlled clock/reset writes and powered GPU identity only after a
hardware contract review.
