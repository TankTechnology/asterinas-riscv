# Coreutils' test suite has interactive (mv -i) and timing-sensitive
# (tail --follow) tests that fail under qemu-user emulation; the
# produced binaries are unaffected. Skip the suite for the emulated
# build; native builds only lose the redundant check.
final: prev: {
  coreutils = prev.coreutils.overrideAttrs (old: { doCheck = false; });
}
