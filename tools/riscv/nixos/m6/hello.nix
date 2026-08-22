# M6 "hello" (path B): install a prebuilt riscv64 hello into /nix/store.
#
# The binary is cross-compiled on the host by build_m6.sh (riscv64 musl, PIE)
# and bundled at /m6/hello-prebuilt. The builder is busybox sh (PIE), which
# copies it into $out/bin/hello. Running $out/bin/hello prints "Hello, world!".
#
# Path A (compile in the guest with the Alpine gcc — see hello-gcc.nix) is
# blocked by a kernel gap: gcc/cc1 are non-PIE (ET_EXEC) and Asterinas does not
# yet execute dynamically-linked ET_EXEC binaries (M6-report.md).
builtins.derivation {
  name = "m6-hello";
  system = "riscv64-linux";
  builder = "/bin/sh";
  args = [ "-c" "mkdir -p $out/bin && cp /m6/hello-prebuilt $out/bin/hello && chmod 755 $out/bin/hello" ];
  PATH = "/usr/bin:/bin:/usr/sbin:/sbin";
}
