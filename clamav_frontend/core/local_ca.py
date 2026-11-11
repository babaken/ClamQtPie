"""
WebUI の HTTPS のローカル認証局（CA）とサーバー証明書（サービス化・Web API設計書 3.5節。段階 12-5e1）

- CA: ECDSA P-256、有効期間 2 年。CN は「ClamQtPie Local CA (<ホスト名>)」。Name Constraints（critical）で、発行できる
  名前をこのPC の名前（net_scope.host_names）と、作った時点の LAN のサブネット（net_scope.lan_networks）とループバックに
  限る（CA の鍵が盗まれても、LAN のほかの機器になりすませないように）。CA の鍵はサーバー証明書の発行にだけ使う
- サーバー証明書: CA で署名。ECDSA P-256、有効期間 1 年。SAN はこのPC の名前・各 LAN のアドレス・127.0.0.1
- 置き場所は <データ>/tls/（ca.key・server.key は SYSTEM・root だけ。ca.crt・server.crt も同じフォルダ）
- CA は自動では作り直さない（作り直すと、すべての端末で登録のやり直しになる。12-5e の dig Q13）。期限の 30 日前から
  知らせ、サブネットが変わって Name Constraints から外れたら作り直し（reset_ca）を勧める（Q6）
- サーバー証明書は、名前・アドレスが変わったか、期限まで 30 日を切ったら作り直す（起動時と 1 日 1 回）
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from core import net_scope
from core.private_files import make_private_dir, write_private_file

CA_KEY = "ca.key"
CA_CERT = "ca.crt"
SERVER_KEY = "server.key"
SERVER_CERT = "server.crt"

CA_DAYS = 365 * 2
SERVER_DAYS = 365
#: 期限の何日前から作り直す（サーバー証明書）・知らせる（CA）か
RENEW_BEFORE_DAYS = 30


@dataclass(frozen=True)
class Names:
    """証明書に入れる名前とアドレス"""
    dns: tuple[str, ...]
    ips: tuple[ipaddress.IPv4Address, ...]
    networks: tuple[ipaddress.IPv4Network, ...]

    @classmethod
    def current(cls) -> "Names":
        addresses = net_scope.lan_addresses()
        return cls(dns=tuple(net_scope.host_names()),
                   ips=(ipaddress.IPv4Address("127.0.0.1"), *sorted({a.address for a in addresses})),
                   networks=tuple(net_scope.lan_networks(addresses)))


@dataclass
class TlsStatus:
    ca_fingerprint: str | None = None
    ca_not_after: dt.datetime | None = None
    server_not_after: dt.datetime | None = None
    server_names: list[str] = field(default_factory=list)
    #: 管理者に知らせること（CA の期限・サブネットの変化）
    notes: list[str] = field(default_factory=list)
    ca_expired: bool = False


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _x509():
    from cryptography import x509

    return x509


def _new_key():
    from cryptography.hazmat.primitives.asymmetric import ec

    return ec.generate_private_key(ec.SECP256R1())


def _key_pem(key) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _cert_pem(cert) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return cert.public_bytes(serialization.Encoding.PEM)


def fingerprint(cert) -> str:
    """SHA-256 のフィンガープリント（AB:CD:... の形。ブラウザ・OS の表示と同じ）"""
    from cryptography.hazmat.primitives import hashes

    return ":".join(f"{b:02X}" for b in cert.fingerprint(hashes.SHA256()))


def _hostname_for_cn(names: Names) -> str:
    return next((n for n in names.dns if n != "localhost" and "." not in n), "localhost")


class LocalCa:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    # -- 読み出し ---------------------------------------------------------------------------------
    def path(self, name: str) -> Path:
        return self.directory / name

    def _load_cert(self, name: str):
        try:
            return _x509().load_pem_x509_certificate(self.path(name).read_bytes())
        except (OSError, ValueError):
            return None

    def ca_cert(self):
        return self._load_cert(CA_CERT)

    def server_cert(self):
        return self._load_cert(SERVER_CERT)

    def has_ca(self) -> bool:
        return self.ca_cert() is not None and self.path(CA_KEY).is_file()

    def ca_pem(self) -> bytes | None:
        cert = self.ca_cert()
        return _cert_pem(cert) if cert is not None else None

    def ca_der(self) -> bytes | None:
        from cryptography.hazmat.primitives import serialization

        cert = self.ca_cert()
        return cert.public_bytes(serialization.Encoding.DER) if cert is not None else None

    # -- 作成 -------------------------------------------------------------------------------------
    def create_ca(self, names: Names, now: dt.datetime | None = None):
        """CA を作る（前の CA・サーバー証明書は消す）"""
        x509 = _x509()
        from cryptography.hazmat.primitives import hashes
        from cryptography.x509.oid import NameOID

        now = now or _now()
        key = _new_key()
        subject = x509.Name([
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ClamQtPie"),
            x509.NameAttribute(NameOID.COMMON_NAME, f"ClamQtPie Local CA ({_hostname_for_cn(names)})"),
        ])
        permitted = [x509.DNSName(n) for n in names.dns]
        permitted += [x509.IPAddress(n) for n in (net_scope.LOOPBACK_NETWORK, *names.networks)]
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                         crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256())
        )
        self.remove()
        make_private_dir(self.directory)
        write_private_file(self.path(CA_KEY), _key_pem(key))
        write_private_file(self.path(CA_CERT), _cert_pem(cert))
        return cert

    def issue_server(self, names: Names, now: dt.datetime | None = None):
        """CA でサーバー証明書を作る。名前・アドレスのうち、CA の Name Constraints の外のものは入れない"""
        x509 = _x509()
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

        now = now or _now()
        ca_cert = self.ca_cert()
        ca_key = serialization.load_pem_private_key(self.path(CA_KEY).read_bytes(), password=None)
        dns, ips = permitted_names(ca_cert, names)
        key = _new_key()
        sans = [x509.DNSName(n) for n in dns] + [x509.IPAddress(ip) for ip in ips]
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns[1] if len(dns) > 1 else dns[0])]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(min(now + dt.timedelta(days=SERVER_DAYS), ca_cert.not_valid_after_utc))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        write_private_file(self.path(SERVER_KEY), _key_pem(key))
        write_private_file(self.path(SERVER_CERT), _cert_pem(cert))
        return cert

    def remove_server(self) -> None:
        """サーバー証明書と鍵だけを消す（CA は残す。次の ensure で同じ CA から作り直す）"""
        for name in (SERVER_CERT, SERVER_KEY):
            try:
                self.path(name).unlink()
            except FileNotFoundError:
                pass

    def remove(self) -> None:
        """CA とサーバー証明書を消す（CA の作り直し・HTTPS を使わなくなったとき）"""
        if self.directory.is_symlink():
            raise OSError(f"フォルダがリンクになっています: {self.directory}")
        if self.directory.is_dir():
            shutil.rmtree(self.directory)

    # -- 更新 -------------------------------------------------------------------------------------
    def ensure(self, names: Names | None = None, now: dt.datetime | None = None) -> bool:
        """HTTPS で待ち受けられる状態にする。CA が無ければ作り、サーバー証明書が無い・古い・名前が変わったら作り直す。
        作った・作り直したら True（待ち受けを始め直す）。CA が期限切れなら作らずに RuntimeError"""
        names = names or Names.current()
        now = now or _now()
        created = False
        if not self.has_ca():
            self.create_ca(names, now)
            created = True
        ca_cert = self.ca_cert()
        if ca_cert.not_valid_after_utc <= now:
            raise RuntimeError("ローカル CA の期限が切れています（clamqtpie-ctl web tls reset-ca で作り直してください）")
        server = self.server_cert()
        if created or server is None or not self.path(SERVER_KEY).is_file() \
                or server_needs_renewal(server, ca_cert, names, now):
            self.issue_server(names, now)
            return True
        return False

    def status(self, names: Names | None = None, now: dt.datetime | None = None) -> TlsStatus:
        now = now or _now()
        status = TlsStatus()
        ca_cert = self.ca_cert()
        if ca_cert is None:
            return status
        status.ca_fingerprint = fingerprint(ca_cert)
        status.ca_not_after = ca_cert.not_valid_after_utc
        server = self.server_cert()
        if server is not None:
            status.server_not_after = server.not_valid_after_utc
            status.server_names = san_names(server)
        if status.ca_not_after <= now:
            status.ca_expired = True
            status.notes.append("ローカル CA の期限が切れています。clamqtpie-ctl web tls reset-ca で作り直し、"
                                "各端末に CA 証明書を登録し直してください")
        elif status.ca_not_after - now <= dt.timedelta(days=RENEW_BEFORE_DAYS):
            status.notes.append(f"ローカル CA の期限が近づいています（{status.ca_not_after:%Y-%m-%d}）。"
                                "clamqtpie-ctl web tls reset-ca で作り直し、各端末に CA 証明書を登録し直してください")
        outside = outside_networks(ca_cert, names or Names.current())
        if outside:
            status.notes.append("LAN のサブネットが CA を作ったときと変わりました（"
                                + "・".join(str(n) for n in outside) + "）。この範囲から HTTPS で開くには、"
                                "clamqtpie-ctl web tls reset-ca で CA を作り直し、各端末に登録し直してください")
        return status


# -- 証明書の中身の比べ方 ------------------------------------------------------------------------------
def _constraints(ca_cert):
    x509 = _x509()
    try:
        ext = ca_cert.extensions.get_extension_for_class(x509.NameConstraints).value
    except x509.ExtensionNotFound:
        return [], []
    dns = [n.value for n in ext.permitted_subtrees or [] if isinstance(n, x509.DNSName)]
    nets = [n.value for n in ext.permitted_subtrees or [] if isinstance(n, x509.IPAddress)]
    return dns, nets


def _dns_permitted(name: str, permitted: list[str]) -> bool:
    return any(name == p or name.endswith("." + p) for p in permitted)


def permitted_names(ca_cert, names: Names) -> tuple[list[str], list[ipaddress.IPv4Address]]:
    dns_ok, nets_ok = _constraints(ca_cert)
    dns = [n for n in names.dns if _dns_permitted(n, dns_ok)] or ["localhost"]
    ips = [ip for ip in names.ips if any(ip in net for net in nets_ok)]
    return dns, ips


def outside_networks(ca_cert, names: Names) -> list[ipaddress.IPv4Network]:
    """今の LAN のサブネットのうち、CA の Name Constraints に入っていないもの"""
    _, nets_ok = _constraints(ca_cert)
    return [n for n in names.networks if not any(n.subnet_of(ok) for ok in nets_ok if ok.version == 4)]


def san_names(cert) -> list[str]:
    x509 = _x509()
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return []
    return [*san.get_values_for_type(x509.DNSName), *(str(ip) for ip in san.get_values_for_type(x509.IPAddress))]


def server_needs_renewal(server, ca_cert, names: Names, now: dt.datetime) -> bool:
    if server.issuer != ca_cert.subject:
        return True
    if server.not_valid_after_utc - now <= dt.timedelta(days=RENEW_BEFORE_DAYS) \
            and server.not_valid_after_utc < ca_cert.not_valid_after_utc:
        return True
    dns, ips = permitted_names(ca_cert, names)
    return sorted(san_names(server)) != sorted([*dns, *(str(ip) for ip in ips)])
