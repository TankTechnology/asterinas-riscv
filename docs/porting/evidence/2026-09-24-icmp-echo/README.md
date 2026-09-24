# IPv4 Echo Request diagnosis and kernel test

On the Megrez board's existing Asterinas boot
`80a2b08d-e2aa-4f6b-b3ae-9394770f8384` (Image SHA-256
`3e3707b7e59f46303a914395a8dd4a85de55d0feb063fa26bb510130ce02b40e`),
the host's direct `ping -I enp12s0 -c 2 -W 2 10.100.19.200` received
**0/2 replies**. The [raw ping output](old-image-icmp-ping.txt) records the
exit status. The board had `eth0=10.100.19.200/21`, and a nonce-marked
temporary Python HTTP service on that address returned HTTP 200 to the host
over direct TCP in 60 ms. The response matched the marker in the
[direct curl transcript](inbound-curl-direct.txt). After stopping the service,
the same port [refused connections](inbound-after-stop.txt). The board's
[network inventory](board-network-inventory.json.gz) and
[start](board-inbound-start.json.gz)/[stop](board-inbound-stop.json.gz)
serial transcripts retain UID-0 and boot-ID framing. Two independent serial
[reopens](current-image-handoff-1.log.gz)
([second](current-image-handoff-2.log.gz)) succeeded after cleanup.

In this source revision, `aster-bigtcp`'s IPv4 ingress dispatch handled TCP and
UDP but returned no response for ICMP. The candidate change replies only to a
checksum-valid ICMPv4 Echo Request addressed to a local unicast IPv4 address.
It reverses source and destination addresses and preserves the identifier,
sequence number, and data. The unit tests also reject an invalid checksum,
other ICMP message types, and broadcast destinations. The
[RISC-V QEMU kernel-test log](ktest-qemu-serial.log.gz) shows **21/21**
`aster-bigtcp` tests passing, including the two new Echo tests; the RISC-V
kernel build passed separately.

The release candidate from commit `8a3086f9418c43b2da1f2b62ff1ecb0260fed2bf`
was subsequently booted on Megrez. Its Image SHA-256 is
`754fb3b29663a5173b3115d9c357c8f67af0834f6df9c8f895aeb84ebf071f80`.
RockOS verified that SHA-256 after staging, and U-Boot verified the Image,
DTB, and initramfs CRC32 values before `booti`. The candidate reached the
debug root console, UID 0, systemd, graphical service, and ext2 root on
`/dev/mmcblk0p2`; the boot ID was
`82b3b011-49ab-456e-ade5-5faddc79432c`.

Two short direct-host [ping runs](new-image-icmp-ping-1.txt)
([repeat](new-image-icmp-ping-2.txt)) each received **2/2 replies** from
`10.100.19.200` with TTL 64. A temporary HTTP service on that address
returned a nonce-matching body to the host, and its port refused connections
after cleanup. The [board validation summary](board-validation.json) records
the exact artifact, boot, ICMP, TCP, and recovery observations. Each serial
probe used a fresh connection and UID-0/boot-ID frame. The Asterinas guest
then reached a fresh U-Boot prompt after the configured recovery deadline.
The serial capture begins with the new firmware epoch, so it does not
distinguish the userspace timer from the kernel fallback. The original
default menu booted RockOS, whose root shell
and new boot ID were verified through another fresh serial connection. The
test-only Image was removed after verifying its SHA-256 and confirming that
the menu did not reference it; `/boot` recovered 11,788,288 free bytes.
