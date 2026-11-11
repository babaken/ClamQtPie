"""WebUI の HTTPS（ローカル CA）のテスト（サービス化・Web API設計書 3.5節。段階 12-5e1）

- このPC が直接つながっている LAN の範囲（プライベートアドレスだけ、仮想ブリッジ・VPN・リンクローカルを除く）
- ローカル CA の Name Constraints（盗まれた CA の鍵で、LAN のほかの機器になりすませない）とサーバー証明書の作り直し
- HTTPS の待ち受け、Cookie の __Host-・Secure、CA 証明書の配布、clamqtpie-ctl web tls
"""
from __future__ import annotations

import datetime as dt
import io
import ipaddress
import socket
import ssl
import sys
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509 import verification
from fastapi.testclient import TestClient

from api.app import API_PREFIX, create_app
from api.auth import ROLE_ADMIN, SECURE_SESSION_COOKIE, SESSION_COOKIE, ApiTokens
from api.bridge import EventHub
from api.context import ApiContext
from api.web_control import WebController
from core import local_ca, net_scope, web_password
from core.config_model import ConfigModel, validate_and_fix
from core.config_service import ConfigService
from core.local_ca import CA_CERT, CA_KEY, SERVER_KEY, LocalCa, Names

from tests.unit.test_webui_login import PASSWORD, _DirectInvoker

HTTPS = "https://127.0.0.1:8766"
HTTP = "http://127.0.0.1:8766"
NAMES = Names(dns=("localhost", "testpc", "testpc.local"),
              ips=(ipaddress.IPv4Address("127.0.0.1"), ipaddress.IPv4Address("192.168.1.10")),
              networks=(ipaddress.IPv4Network("192.168.1.0/24"),))
NOW = dt.datetime(2026, 11, 5, tzinfo=dt.timezone.utc)


# -- LAN の範囲 ----------------------------------------------------------------------------------------
def test_lan_addresses_keep_only_private_physical_networks():
    interfaces = {
        "eth0": [("192.168.1.10", "255.255.255.0")],
        "wlan0": [("10.1.2.3", "255.255.0.0"), ("169.254.3.4", "255.255.0.0")],  # リンクローカルは除く
        "docker0": [("172.17.0.1", "255.255.0.0")],
        "br-1234": [("172.18.0.1", "255.255.0.0")],
        "vEthernet (WSL (Hyper-V firewall))": [("172.20.0.1", "255.255.240.0")],
        "wg0": [("10.8.0.2", "255.255.255.0")],  # VPN（遠くのネットワーク）
        "pub0": [("203.0.113.5", "255.255.255.0")],  # グローバルアドレス
        "lo": [("127.0.0.1", "255.0.0.0")],
        "ppp-point": [("192.168.9.9", "255.255.255.255")],
        "host32": [("192.168.7.7", "255.255.255.255")],  # /32 はサブネットではない
        "イーサネット 3": [("192.168.56.1", "255.255.255.0")],  # Windows の表示名（説明で見分ける）
    }
    descriptions = {"イーサネット 3": "VirtualBox Host-Only Ethernet Adapter"}
    found = net_scope.lan_addresses(interfaces, descriptions)
    assert [(a.interface, str(a.network)) for a in found] == [("eth0", "192.168.1.0/24"), ("wlan0", "10.1.0.0/16")]
    assert [str(n) for n in net_scope.lan_networks(found)] == ["10.1.0.0/16", "192.168.1.0/24"]


def test_lan_networks_drop_overlaps():
    found = net_scope.lan_addresses({"a": [("192.168.1.10", "255.255.255.0")], "b": [("192.168.1.20", "255.255.0.0")]})
    assert [str(n) for n in net_scope.lan_networks(found)] == ["192.168.0.0/16"]


def test_host_names_are_safe_dns_names():
    assert net_scope.host_names("TestPC", "testpc.example.lan") == [
        "localhost", "testpc", "testpc.local", "testpc.lan", "testpc.home.arpa", "testpc.example.lan"]
    # DNS の名前に使えない文字（Windows の PC の名前の _ など）は入れない
    assert net_scope.host_names("my_pc", "my_pc") == ["localhost"]


# -- ローカル CA -----------------------------------------------------------------------------------------
def _verify(ca: LocalCa, cert, name: str, now=NOW) -> None:
    subject = x509.IPAddress(ipaddress.ip_address(name)) if name[0].isdigit() else x509.DNSName(name)
    store = verification.Store([ca.ca_cert()])
    verifier = verification.PolicyBuilder().store(store).time(now).build_server_verifier(subject)
    verifier.verify(cert, [])


