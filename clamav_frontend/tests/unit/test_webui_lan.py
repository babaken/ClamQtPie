"""WebUI の LAN からの接続（core/web_lan.py・api/web_control.py・api/auth.py・clamqtpie-ctl web lan。段階 12-5e2）のテスト。
実際のファイアウォール・ネットワークは変えない（LAN の範囲とファイアウォールの関数を差し替える）"""
from __future__ import annotations

import io
import ipaddress
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import API_PREFIX, create_app
from api.auth import ROLE_ADMIN, ApiTokens
from api.bridge import EventHub
from api.context import ApiContext
from api.server import create_listen_socket
from api.web_control import WebController, credential_changes, disable_totp, tls_changes, web_changes
from api.web_sessions import GLOBAL_MAX_FAILURES, LoginGuard
from core import local_ca, totp, web_lan
from core.config_model import ConfigModel, validate_and_fix
from core.config_service import ConfigService
from core.exceptions import ConfigValidationError
from core.local_ca import Names
from core.secret_store import FileSecretStore
from core.web_lan import LanScope
from tests.unit.test_webui_login import PASSWORD, _DirectInvoker

NAMES = Names(dns=("localhost", "testpc", "testpc.local"),
              ips=(ipaddress.IPv4Address("127.0.0.1"), ipaddress.IPv4Address("192.168.1.10")),
              networks=(ipaddress.IPv4Network("192.168.1.0/24"),))
SCOPE = LanScope(addresses=(ipaddress.IPv4Address("192.168.1.10"),),
                 networks=(ipaddress.IPv4Network("192.168.1.0/24"),), names=("testpc", "testpc.local"))
LAN_URL = "https://192.168.1.10:8766"


def _ready(api, **overrides):
    """LAN からの接続の条件をそろえた api の設定"""
    values = dict(web_tls="local_ca", web_totp_enabled=True, web_password_length=12)
    values.update(overrides)
    for name, value in values.items():
        setattr(api, name, value)
    return api


# -- 許可の条件 ----------------------------------------------------------------------------------------
def test_lan_needs_https_totp_and_a_long_password():
    api = ConfigModel().api
    with pytest.raises(ConfigValidationError) as error:
        web_changes(api, {"lan": True})
    assert error.value.detail == "lan"
    assert all(word in error.value.message for word in ("HTTPS", "二段階認証", "12 文字"))
    for missing in ({"web_tls": "off"}, {"web_totp_enabled": False}, {"web_password_length": 11}):
        with pytest.raises(ConfigValidationError):
            web_changes(_ready(ConfigModel().api, **missing), {"lan": True})
    assert web_changes(_ready(ConfigModel().api), {"lan": True})["web_lan"] is True
    assert web_changes(ConfigModel().api, {"lan": False})["web_lan"] is False  # やめるのはいつでもできる


def test_breaking_a_condition_turns_lan_off(tmp_path):
    assert tls_changes("off")["web_lan"] is False
    assert "web_lan" not in tls_changes("local_ca")
    assert credential_changes("admin", "short-pw-1")["web_lan"] is False  # 12 文字より短い
    assert "web_lan" not in credential_changes("admin", "long-enough-pass")
    service = ConfigService(tmp_path / "config.json", secret_store=FileSecretStore(tmp_path / "secrets"))
    service.load()
    model = service.current
    _ready(model.api, web_lan=True)
    service.save(model)
    disable_totp(service)
    assert service.current.api.web_lan is False


def test_config_with_lan_but_without_the_conditions_is_corrected():
    model = ConfigModel()
    model.api.web_lan = True  # 設定ファイルを直接書き換えても、条件がそろわなければ許可しない
    warnings = []
    validate_and_fix(model, warnings.append)
    assert model.api.web_lan is False and any("api.web_lan" in w for w in warnings)
    model = ConfigModel()
    model.api.web_password_hash = "x"  # パスワードの形は別の検査で見る
    _ready(model.api, web_lan=True)
    validate_and_fix(model, [].append)
    assert model.api.web_lan is (model.api.web_password_length >= 12 and model.api.web_tls == "local_ca")


def test_lan_sessions_last_at_most_fifteen_minutes():
    api = ConfigModel().api
    api.session_timeout_min = 30
    assert web_lan.session_timeout_min(api) == 30
    api.web_lan = True
    assert web_lan.session_timeout_min(api) == 15
    api.session_timeout_min = 10
    assert web_lan.session_timeout_min(api) == 10


