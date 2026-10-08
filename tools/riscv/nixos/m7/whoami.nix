# M7 multi-user proof: the builder writes its own username into $out. With
# `build-users-group = nixbld` the daemon drops the builder's privileges to a
# nixbld member (e.g. nixbld1); without multi-user mode it would be `root`.
builtins.derivation {
  name = "m7-whoami";
  system = "riscv64-linux";
  builder = "/bin/sh";
  args = [ "-c" "id -un > $out" ];
  PATH = "/usr/bin:/bin:/usr/sbin:/sbin";
}