def _ca(tmp_path) -> LocalCa:
    ca = LocalCa(tmp_path / "tls")
    assert ca.ensure(NAMES, NOW) is True
    return ca


def test_ca_and_server_certificate(tmp_path):
    ca = _ca(tmp_path)
    cert = ca.ca_cert()
    assert isinstance(cert.public_key(), ec.EllipticCurvePublicKey) and cert.public_key().curve.name == "secp256r1"
    assert (cert.not_valid_after_utc - NOW).days in (729, 730)
    constraints = cert.extensions.get_extension_for_class(x509.NameConstraints)
    assert constraints.critical
    assert "ClamQtPie Local CA (testpc)" in cert.subject.rfc4514_string()
    for name in ("localhost", "testpc", "testpc.local", "127.0.0.1", "192.168.1.10"):
        _verify(ca, ca.server_cert(), name)
    server = ca.server_cert()
    assert (server.not_valid_after_utc - NOW).days in (364, 365)
    assert ca.ensure(NAMES, NOW) is False  # 変わっていなければ作り直さない


def test_stolen_ca_key_cannot_impersonate_other_devices(tmp_path):
    """CA の鍵が盗まれても、Name Constraints の外（ルーター・別のサブネット・ほかのドメイン）の証明書は信頼されない"""
    ca = _ca(tmp_path)
    ca_key = serialization.load_pem_private_key(ca.path(CA_KEY).read_bytes(), password=None)
    for name, san in [("10.0.0.1", x509.IPAddress(ipaddress.ip_address("10.0.0.1"))),
                      ("router.local", x509.DNSName("router.local")), ("example.com", x509.DNSName("example.com"))]:
        key = ec.generate_private_key(ec.SECP256R1())
        rogue = (x509.CertificateBuilder()
                 .subject_name(x509.Name([])).issuer_name(ca.ca_cert().subject).public_key(key.public_key())
                 .serial_number(x509.random_serial_number())
                 .not_valid_before(NOW - dt.timedelta(days=1)).not_valid_after(NOW + dt.timedelta(days=30))
                 .add_extension(x509.SubjectAlternativeName([san]), critical=False)
                 .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                 .sign(ca_key, hashes.SHA256()))
        with pytest.raises(verification.VerificationError):
            _verify(ca, rogue, name)


def test_server_certificate_is_renewed_when_names_change_or_expiry_nears(tmp_path):
    ca = _ca(tmp_path)
    moved = Names(dns=NAMES.dns, ips=(ipaddress.IPv4Address("127.0.0.1"), ipaddress.IPv4Address("192.168.1.77")),
                  networks=NAMES.networks)
    assert ca.ensure(moved, NOW) is True
    _verify(ca, ca.server_cert(), "192.168.1.77")
    later = NOW + dt.timedelta(days=340)  # 期限まで 30 日を切った
    assert ca.ensure(moved, later) is True
    assert ca.server_cert().not_valid_after_utc > later + dt.timedelta(days=300)


def test_ca_is_not_recreated_but_changes_are_reported(tmp_path):
    ca = _ca(tmp_path)
    fingerprint = ca.status(NAMES, NOW).ca_fingerprint
    other = Names(dns=NAMES.dns, ips=(ipaddress.IPv4Address("127.0.0.1"), ipaddress.IPv4Address("10.0.0.5")),
                  networks=(ipaddress.IPv4Network("10.0.0.0/24"),))
    assert ca.ensure(other, NOW) is True  # サーバー証明書は作り直すが、CA の外のアドレスは入れない
    status = ca.status(other, NOW)
    assert status.ca_fingerprint == fingerprint and "10.0.0.5" not in status.server_names
    assert any("サブネットが CA を作ったときと変わりました" in n and "reset-ca" in n for n in status.notes)
    near = ca.ca_cert().not_valid_after_utc - dt.timedelta(days=10)
    assert any("期限が近づいています" in n for n in ca.status(NAMES, near).notes)
    expired = ca.ca_cert().not_valid_after_utc + dt.timedelta(days=1)
    assert ca.status(NAMES, expired).ca_expired
    with pytest.raises(RuntimeError):  # 期限の切れた CA は自動では作り直さない
        ca.ensure(NAMES, expired)
    assert ca.status(NAMES, NOW).ca_fingerprint == fingerprint


@pytest.mark.skipif(sys.platform == "win32", reason="Linux の権限")
def test_keys_are_readable_by_root_only(tmp_path):
    ca = _ca(tmp_path)
    for name in (CA_KEY, SERVER_KEY):
        assert ca.path(name).stat().st_mode & 0o077 == 0


