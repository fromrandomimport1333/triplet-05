"""One-time setup of the secrets: TLS certificates, HMAC keys, broker accounts.

    python tools/setup.py                 # from brain/: creates what is missing in secrets/
    python tools/setup.py --host 192.168.1.20 --host brain-laptop   # extra names in the broker cert
    python tools/setup.py --force         # recreate everything (new keys: share them again)

Writes (all git-ignored, never committed):

    secrets/ca.crt, secrets/ca.key           triplet CA (give ca.crt to the Twin and Guardian teams)
    secrets/broker/server.crt, server.key    broker certificate (localhost, 127.0.0.1, mosquitto, this
                                             machine's name and LAN addresses, plus --host values)
    secrets/broker/passwd                    Mosquitto password file (PBKDF2-SHA512, "$7$" format)
    secrets/broker-password.txt              password of the `brain` account (config/security.yaml)
    secrets/twin-password.txt, guardian-password.txt, tester-password.txt   other accounts
    secrets/keys/twin-brain.key, guardian-brain.key                         HMAC keys (32 random bytes, hex)

Python only (uses `cryptography`, already a Brain dependency): no OpenSSL or mosquitto_passwd needed.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import ipaddress
import os
import secrets
import socket
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

BRAIN = Path(__file__).resolve().parents[1]
SECRETS = BRAIN / "secrets"
ACCOUNTS = {           # broker account -> password file (relative to secrets/)
    "brain": "broker-password.txt",
    "twin": "twin-password.txt",
    "guardian": "guardian-password.txt",
    "tester": "tester-password.txt",       # scenario runner (test control topics only)
}
HMAC_KEYS = ["twin-brain.key", "guardian-brain.key"]
PBKDF2_ITERATIONS = 101                     # Mosquitto's default for "$7$" hashes


def mosquitto_hash(password: str) -> str:
    """Password hash in Mosquitto's format: $7$<iterations>$<salt b64>$<hash b64> (PBKDF2-SHA512)."""
    salt = secrets.token_bytes(12)
    digest = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"$7${PBKDF2_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def write(path: Path, data: bytes | str, force: bool, private: bool = False) -> bool:
    if path.exists() and not force:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    if private:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass                                # Windows: ACLs, not modes
    print("wrote", path.relative_to(BRAIN) if path.is_relative_to(BRAIN) else path)
    return True


def local_names() -> tuple[set[str], set[str]]:
    dns = {"localhost", "mosquitto", "broker", socket.gethostname().lower()}
    ips = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return dns, ips


def pem_key(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption())


def make_ca(force: bool) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    crt_path, key_path = SECRETS / "ca.crt", SECRETS / "ca.key"
    if crt_path.exists() and key_path.exists() and not force:
        return (x509.load_pem_x509_certificate(crt_path.read_bytes()),
                serialization.load_pem_private_key(key_path.read_bytes(), None))
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "City Brain Convergence triplet CA")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            # key identifiers (RFC 5280): strict verifiers (Python 3.13+ default) refuse certificates without them
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    write(crt_path, cert.public_bytes(serialization.Encoding.PEM), True)
    write(key_path, pem_key(key), True, private=True)
    return cert, key


def make_server_cert(ca: x509.Certificate, ca_key, extra: list[str], force: bool) -> None:
    crt_path = SECRETS / "broker" / "server.crt"
    if crt_path.exists() and not force and not extra:
        return
    dns, ips = local_names()
    for h in extra:
        try:
            ips.add(str(ipaddress.ip_address(h)))
        except ValueError:
            dns.add(h.lower())
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    san = [x509.DNSName(d) for d in sorted(dns)] + [x509.IPAddress(ipaddress.ip_address(i)) for i in sorted(ips)]
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "city-brain-broker")]))
            .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=180))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, key_cert_sign=False,
                                         crl_sign=False, content_commitment=False, data_encipherment=False,
                                         key_agreement=True, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    write(crt_path, cert.public_bytes(serialization.Encoding.PEM), True)
    write(SECRETS / "broker" / "server.key", pem_key(key), True, private=True)
    write(SECRETS / "broker" / "ca.crt", ca.public_bytes(serialization.Encoding.PEM), True)
    print("  broker certificate names:", ", ".join(sorted(dns) + sorted(ips)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", action="append", default=[], help="extra broker name or IP (repeatable)")
    ap.add_argument("--force", action="store_true", help="recreate everything")
    ap.add_argument("--renew-certs", action="store_true", help="recreate the CA and broker certificate only (keys and passwords kept)")
    args = ap.parse_args()

    ca, ca_key = make_ca(args.force or args.renew_certs)
    make_server_cert(ca, ca_key, args.host, args.force or args.renew_certs)
    for name in HMAC_KEYS:
        write(SECRETS / "keys" / name, secrets.token_hex(32) + "\n", args.force, private=True)
    lines, changed = [], False
    for user, file in ACCOUNTS.items():
        changed |= write(SECRETS / file, secrets.token_urlsafe(24) + "\n", args.force, private=True)
        lines.append(f"{user}:{mosquitto_hash((SECRETS / file).read_text(encoding='utf-8').strip())}")
    passwd = SECRETS / "broker" / "passwd"
    if changed or args.force or not passwd.exists():
        write(passwd, "\n".join(lines) + "\n", True, private=True)
    print("secrets ready in", SECRETS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
