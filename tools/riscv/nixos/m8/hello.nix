# M8 "hello" for the lightweight route: install a prebuilt riscv64 hello into
# /nix/store. The binary is cross-compiled on the host by build_m8_light.sh and
# bundled at /m8/hello-prebuilt (same path B as M6/M7 — path A is still blocked
# by the ET_EXEC loader gap).
builtins.derivation {
  name = "m8-hello";
  system = "riscv64-linux";
  builder = "/bin/sh";
  args = [ "-c" "mkdir -p $out/bin && cp /m8/hello-prebuilt $out/bin/hello && chmod 755 $out/bin/hello" ];
  PATH = "/usr/bin:/bin:/usr/sbin:/sbin";
}
