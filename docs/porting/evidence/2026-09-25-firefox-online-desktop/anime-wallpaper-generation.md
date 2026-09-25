# Anime wallpaper generation

The original bitmap was generated with Codex's built-in `image_gen` tool. No
external illustration, character, franchise, or artist style was copied. The
first 16:9 draft had SHA-256
`ba925f2b52f6cf3f077b488af1a558db6c2e84be1c291b06cedd48e95aa7a23f`.
The second call used that draft as its reference image and extended the scene
to approximately 16:10. The checked-in 1586×992 PNG is
[`desktop_anime_wallpaper.png`](../../../../tools/riscv/debian/rootfs/desktop_anime_wallpaper.png),
SHA-256 `a784c14458f701ba3a4ee2cea0d24ed3297d2608de6c92067b5f2fca2c6348eb`.

Initial prompt:

> Use case: stylized-concept. Asset type: original anime-style desktop wallpaper
> for an Asterinas Debian desktop on a 16:9 HDMI monitor. Create a wide cinematic
> landscape composition suitable for 1920x1080 and 4K display scaling: a
> peaceful Japanese coastal town at blue hour, viewed from a slightly elevated
> path, distant sea and soft cloud bands, a few warm window lights, detailed
> but restrained hand-painted anime background aesthetic. Keep the entire
> leftmost 28% visually quiet and darker with sky/sea gradients so white desktop
> icon labels remain legible; put the town, lantern glow, and most detail on the
> middle-right. Maintain visual interest at center without a character covering
> app windows. Cool navy/indigo and teal balanced by small warm amber accents.
> No people, no recognizable copyrighted characters or franchise elements, no
> text, no logos, no UI, no border. Crisp, polished original illustration; avoid
> heavy grain and excessive tiny details.

16:10 extension prompt:

> Edit this original anime coastal-town desktop wallpaper into a true 16:10
> landscape composition intended for a 2560×1600 monitor. Preserve the exact
> scene, mood, and visual style. Extend the canvas vertically (more dark blue
> sky at top and more foreground path/sea at bottom) instead of cropping any
> existing left or right content; keep the left 28% quiet and dark for desktop
> icons. Maintain crisp painted detail, no people, no text, no logos, no UI.
> Output the highest practical resolution with a 16:10 aspect ratio.

The generation tool delivered 1586×992 pixels. PCManFM proportionally scales
and crops it to fill the active desktop canvas; the source image does not
increase the kernel's HDMI output mode.
