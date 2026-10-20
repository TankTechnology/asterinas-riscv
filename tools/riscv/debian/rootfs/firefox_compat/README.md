# Firefox compatibility overlays

`0001-recognize-virgl-webgl-renderer.patch` is a source patch for the exact
Debian ESR source package used by the QEMU DRM rootfs (`firefox-esr
140.16.0esr-1~deb13u1`). It is intentionally stored separately from the
signed Debian package path: applying it requires a Debian Trixie RISC-V build,
the resulting package must have a new provenance and hash, and the rootfs
builder must admit that package through an explicit development overlay.

`0002-prefer-drm-render-node-for-webrender.patch` is a second source-audited
candidate for the Firefox EGL boundary. It makes the existing
`forceAccel` WebRender request try the configured DRM render node even when
GTK has an X11 display, while preserving the existing EGL fallback. It has
only been applied with zero fuzz to the audited source snapshot; it has not
been built or used in a runtime image.

Run `source_patch_gate.py` with
`source_patch_manifest.json` to verify the pinned four-file source snapshot,
both patch hashes, and ordered `patch --dry-run --fuzz=0` applicability in a
disposable tree. The gate is source-only and does not assert a built package
or a WebRender runtime result.

After a real patched Debian package is built, run
`package_provenance_gate.py --package <deb> --provenance <json> --compat-root
<this-directory>`. Its provenance envelope must bind the package hash and
Debian control identity to the source manifest, both patch hashes, the source
`.dsc` hash, and an immutable builder image digest. It only admits metadata;
rootfs installation and QEMU WebRender remain separate gates.

`source_package_manifest.json` pins the matching Debian Security ESR 140
descriptor and the two core source archives. It is an input anchor, not a
signature-verification result; the build environment must verify the `.dsc`
signature before downloading or unpacking the archives.

`source_package_gate.py --manifest source_package_manifest.json --dsc <file>`
rechecks the descriptor hash, package/version/format fields, and both
`Checksums-Sha256` rows before a build starts. It does not verify the embedded
OpenPGP signature; that remains a builder prerequisite.

For a disposable development root, `compat_overlay.py` performs the package
gate, audits archive paths and symlinks, requires the Firefox executable and
`libxul.so`, and writes a deterministic
`/usr/share/asterinas/firefox-riscv-compat-overlay.json` marker. It leaves the
dpkg database untouched; this path must remain separate from the signed Debian
package claim.

`build_firefox_esr.sh` is the controlled build entry: it requires an immutable
Docker image reference, runs both source gates, enforces a 12 GiB free-space
floor, applies the patches with zero fuzz in a disposable copy, and runs the
package provenance gate on the resulting `.deb`. It never changes the source
tree and does not start when the preflight fails.

This patch only fixes truthful WebGL renderer sanitization. It does not claim
that WebRender's EGL context creation is fixed; the QEMU r47 receipt still
shows a null WebRender provider. A patched package cannot be used as a Task 6
pass until the strict QEMU scene gate also records `renderer=virgl`, a
screenshot hash, frame samples, and restart recovery.
