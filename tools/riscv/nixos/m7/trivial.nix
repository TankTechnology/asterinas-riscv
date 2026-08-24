# M7 minimal validation through the daemon: a derivation whose builder writes a
# fixed string to $out. Exercises the daemon's full "realise a derivation" path.
builtins.derivation {
  name = "m7-trivial";
  system = "riscv64-linux";
  builder = "/bin/sh";
  args = [ "-c" "echo -n hello-from-daemon > $out" ];
}