# -- LAN の範囲 ----------------------------------------------------------------------------------------
def test_scope_allows_only_loopback_and_the_lan_subnets():
    assert SCOPE.allows_client("127.0.0.1") and SCOPE.allows_client("::1")
    assert SCOPE.allows_client("192.168.1.77")
    for client in ("192.168.2.5", "10.0.0.5", "203.0.113.9", "fe80::1", "testclient", ""):
        assert not SCOPE.allows_client(client)
    assert SCOPE.listen_hosts == ["127.0.0.1", "192.168.1.10"]
    assert {"localhost:8766", "127.0.0.1:8766", "testpc:8766", "testpc.local:8766", "192.168.1.10:8766"} \
        == SCOPE.allowed_hosts(8766)
    assert SCOPE.urls(8766) == [f"{LAN_URL}/web/login", "https://testpc.local:8766/web/login"]


def test_only_loopback_or_lan_addresses_can_be_listened_on():
    with pytest.raises(ValueError):
        create_listen_socket(0, "192.168.1.10")  # 内部 API・LAN を許可していない WebUI
    for host in ("0.0.0.0", "203.0.113.5", "169.254.1.1", "::"):
        with pytest.raises(ValueError):
            create_listen_socket(0, host, allow_lan=True)


# -- 待ち受け --------------------------------------------------------------------------------------------
class _Server:
    instances: list = []

    def __init__(self, app, port, name="", event_tag="", tls=None, hosts=None):
        self.app, self.port, self.tls, self.hosts = app, port, tls, hosts
        self.is_running = False
        _Server.instances.append(self)

    def start(self):
        self.is_running = True

    def stop(self):
        self.is_running = False


class _Lan:
    """core.web_lan の代わり（LAN の範囲とファイアウォール）"""
    FIREWALL_RULE = web_lan.FIREWALL_RULE

    def __init__(self):
        self.scope = SCOPE
        self.calls = []
        self.notes = []

    class LanScope:
        pass

    def open_firewall(self, port, program):
        self.calls.append(("open", port))
        return None

    def close_firewall(self):
        self.calls.append(("close",))

    def network_notes(self, port, scope):
        return list(self.notes)


