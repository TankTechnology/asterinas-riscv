#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Generate the BROWSER-M12 TLS certificate matrix.

Produces, into <outdir> (default /tmp/tls-certs), a test CA plus four server
certificates that exercise the TLS certificate-validation decision points of a
client (NetSurf's libcurl fetcher / the standalone curl binary) when it connects
to ``https://10.0.2.2:<port>/`` — the guest's view of the QEMU host loopback via
slirp user networking:

  * ca.crt/ca.key            self-signed CA (CN=Asterinas TLS Test CA)
  * valid.crt/key            signed by the CA, SAN ``IP:10.0.2.2``, in-date
  * expired.crt/key          signed by the CA, SAN ``IP:10.0.2.2``, dates in the past
  * wronghost.crt/key        signed by the CA, SAN ``DNS:wrong.example.com`` (hostname
                             mismatch against 10.0.2.2), in-date
  * selfsigned.crt/key       self-signed leaf, SAN ``IP:10.0.2.2`` (untrusted CA)

The hostname/IP checks are what a *valid-but-mismatched* certificate trips on,
whereas the expired and self-signed cases trip on the chain/validity checks.
Together they pin down whether a TLS failure is a chain, validity, or name error.

Usage:
    python3 gen_tls_certs.py [outdir]
"""

from __future__ import annotations

import datetime
import ipaddress
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

HOST_IP = "10.0.2.2"  # slirp's "host" address as seen from the guest


def gen_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def subject(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def write_pem(obj, path: Path) -> None:
    path.write_bytes(obj.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ) if isinstance(obj, rsa.RSAPrivateKey) else obj.public_bytes(
        serialization.Encoding.PEM))


def make_cert(*, cn: str, san, key, issuer_cn: str, issuer_key,
              not_before: datetime.datetime, not_after: datetime.datetime,
              ca: bool) -> x509.Certificate:
    builder = (x509.CertificateBuilder()
               .subject_name(subject(cn))
               .issuer_name(subject(issuer_cn))
               .public_key(key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(not_before)
               .not_valid_after(not_after)
               .add_extension(x509.SubjectAlternativeName(san), critical=False))
    if ca:
        builder = builder.add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True)
    else:
        builder = builder.add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True)
    return builder.sign(issuer_key, hashes.SHA256())


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/tls-certs")
    out.mkdir(parents=True, exist_ok=True)

    now = datetime.datetime.now(datetime.timezone.utc)

    # Test CA (self-signed).
    ca_key = gen_key()
    ca_cert = make_cert(cn="Asterinas TLS Test CA", san=[], key=ca_key,
                        issuer_cn="Asterinas TLS Test CA", issuer_key=ca_key,
                        not_before=now - datetime.timedelta(days=1),
                        not_after=now + datetime.timedelta(days=3650), ca=True)
    write_pem(ca_key, out / "ca.key")
    write_pem(ca_cert, out / "ca.crt")

    ip_san = [x509.IPAddress(ipaddress.IPv4Address(HOST_IP))]
    wrong_san = [x509.DNSName("wrong.example.com")]

    cases = [
        # name, cn, san, not_before, not_after
        ("valid", HOST_IP, ip_san,
         now - datetime.timedelta(days=1), now + datetime.timedelta(days=30)),
        ("expired", HOST_IP, ip_san,
         now - datetime.timedelta(days=1000), now - datetime.timedelta(days=30)),
        ("wronghost", "wrong.example.com", wrong_san,
         now - datetime.timedelta(days=1), now + datetime.timedelta(days=30)),
    ]
    for name, cn, san, nb, na in cases:
        key = gen_key()
        cert = make_cert(cn=cn, san=san, key=key,
                         issuer_cn="Asterinas TLS Test CA", issuer_key=ca_key,
                         not_before=nb, not_after=na, ca=False)
        write_pem(key, out / f"{name}.key")
        write_pem(cert, out / f"{name}.crt")

    # Self-signed leaf (its own issuer).
    self_key = gen_key()
    self_cert = make_cert(cn=HOST_IP, san=ip_san, key=self_key,
                          issuer_cn=HOST_IP, issuer_key=self_key,
                          not_before=now - datetime.timedelta(days=1),
                          not_after=now + datetime.timedelta(days=30), ca=False)
    write_pem(self_key, out / "selfsigned.key")
    write_pem(self_cert, out / "selfsigned.crt")

    print(f"wrote CA + 4 server certs to {out}")
    for p in sorted(out.glob("*")):
        print(f"  {p.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
