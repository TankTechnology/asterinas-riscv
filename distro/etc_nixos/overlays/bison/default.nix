# Bison's test suite is not reliable under qemu-user emulation: three
# "nondeterministic parse" tests fail deterministically in the RISC-V
# cross build. The suite does not affect the produced binaries, so
# disable it for the emulated build (bison runs the suite in
# installCheckPhase; its doCheck is already false).
final: prev: {
  bison = prev.bison.overrideAttrs (old: { doInstallCheck = false; });
}