@pytest.fixture
def env(tmp_path, monkeypatch):
    _Server.instances = []
    monkeypatch.setattr(local_ca.Names, "current", classmethod(lambda cls: NAMES))
    config_service = ConfigService(tmp_path / "config.json", secret_store=FileSecretStore(tmp_path / "secrets"))
    config_service.load()
    notices = []
    context = ApiContext(
        config_service=config_service, clamav_service=SimpleNamespace(), realtime_manager=SimpleNamespace(),
        scan_dispatcher=SimpleNamespace(is_running=False), update_service=SimpleNamespace(),
        update_dispatcher=SimpleNamespace(is_running=False), scheduler_service=SimpleNamespace(),
        quarantine_service=SimpleNamespace(), exclude_service=SimpleNamespace(),
        watch_target_service=SimpleNamespace(), log_reader_service=SimpleNamespace(),
        invoker=_DirectInvoker(), events=EventHub(),
        notification_service=SimpleNamespace(notify_system=lambda title, body: notices.append(title)),
    )
    lan = _Lan()
    lan.LanScope = SimpleNamespace(current=lambda: lan.scope)
    tokens = ApiTokens(tmp_path / "api")
    context.web = web = WebController(context, tokens, server_factory=_Server, tls_dir=tmp_path / "tls", lan=lan)
    internal = TestClient(create_app(tokens, context), base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    admin = {"Authorization": f"Bearer {tokens.token_for(ROLE_ADMIN)}"}
    assert internal.put(f"{API_PREFIX}/web/credentials", headers=admin,
                        json={"username": "admin", "password": PASSWORD}).status_code == 200
    assert internal.put(f"{API_PREFIX}/web", headers=admin, json={"enabled": True}).status_code == 200
    assert internal.put(f"{API_PREFIX}/web/tls", headers=admin, json={"mode": "local_ca"}).status_code == 200
    secret = totp.generate_secret()
    code = totp.code_at(secret, totp.current_step())
    assert internal.post(f"{API_PREFIX}/web/totp", headers=admin, json={"secret": secret, "code": code}).status_code == 200
    return SimpleNamespace(config=config_service, web=web, internal=internal, admin=admin, tokens=tokens,
                           context=context, lan=lan, secret=secret, notices=notices)


def _allow(env, allow=True):
    return env.internal.put(f"{API_PREFIX}/web", headers=env.admin, json={"lan": allow})


def _browser(env, client="192.168.1.20", base_url=LAN_URL):
    app = create_app(env.tokens, env.context, listener="web", web=env.web)
    return TestClient(app, base_url=base_url, client=(client, 50100))


def _login(browser, base_url=LAN_URL, password=PASSWORD, code="000000"):
    return browser.post(f"{API_PREFIX}/auth/session", headers={"Origin": base_url},
                        json={"username": "admin", "password": password, "code": code})


def test_allowing_lan_listens_on_the_lan_addresses_and_opens_the_firewall(env):
    status = _allow(env).json()
    server = _Server.instances[-1]
    assert server.is_running and server.tls is not None and server.hosts == ["127.0.0.1", "192.168.1.10"]
    assert status["lan"] is True and status["lan_listening"] is True
    assert f"{LAN_URL}/web/login" in status["urls"] and status["session_timeout_min"] == 15
    assert ("open", server.port) in env.lan.calls
    status = _allow(env, False).json()
    assert _Server.instances[-1].hosts is None and status["lan"] is False and ("close",) in env.lan.calls


def test_lan_is_refused_until_the_conditions_are_met(env):
    env.internal.delete(f"{API_PREFIX}/web/totp", headers=env.admin)
    response = _allow(env)
    assert response.status_code == 400 and "二段階認証" in response.json()["error"]["message"]
    assert env.config.current.api.web_lan is False


def test_turning_https_off_turns_lan_off_and_closes_the_firewall(env):
    _allow(env)
    status = env.internal.put(f"{API_PREFIX}/web/tls", headers=env.admin, json={"mode": "off"}).json()
    assert status["lan"] is False and env.config.current.api.web_lan is False
    assert _Server.instances[-1].hosts is None and env.lan.calls[-1] == ("close",)


def test_lan_setting_survives_turning_the_webui_off(env):
    _allow(env)
    env.internal.put(f"{API_PREFIX}/web", headers=env.admin, json={"enabled": False})
    assert env.config.current.api.web_lan is True and ("close",) in env.lan.calls
    status = env.internal.put(f"{API_PREFIX}/web", headers=env.admin, json={"enabled": True}).json()
    assert status["lan_listening"] is True and _Server.instances[-1].hosts == ["127.0.0.1", "192.168.1.10"]


def test_listener_restarts_when_the_lan_address_changes(env):
    import datetime as dt

    _allow(env)
    before = _Server.instances[-1]
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=2)
    assert env.web.check_lan(later) is False  # 変わっていない
    env.lan.scope = LanScope(addresses=(ipaddress.IPv4Address("192.168.1.11"),),
                             networks=SCOPE.networks, names=SCOPE.names)
    assert env.web.check_lan(later + dt.timedelta(minutes=2)) is True
    assert not before.is_running and _Server.instances[-1].hosts == ["127.0.0.1", "192.168.1.11"]


def test_expired_ca_turns_lan_off_and_tells_the_admin(env, monkeypatch):
    import datetime as dt

    _allow(env)

    def expired(self, *args, **kwargs):
        raise RuntimeError("ローカル CA の期限が切れています")

    monkeypatch.setattr(local_ca.LocalCa, "ensure", expired)
    assert env.web.check_tls(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)) is True
    assert env.config.current.api.web_lan is False and _Server.instances[-1].hosts is None
    assert "LAN からの接続を OFF にしました" in env.notices


def test_network_notes_are_shown(env):
    env.lan.notes = ["パブリックのネットワーク（Wi-Fi）からはつながりません"]
    status = _allow(env).json()
    assert status["lan_notes"] == env.lan.notes


# -- 接続元・Host の検査 ----------------------------------------------------------------------------------
def test_lan_clients_are_accepted_only_while_lan_is_allowed(env):
    browser = _browser(env)
    assert _login(browser).status_code == 403  # 許可していない
    _allow(env)
    browser = _browser(env)
    assert _login(browser).status_code == 401  # 届いた（確認コードが違う）
    outside = _browser(env, client="192.168.2.20")
    assert _login(outside).status_code == 403  # LAN のサブネットの外
    assert outside.get(f"{API_PREFIX}/web/ca-info").status_code == 403


def test_lan_host_names_are_accepted_and_others_are_not(env):
    _allow(env)
    for base in ("https://testpc.local:8766", "https://testpc:8766", LAN_URL):
        assert _login(_browser(env, base_url=base), base_url=base).status_code == 401
    evil = "https://evil.example:8766"
    assert _login(_browser(env, base_url=evil), base_url=evil).status_code == 400
    # Origin が Host と違えば断る
    browser = _browser(env)
    assert _login(browser, base_url="https://testpc.local:8766").status_code == 403


