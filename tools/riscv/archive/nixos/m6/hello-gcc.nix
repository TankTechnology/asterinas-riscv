# M6 "hello" (path A): compile hello.c in the guest with the Alpine riscv64
# gcc. Currently BLOCKED: gcc and cc1 are non-PIE (ET_EXEC) dynamically-linked
# binaries, and Asterinas does not yet execute those (they exit 0 with no
# output). Kept as the reference for once the ET_EXEC loader gap is fixed.
#
# Requires `build_m6.sh --with-gcc` to put gcc/binutils/make/musl-dev into the
# rootfs, and `--impure` because hello.c is referenced by absolute path.
builtins.derivation {
  name = "m6-hello";
  system = "riscv64-linux";
  builder = "/bin/sh";
  args = [ "-c" "mkdir -p $out/bin && gcc /m6/hello.c -O2 -o $out/bin/hello" ];
  PATH = "/usr/bin:/bin:/usr/sbin:/sbin";
}