def test_invalid_tls_mode_falls_back_to_off():
    model = ConfigModel()
    model.api.web_tls = "custom"  # 利用者の証明書は v0.10.0 では受け付けない
    warnings = []
    validate_and_fix(model, warnings.append)
    assert model.api.web_tls == "off" and any("api.web_tls" in w for w in warnings)


# -- WebUI の待ち受け ------------------------------------------------------------------------------------
class _Server:
    instances: list = []

    def __init__(self, app, port, name="", event_tag="", tls=None):
        self.app, self.port, self.tls = app, port, tls
        self.is_running = False
        _Server.instances.append(self)

    def start(self):
        self.is_running = True

    def stop(self):
        self.is_running = False


@pytest.fixture
def env(tmp_path, monkeypatch):
    _Server.instances = []
    monkeypatch.setattr(local_ca.Names, "current", classmethod(lambda cls: NAMES))
    config_service = ConfigService(tmp_path / "config.json")
    config_service.load()
    context = ApiContext(
        config_service=config_service, clamav_service=SimpleNamespace(), realtime_manager=SimpleNamespace(),
        scan_dispatcher=SimpleNamespace(is_running=False), update_service=SimpleNamespace(),
        update_dispatcher=SimpleNamespace(is_running=False), scheduler_service=SimpleNamespace(),
        quarantine_service=SimpleNamespace(), exclude_service=SimpleNamespace(),
        watch_target_service=SimpleNamespace(), log_reader_service=SimpleNamespace(),
        invoker=_DirectInvoker(), events=EventHub(),
        notification_service=SimpleNamespace(notify_system=lambda title, body: None),
    )
    tokens = ApiTokens(tmp_path / "api")
    context.web = web = WebController(context, tokens, server_factory=_Server, tls_dir=tmp_path / "tls")
    internal = TestClient(create_app(tokens, context), base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    admin = {"Authorization": f"Bearer {tokens.token_for(ROLE_ADMIN)}"}
    response = internal.put(f"{API_PREFIX}/web/credentials", headers=admin,
                            json={"username": "admin", "password": PASSWORD})
    assert response.status_code == 200
    assert internal.put(f"{API_PREFIX}/web", headers=admin, json={"enabled": True}).status_code == 200
    return SimpleNamespace(config=config_service, web=web, internal=internal, admin=admin, tokens=tokens,
                           context=context, tmp_path=tmp_path)


def _browser(env, base_url):
    app = create_app(env.tokens, env.context, listener="web", web=env.web)
    return TestClient(app, base_url=base_url, client=("127.0.0.1", 50001))


def test_turning_https_on_restarts_the_listener_with_a_certificate(env):
    assert _Server.instances[-1].tls is None
    status = env.internal.put(f"{API_PREFIX}/web/tls", headers=env.admin, json={"mode": "local_ca"}).json()
    server = _Server.instances[-1]
    assert server.is_running and server.tls is not None and server.tls[0].endswith("server.crt")
    assert status["tls"] == "local_ca" and status["urls"][0].startswith("https://")
    assert status["tls_info"]["ca_fingerprint"].count(":") == 31
    assert env.config.current.api.web_tls == "local_ca"
    off = env.internal.put(f"{API_PREFIX}/web/tls", headers=env.admin, json={"mode": "off"}).json()
    assert off["tls"] == "off" and _Server.instances[-1].tls is None and off["tls_info"] is None


def test_certificate_failure_turns_webui_off_without_stopping_the_service(env, monkeypatch):
    def broken(self, *args, **kwargs):
        raise TypeError("壊れた証明書のライブラリ")

    monkeypatch.setattr(LocalCa, "ensure", broken)
    status = env.web.set_tls("local_ca")  # 例外を外に出さない
    assert status["enabled"] is False and not env.web.is_running


def test_tls_mode_is_admin_only_and_checked(env):
    assert env.internal.put(f"{API_PREFIX}/web/tls", headers=env.admin, json={"mode": "custom"}).status_code == 400
    browser = _browser(env, HTTP)
    assert browser.put(f"{API_PREFIX}/web/tls", json={"mode": "local_ca"},
                       headers={"Origin": HTTP}).status_code in (401, 403)


def test_daily_check_renews_the_certificate_and_restarts(env, monkeypatch):
    env.web.set_tls("local_ca")
    count = len(_Server.instances)
    assert env.web.check_tls() is False  # 1 日たっていない
    moved = Names(dns=NAMES.dns, ips=(*NAMES.ips, ipaddress.IPv4Address("192.168.1.99")), networks=NAMES.networks)
    monkeypatch.setattr(local_ca.Names, "current", classmethod(lambda cls: moved))
    tomorrow = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1, minutes=1)
    assert env.web.check_tls(tomorrow) is True
    assert len(_Server.instances) == count + 1 and _Server.instances[-1].is_running
    assert "192.168.1.99" in env.web.tls_info()["server_names"]