def test_lan_login_with_the_code_works(env):
    _allow(env)
    browser = _browser(env)
    code = totp.code_at(env.secret, totp.current_step() + 1)
    response = _login(browser, code=code)
    assert response.status_code == 200 and response.json()["lan"] is True
    assert response.json()["session_timeout_min"] == 15


# -- 全体のロック ----------------------------------------------------------------------------------------
def test_global_lock_counts_lan_failures_only():
    now = [0.0]
    guard = LoginGuard(clock=lambda: now[0])
    for _ in range(GLOBAL_MAX_FAILURES - 1):
        assert guard.lan_failure() is False
    assert guard.lan_failure() is True and guard.global_locked_seconds() > 0
    now[0] += 15 * 60 + 1
    assert guard.global_locked_seconds() == 0


def test_global_lock_failures_older_than_an_hour_are_forgotten():
    now = [0.0]
    guard = LoginGuard(clock=lambda: now[0])
    for _ in range(GLOBAL_MAX_FAILURES - 1):
        guard.lan_failure()
    now[0] += 60 * 60 + 1
    assert guard.lan_failure() is False


def test_global_lock_stops_lan_logins_but_not_this_pc(env):
    _allow(env)
    for i in range(GLOBAL_MAX_FAILURES):
        # 接続元を変えながら失敗する（接続元ごとのロックにはかからない）
        _login(_browser(env, client=f"192.168.1.{100 + i}"), password="wrong-password-x")
    locked = _login(_browser(env, client="192.168.1.250"))
    assert locked.status_code == 429 and locked.json()["error"]["code"] == "E8106"
    assert "LAN からの WebUI のログインを止めました" in env.notices
    local = _browser(env, client="127.0.0.1", base_url="https://127.0.0.1:8766")
    assert _login(local, base_url="https://127.0.0.1:8766").status_code == 401  # このPC のブラウザは止めない


