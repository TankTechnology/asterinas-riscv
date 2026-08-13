# M6 "hello": compile hello.c in the guest with the Alpine riscv64 gcc and
# place the result at $out/bin/hello. The source is referenced by absolute path
# and therefore requires `--impure` (sandbox is disabled, so the builder sees
# the whole filesystem).
builtins.derivation {
  name = "m6-hello";
  system = "riscv64-linux";
  builder = "/bin/sh";
  args = [ "-c" "mkdir -p $out/bin && gcc /m6/hello.c -O2 -o $out/bin/hello" ];
}