def test_reset_ca_makes_a_new_ca_and_reports_the_old_one(env):
    env.web.set_tls("local_ca")
    old = env.web.tls_info()["ca_fingerprint"]
    result = env.internal.post(f"{API_PREFIX}/web/tls/reset-ca", headers=env.admin).json()
    assert result["previous_ca_fingerprint"] == old
    assert result["tls_info"]["ca_fingerprint"] != old


def test_https_session_cookie_is_host_prefixed_and_secure(env):
    env.web.set_tls("local_ca")
    browser = _browser(env, HTTPS)
    response = browser.post(f"{API_PREFIX}/auth/session", headers={"Origin": HTTPS},
                            json={"username": "admin", "password": PASSWORD})
    assert response.status_code == 200, response.text
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SECURE_SESSION_COOKIE}=") and "Secure" in cookie and "Path=/" in cookie
    assert "Domain" not in cookie and not cookie.startswith(f"{SESSION_COOKIE}=")
    assert browser.get(f"{API_PREFIX}/auth/session").status_code == 200


def test_https_webui_refuses_requests_from_http_pages(env):
    env.web.set_tls("local_ca")
    browser = _browser(env, HTTPS)
    response = browser.post(f"{API_PREFIX}/auth/session", headers={"Origin": HTTP},
                            json={"username": "admin", "password": PASSWORD})
    assert response.status_code == 403


def test_ca_certificate_is_offered_over_https_only(env):
    env.web.set_tls("local_ca")
    https = _browser(env, HTTPS)
    response = https.get("/web/ca.crt")
    assert response.status_code == 200 and response.headers["content-type"] == "application/x-x509-ca-cert"
    cert = x509.load_der_x509_certificate(response.content)
    assert local_ca.fingerprint(cert) == https.get("/web/ca-info").json()["fingerprint"]
    assert b"PRIVATE KEY" not in response.content
    assert _browser(env, HTTP).get("/web/ca.crt").status_code == 404
    env.web.set_tls("off")
    assert _browser(env, HTTP).get("/web/ca-info").status_code == 404