# -- ファイアウォール・ネットワークの知らせ ---------------------------------------------------------------
class _Runner:
    def __init__(self, outputs=None):
        self.calls, self.outputs = [], dict(outputs or {})

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        name = cmd[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        code, out = self.outputs.get(name, (0, ""))
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows のファイアウォール")
def test_windows_firewall_rule_is_private_local_subnet_and_the_service_only():
    runner = _Runner()
    assert web_lan.open_firewall(8766, r"C:\Program Files\ClamQtPie\ClamQtPieService.exe", runner) is None
    delete, add = runner.calls
    assert delete[-1] == f"name={web_lan.FIREWALL_RULE}" and "delete" in delete
    assert {"dir=in", "action=allow", "protocol=TCP", "localport=8766", "remoteip=localsubnet", "profile=private",
            r"program=C:\Program Files\ClamQtPie\ClamQtPieService.exe"} <= set(add)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows のネットワークの種類")
def test_windows_public_network_is_reported():
    runner = _Runner({"powershell.exe": (0, '[{"InterfaceAlias":"Wi-Fi","NetworkCategory":0},'
                                            '{"InterfaceAlias":"Ethernet","NetworkCategory":1}]')})
    notes = web_lan.network_notes(8766, SCOPE, runner)
    assert len(notes) == 1 and "Wi-Fi" in notes[0] and "プライベート" in notes[0]
    private = _Runner({"powershell.exe": (0, '{"InterfaceAlias":"Ethernet","NetworkCategory":1}')})
    assert web_lan.network_notes(8766, SCOPE, private) == []


@pytest.mark.skipif(sys.platform == "win32", reason="Linux のファイアウォール")
def test_linux_firewall_is_explained_not_changed(monkeypatch):
    monkeypatch.setattr(web_lan, "system_tool", lambda name, system=None: f"/usr/sbin/{name}")
    active = _Runner({"ufw": (0, "Status: active\n")})
    notes = web_lan.network_notes(8766, SCOPE, active)
    assert "ufw" in notes[0] and "sudo ufw allow from 192.168.1.0/24 to any port 8766 proto tcp" in notes[0]
    assert all("allow" not in " ".join(c) for c in active.calls)  # 規則は変えない
    inactive = _Runner({"ufw": (0, "Status: inactive\n"), "firewall-cmd": (252, "not running\n")})
    assert "有効になっていない" in web_lan.network_notes(8766, SCOPE, inactive)[0]


def test_no_lan_address_is_reported():
    assert "見つかりません" in web_lan.network_notes(8766, LanScope(), _Runner())[0]


# -- clamqtpie-ctl web lan ---------------------------------------------------------------------------------
def test_ctl_web_lan_while_the_service_is_stopped(tmp_path, monkeypatch):
    import clamqtpie_ctl

    monkeypatch.setattr(clamqtpie_ctl, "service_running", lambda *a, **k: False)
    monkeypatch.setattr(clamqtpie_ctl, "_audit", lambda paths, message: None)
    data = tmp_path / "svc"

    def ctl(*args):
        out = io.StringIO()
        try:
            code = clamqtpie_ctl.main(["--data-dir", str(data), "web", *args], out=out)
        except SystemExit as exc:
            code = exc.code
        return code, out.getvalue()

    code, text = ctl("lan", "on")
    assert code != 0  # 条件がそろっていない
    service = clamqtpie_ctl._config_service(clamqtpie_ctl.ServicePaths.under(data))
    model = service.load()
    from core.web_password import hash_password

    model.api.web_password_hash = hash_password(PASSWORD)
    _ready(model.api, web_password_length=len(PASSWORD))
    service.save(model)
    code, text = ctl("lan", "on")
    assert code == 0 and "許可しました" in text and "LAN からの接続: 許可（試験的" in text
    assert service.load().api.web_lan is True
    code, text = ctl("tls", "off")
    assert "LAN からの接続の許可も OFF にしました" in text and service.load().api.web_lan is False


def test_ctl_cannot_set_web_lan_with_config_set():
    import clamqtpie_ctl

    assert "api.web_lan" in clamqtpie_ctl._WEB_COMMAND_FIELDS


def test_elevation_confirmation_tells_that_lan_is_on():
    from api_client.admin_ops import Operation, describe

    lines = describe(Operation("PUT", "/web", {"enabled": True, "lan": True, "off_after_min": 60}))
    assert "LAN からも接続できます" in lines[0]
    assert "LAN" not in describe(Operation("PUT", "/web", {"enabled": True, "off_after_min": 60}))[0]
    assert "許可します" in describe(Operation("PUT", "/web", {"lan": True}))[0]


# -- 実際の待ち受け ----------------------------------------------------------------------------------------
def _real_lan_address():
    try:
        found = LanScope.current().addresses
    except Exception:  # noqa: BLE001
        return None
    return str(found[0]) if found else None


# Windows では、LAN のアドレスで待ち受けると python.exe についてファイアウォールの確認の画面が出ることがあるため、Linux だけ
@pytest.mark.skipif(sys.platform == "win32" or _real_lan_address() is None,
                    reason="Windows のファイアウォールの確認の画面を出さない・LAN のアドレスが無い")
def test_server_listens_on_loopback_and_the_lan_address_with_one_port():
    from api.server import ApiServer

    lan_ip = _real_lan_address()

    async def app(scope, receive, send):  # 最小の ASGI アプリ
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    server = ApiServer(app, 0, name="WebUI", event_tag="web", hosts=["127.0.0.1", lan_ip])
    server.start()
    try:
        for host in ("127.0.0.1", lan_ip):
            with socket.create_connection((host, server.port), timeout=5):
                pass
    finally:
        server.stop()
def test_lan_bind_failure_keeps_the_webui_on_this_pc(env):
    """LAN のアドレスで待ち受けられなくても WebUI を OFF にせず、ループバックだけで続け、アドレスが変われば試し直す"""
    import datetime as dt

    original_start = _Server.start

    def start(self):
        if self.hosts:
            raise OSError("アドレスを使えません")
        original_start(self)

    _Server.start = start
    try:
        status = _allow(env).json()
    finally:
        _Server.start = original_start
    assert status["enabled"] is True and status["running"] is True and status["lan_listening"] is False
    assert _Server.instances[-1].hosts is None and "待ち受けられませんでした" in status["lan_notes"][0]
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=2)
    assert env.web.check_lan(later) is False  # 同じ範囲では試し直さない
    env.lan.scope = LanScope(addresses=(ipaddress.IPv4Address("192.168.1.12"),),
                             networks=SCOPE.networks, names=SCOPE.names)
    assert env.web.check_lan(later + dt.timedelta(minutes=2)) is True
    assert _Server.instances[-1].hosts == ["127.0.0.1", "192.168.1.12"] and env.web.status()["lan_listening"]


def test_password_without_a_recorded_length_is_explained():
    api = _ready(ConfigModel().api, web_password_length=0)
    api.web_password_hash = "x"
    assert "確かめられません" in "".join(web_lan.lan_problems(api))
