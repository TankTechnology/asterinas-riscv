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
[RISC-V QEMU kernel-test log](ktest-qemu-serial.log) shows **21/21**
`aster-bigtcp` tests passing, including the two new Echo tests; the RISC-V
kernel build passed separately.

The candidate Image has **not yet been booted on Megrez**. The QEMU kernel
tests prove packet processing but do not prove the physical Ethernet path or
host ping. The running board and its RockOS-default boot menu were left on
the existing Image for a later controlled boot-and-recovery check.