# -- 実際の HTTPS の待ち受け ------------------------------------------------------------------------------
def test_real_https_listener_is_trusted_with_the_local_ca(tmp_path):
    from api.server import ApiServer

    ca = _ca(tmp_path)
    tokens = ApiTokens(tmp_path / "api")
    server = ApiServer(create_app(tokens), 0, name="WebUI", event_tag="web",
                       tls=(str(ca.path(local_ca.SERVER_CERT)), str(ca.path(local_ca.SERVER_KEY))))
    server.start()
    try:
        context = ssl.create_default_context(cafile=str(ca.path(CA_CERT)))
        with socket.create_connection(("127.0.0.1", server.port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
                assert tls.version() in ("TLSv1.2", "TLSv1.3")
        plain = ssl.create_default_context()  # CA を登録していなければ、つながらない
        with socket.create_connection(("127.0.0.1", server.port), timeout=5) as raw:
            with pytest.raises(ssl.SSLCertVerificationError):
                plain.wrap_socket(raw, server_hostname="127.0.0.1")
    finally:
        server.stop()


# -- clamqtpie-ctl web tls -------------------------------------------------------------------------------
def test_ctl_web_tls_offline(tmp_path, monkeypatch):
    import clamqtpie_ctl

    monkeypatch.setattr(clamqtpie_ctl, "service_running", lambda *a, **k: False)
    monkeypatch.setattr(clamqtpie_ctl, "_audit", lambda paths, message: None)
    data = tmp_path / "svc"

    def ctl(*args, ask=lambda prompt: "y"):
        out = io.StringIO()
        code = clamqtpie_ctl.main(["--data-dir", str(data), "web", "tls", *args], out=out, ask=ask)
        return code, out.getvalue()

    code, text = ctl("on")
    assert code == 0 and "HTTPS を有効にしました" in text and "まだ作っていません" in text
    assert "URL:" not in text  # WebUI が OFF のときは、つながらない URL を出さない
    assert clamqtpie_ctl._config_service(clamqtpie_ctl.ServicePaths.under(data)).load().api.web_tls == "local_ca"
    code, text = ctl("export-ca")
    assert code != 0  # サービスが作るまでは CA が無い
    # サービスが CA を作った後
    LocalCa(clamqtpie_ctl.ServicePaths.under(data).tls_dir).ensure(NAMES)
    code, text = ctl("status")
    assert "フィンガープリント" in text and "サーバー証明書の名前" in text
    code, text = ctl("export-ca")
    assert text.startswith("-----BEGIN CERTIFICATE-----") and "PRIVATE" not in text
    target = tmp_path / "ca.crt"
    code, text = ctl("export-ca", str(target))
    assert target.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")
    code, text = ctl("reset-ca", ask=lambda prompt: "n")
    assert "やめました" in text and LocalCa(clamqtpie_ctl.ServicePaths.under(data).tls_dir).has_ca()
    code, text = ctl("reset-ca", "--yes")
    assert "前の CA 証明書を削除してください" in text
    assert not LocalCa(clamqtpie_ctl.ServicePaths.under(data).tls_dir).has_ca()


def test_ctl_web_tls_shows_the_new_url_when_webui_is_on(tmp_path, monkeypatch):
    """HTTPS を切り替えたら、WebUI が ON なら開き直す URL（scheme が変わったもの）を出す"""
    import clamqtpie_ctl

    monkeypatch.setattr(clamqtpie_ctl, "service_running", lambda *a, **k: False)
    monkeypatch.setattr(clamqtpie_ctl, "_audit", lambda paths, message: None)
    data = tmp_path / "svc"
    paths = clamqtpie_ctl.ServicePaths.under(data)
    service = clamqtpie_ctl._config_service(paths)
    config = service.load()
    config.api.web_enabled = True
    config.api.web_password_hash = web_password.hash_password(PASSWORD)
    service.save(config)

    def ctl(*args):
        out = io.StringIO()
        clamqtpie_ctl.main(["--data-dir", str(data), "web", "tls", *args], out=out, ask=lambda prompt: "y")
        return out.getvalue()

    port = config.api.web_port
    assert f"URL: https://127.0.0.1:{port}/web/login" in ctl("on")
    assert f"URL: http://127.0.0.1:{port}/web/login" in ctl("off")


def test_ctl_web_tls_uses_the_url_the_service_returns(tmp_path, monkeypatch):
    """サービスが動いているときは、サービスが返した状態の URL を出す"""
    import clamqtpie_ctl

    class _Api:
        def call(self, method, path, body=None):
            assert (method, path, body) == ("PUT", "/web/tls", {"mode": "local_ca"})
            return {"enabled": True, "urls": ["https://127.0.0.1:8766/web/login"]}

    monkeypatch.setattr(clamqtpie_ctl, "_web_api", lambda paths: _Api())
    out = io.StringIO()
    clamqtpie_ctl.main(["--data-dir", str(tmp_path / "svc"), "web", "tls", "on"], out=out)
    assert "URL: https://127.0.0.1:8766/web/login" in out.getvalue()
def test_renewing_the_server_certificate_keeps_the_ca(env):
    """サーバー証明書だけを作り直す（CA はそのまま。端末の CA の登録はやり直さなくてよい）"""
    env.internal.put(f"{API_PREFIX}/web/tls", headers=env.admin, json={"mode": "local_ca"})
    ca = env.web.ca
    before_ca, before_server = ca.status().ca_fingerprint, ca.server_cert().serial_number
    status = env.internal.post(f"{API_PREFIX}/web/tls/renew", headers=env.admin).json()
    assert status["tls_info"]["ca_fingerprint"] == before_ca
    assert ca.server_cert().serial_number != before_server and _Server.instances[-1].is_running
    browser = _browser(env, HTTPS)
    assert browser.post(f"{API_PREFIX}/web/tls/renew", headers={"Origin": HTTPS}).status_code in (401, 403)


def test_ctl_web_tls_renew_while_the_service_is_stopped(tmp_path, monkeypatch):
    import clamqtpie_ctl

    monkeypatch.setattr(clamqtpie_ctl, "service_running", lambda *a, **k: False)
    monkeypatch.setattr(clamqtpie_ctl, "_audit", lambda paths, message: None)
    data = tmp_path / "svc"
    ca = LocalCa(clamqtpie_ctl.ServicePaths.under(data).tls_dir)
    ca.ensure(NAMES)
    fingerprint = ca.status().ca_fingerprint
    out = io.StringIO()
    assert clamqtpie_ctl.main(["--data-dir", str(data), "web", "tls", "renew"], out=out) == 0
    assert ca.server_cert() is None and ca.has_ca() and ca.status().ca_fingerprint == fingerprint
    assert "作り直しました" in out.getvalue()
