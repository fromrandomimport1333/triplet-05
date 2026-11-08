"""Generate a DEV-ONLY triplet CA plus server/client certificates for mTLS.

python -m mocks.gen_dev_certs --out certs
Produces ca.pem, guardian.pem/.key (server, SAN guardian+localhost), brain/twin/admin/ui
client certs (CN <role>.triplet) and mosquitto.pem/.key. Never use these in production.
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "TSYP14 triplet (dev)"),
                      x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _write(out: Path, stem: str, key, cert) -> None:
    (out / f"{stem}.key").write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                        serialization.PrivateFormat.PKCS8,
                                                        serialization.NoEncryption()))
    (out / f"{stem}.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def generate(out: str | Path, clients=("brain", "twin", "admin", "ui", "guardian-client"),
             servers=(("guardian", ["guardian", "localhost"]), ("mosquitto", ["mosquitto", "guardian-mqtt", "localhost"]))) -> Path:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (x509.CertificateBuilder().subject_name(_name("triplet-dev-ca")).issuer_name(_name("triplet-dev-ca"))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=90))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    _write(out, "ca", ca_key, ca)

    def leaf(cn, sans, usage):
        k = ec.generate_private_key(ec.SECP256R1())
        b = (x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(ca.subject)
             .public_key(k.public_key()).serial_number(x509.random_serial_number())
             .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=30))
             .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
             .add_extension(x509.ExtendedKeyUsage([usage]), critical=False))
        if sans:
            alt = [x509.DNSName(s) for s in sans] + [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            b = b.add_extension(x509.SubjectAlternativeName(alt), critical=False)
        return k, b.sign(ca_key, hashes.SHA256())

    for stem, sans in servers:
        _write(out, stem, *leaf(stem, sans, ExtendedKeyUsageOID.SERVER_AUTH))
    for role in clients:
        _write(out, role, *leaf(f"{role}.triplet", None, ExtendedKeyUsageOID.CLIENT_AUTH))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="certs")
    a = ap.parse_args()
    print("dev certs written to", generate(a.out))
