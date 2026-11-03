"""WebUI の待ち受け・ログイン・セッション・再確認・設定のテスト（サービス化・Web API設計書 3.1節・3.3節・6.11節。段階 12-5a）"""
from __future__ import annotations

import asyncio
import concurrent.futures
import io
import json
import socket
import urllib.request
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from api.app import API_PREFIX, create_app
from api.auth import ROLE_ADMIN, ApiTokens, require_admin
from api.bridge import EventHub
from api.context import ApiContext
from api.web_control import WebController
from api.web_sessions import LOCK_SEC, MAX_FAILURES, LoginGuard, WebSessions
from core import duration_text, web_password
from core.config_model import ConfigModel, validate_and_fix
from core.config_service import ConfigService

PASSWORD = "correct-horse-1"
WEB = "http://127.0.0.1:8766"
INTERNAL = "http://127.0.0.1:8765"


class _DirectInvoker:
    def call(self, fn, *args):
        future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args))
        except BaseException as exc:  # noqa: BLE001
            future.set_exception(exc)
        return future

    async def run(self, fn, *args):
        return await asyncio.wrap_future(self.call(fn, *args))


class _FakeServer:
    """WebController の待ち受けの代わり（start・stop を記録する）"""

    instances: list = []

    def __init__(self, app, port, name="", event_tag=""):
        self.app, self.port, self.name, self.event_tag = app, port, name, event_tag
        self.is_running = False
        _FakeServer.instances.append(self)

    def start(self):
        if getattr(_FakeServer, "next_fails", False):
            raise OSError("ポートは使用中です")
        self.is_running = True

    def stop(self):
        self.is_running = False


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    _FakeServer.instances = []
    _FakeServer.next_fails = False
    config_service = ConfigService(tmp_path / "config.json")
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
    tokens = ApiTokens(tmp_path / "api")
    context.web = web = WebController(context, tokens, server_factory=_FakeServer)
    internal_app = create_app(tokens, context)
    web_app = create_app(tokens, context, listener="web", web=web)

    def admin_only(role: str = Depends(require_admin)):
        return {"role": role}

    web_app.add_api_route(f"{API_PREFIX}/_admin", admin_only, methods=["POST"])
    internal = TestClient(internal_app, base_url=INTERNAL, client=("127.0.0.1", 50000))
    browser = TestClient(web_app, base_url=WEB, client=("127.0.0.1", 50001))
    admin = {"Authorization": f"Bearer {tokens.token_for(ROLE_ADMIN)}"}
    return SimpleNamespace(config=config_service, web=web, internal=internal, browser=browser, admin=admin,
                           notices=notices, tokens=tokens, web_app=web_app, context=context)


def _set_login(env, username="admin", password=PASSWORD):
    response = env.internal.put(f"{API_PREFIX}/web/credentials", headers=env.admin,
                                json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response


def _login(env, password=PASSWORD, client=None):
    client = client or env.browser
    return client.post(f"{API_PREFIX}/auth/session", headers={"Origin": WEB},
                       json={"username": "admin", "password": password})


def _logged_in(env):
    _set_login(env)
    response = _login(env)
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


# -- パスワード ----------------------------------------------------------------------------------------
def test_password_hash_is_salted_and_verifies():
    first, second = web_password.hash_password(PASSWORD), web_password.hash_password(PASSWORD)
    assert first != second and first.startswith("scrypt$16384$8$1$")  # ソルトが毎回違う
    assert PASSWORD not in first
    assert web_password.verify_password(PASSWORD, first)
    assert not web_password.verify_password("wrong-password", first)
    assert not web_password.verify_password(PASSWORD, "")  # 未設定でも計算してから失敗にする
    assert not web_password.verify_password(PASSWORD, "scrypt$1048576$8$1$AAAA$AAAA")  # 重すぎる作り方は受け付けない


@pytest.mark.parametrize("password, lan, ok", [
    ("1234567", False, False), ("12345678", False, True), ("12345678901", True, False), ("123456789012", True, True),
    ("abc\ndefgh", False, False), ("x" * 1025, False, False),
])
def test_password_rules(password, lan, ok):
    assert (web_password.password_problem(password, lan=lan) is None) is ok


@pytest.mark.parametrize("name, ok", [("admin", True), ("a.b-c_1", True), ("", False), ("a b", False),
                                      ("x" * 33, False), ("管理者", False)])
def test_username_rules(name, ok):
    assert (web_password.username_problem(name) is None) is ok


def test_generated_password_avoids_confusing_characters():
    generated = web_password.generate_password()
    assert len(generated) == 20 and not set(generated) & set("0Oo1lI")


# -- 長さ・日時 ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("text, minutes", [("30m", 30), ("8H", 480), ("2d", 2880), ("1d12h", 2160), ("1w", 10080),
                                           ("1y", 525600)])
def test_durations_are_parsed(text, minutes):
    assert duration_text.parse_duration_minutes(text) == minutes


@pytest.mark.parametrize("text", ["", "0m", "8", "h", "367d", "1y2d", "-1h", "1.5h"])
def test_bad_durations_are_refused(text):
    with pytest.raises(ValueError):
        duration_text.parse_duration_minutes(text)


def test_off_at_must_be_in_the_future_and_within_a_year():
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    assert duration_text.check_off_at("2026-11-04T00:00:00+00:00", now) == "2026-11-04T00:00:00+00:00"
    for value in ("2026-11-02T00:00:00+00:00", "2027-12-01T00:00:00+00:00", "2026-11-04T00:00:00", "x"):
        with pytest.raises(ValueError):
            duration_text.check_off_at(value, now)


# -- 設定 ----------------------------------------------------------------------------------------------
def test_webui_cannot_stay_on_without_a_password():
    model = ConfigModel()
    model.api.web_enabled = True
    model.api.web_port = model.api.port
    model.api.web_password_hash = "broken"
    warnings = []
    validate_and_fix(model, warnings.append)
    assert model.api.web_enabled is False and model.api.web_password_hash == ""
    assert model.api.web_port != model.api.port and len(warnings) == 3


def test_api_settings_are_not_shown_or_changed_through_the_config_api(env):
    _set_login(env)
    data = env.internal.get(f"{API_PREFIX}/config", headers=env.admin).json()
    assert "api" not in data  # パスワードのハッシュを API で返さない
    status = env.internal.get(f"{API_PREFIX}/web", headers=env.admin).json()
    assert status["password_set"] is True and "web_password_hash" not in json.dumps(status)


# -- ON/OFF・ログインの設定（Bearer だけ） -----------------------------------------------------------------
def test_webui_cannot_be_turned_on_before_the_login_is_set(env):
    response = env.internal.put(f"{API_PREFIX}/web", headers=env.admin, json={"enabled": True})
    assert response.status_code == 400 and env.config.current.api.web_enabled is False


def test_webui_turns_on_and_off_with_a_timer(env):
    _set_login(env)
    response = env.internal.put(f"{API_PREFIX}/web", headers=env.admin, json={"enabled": True, "off_after_min": 60})
    assert response.status_code == 200 and response.json()["running"] is True
    assert _FakeServer.instances[-1].port == 8766 and _FakeServer.instances[-1].event_tag == "web"
    off_at = datetime.fromisoformat(env.config.current.api.web_off_at)
    assert timedelta(minutes=59) < off_at - datetime.now(timezone.utc) <= timedelta(minutes=60)

    assert env.web.check_timer(now=off_at - timedelta(seconds=1)) is False
    assert env.web.check_timer(now=off_at + timedelta(seconds=1)) is True
    assert env.config.current.api.web_enabled is False and env.config.current.api.web_off_at == ""
    assert not env.web.is_running and env.notices == ["WebUI を OFF にしました"]


def test_lan_is_refused_until_https_and_two_factor_exist(env):
    _set_login(env)
    response = env.internal.put(f"{API_PREFIX}/web", headers=env.admin, json={"enabled": True, "lan": True})
    assert response.status_code == 400 and env.config.current.api.web_enabled is False


def test_user_token_cannot_change_the_webui(env):
    user = {"Authorization": f"Bearer {env.tokens.token_for('user')}"}
    assert env.internal.put(f"{API_PREFIX}/web/credentials", headers=user,
                            json={"username": "admin", "password": PASSWORD}).status_code == 403
    assert env.internal.get(f"{API_PREFIX}/web", headers=user).status_code == 200


def test_failed_listen_turns_the_webui_off_and_notifies(env):
    _set_login(env)
    _FakeServer.next_fails = True
    response = env.internal.put(f"{API_PREFIX}/web", headers=env.admin, json={"enabled": True})
    assert response.status_code == 200 and response.json()["enabled"] is False
    assert env.notices == ["WebUI を開始できませんでした"]


# -- ログイン ----------------------------------------------------------------------------------------
def test_login_issues_a_session_cookie_and_a_csrf_token(env):
    _set_login(env)
    response = _login(env)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "session=" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    assert response.json()["csrf_token"] and "csrf" not in cookie  # CSRF トークンは Cookie にしない
    assert env.browser.get(f"{API_PREFIX}/web").status_code == 200


def test_wrong_user_and_wrong_password_look_the_same(env):
    _set_login(env)
    bad_password = _login(env, password="wrong-password-1")
    bad_user = env.browser.post(f"{API_PREFIX}/auth/session", headers={"Origin": WEB},
                                json={"username": "root", "password": PASSWORD})
    assert bad_password.status_code == bad_user.status_code == 401
    assert bad_password.json() == bad_user.json()


def test_repeated_failures_lock_the_client(env):
    _set_login(env)
    for _ in range(MAX_FAILURES):
        assert _login(env, password="wrong-password-1").status_code == 401
    assert _login(env).status_code == 429  # 正しいパスワードでも、ロック中は受け付けない
    other = TestClient(env.web_app, base_url=WEB, client=("::1", 50002))
    assert _login(env, client=other).status_code == 200  # 接続元ごと


def test_login_replaces_the_old_session(env):
    _logged_in(env)
    old = env.browser.cookies.get("session")
    _login(env)
    new = env.browser.cookies.get("session")
    assert new and new != old
    stale = TestClient(env.web_app, base_url=WEB, client=("127.0.0.1", 50003), cookies={"session": old})
    assert stale.get(f"{API_PREFIX}/web").status_code == 401


def test_logout_ends_the_session(env):
    csrf = _logged_in(env)
    response = env.browser.delete(f"{API_PREFIX}/auth/session", headers={"Origin": WEB, "X-CSRF-Token": csrf})
    assert response.status_code == 204
    assert env.browser.get(f"{API_PREFIX}/web").status_code == 401


def test_changing_the_login_ends_all_sessions(env):
    _logged_in(env)
    _set_login(env, password="another-pass-2")
    assert env.browser.get(f"{API_PREFIX}/web").status_code == 401


def test_turning_the_webui_off_ends_all_sessions(env):
    _logged_in(env)
    env.internal.put(f"{API_PREFIX}/web", headers=env.admin, json={"enabled": True})
    env.internal.put(f"{API_PREFIX}/web", headers=env.admin, json={"enabled": False})
    assert env.browser.get(f"{API_PREFIX}/web").status_code == 401


# -- 待ち受けごとの認証 -------------------------------------------------------------------------------------
def test_each_listener_accepts_only_its_own_credentials(env):
    _logged_in(env)
    cookie = {"session": env.browser.cookies.get("session")}
    internal_with_cookie = TestClient(env.internal.app, base_url=INTERNAL, client=("127.0.0.1", 50004), cookies=cookie)
    assert internal_with_cookie.get(f"{API_PREFIX}/web").status_code == 401  # 内部 API は Cookie を受け付けない
    bearer_on_web = TestClient(env.web_app, base_url=WEB, client=("127.0.0.1", 50005))
    assert bearer_on_web.get(f"{API_PREFIX}/web", headers=env.admin).status_code == 401  # WebUI は Bearer を受け付けない


def test_login_route_exists_only_on_the_webui(env):
    _set_login(env)
    response = env.internal.post(f"{API_PREFIX}/auth/session", json={"username": "admin", "password": PASSWORD})
    assert response.status_code == 404


def test_webui_session_cannot_change_the_webui_settings(env):
    csrf = _logged_in(env)
    headers = {"Origin": WEB, "X-CSRF-Token": csrf}
    env.browser.post(f"{API_PREFIX}/auth/reauth", headers=headers, json={"password": PASSWORD})
    response = env.browser.put(f"{API_PREFIX}/web", headers=headers, json={"enabled": False})
    assert response.status_code == 403 and response.json()["error"]["code"] == "E8102"
    response = env.browser.put(f"{API_PREFIX}/web/credentials", headers=headers,
                               json={"username": "admin", "password": "new-password-3"})
    assert response.status_code == 403


def test_webui_accepts_only_loopback_clients_for_now(env):
    _set_login(env)
    lan = TestClient(env.web_app, base_url=WEB, client=("192.168.1.20", 50006))
    assert _login(env, client=lan).status_code == 403


# -- CSRF・Origin ------------------------------------------------------------------------------------------
def test_state_changes_need_the_csrf_token_of_the_session(env):
    csrf = _logged_in(env)
    no_token = env.browser.post(f"{API_PREFIX}/auth/reauth", headers={"Origin": WEB}, json={"password": PASSWORD})
    assert no_token.status_code == 403 and no_token.json()["error"]["code"] == "E8103"
    wrong = env.browser.post(f"{API_PREFIX}/auth/reauth", headers={"Origin": WEB, "X-CSRF-Token": csrf + "x"},
                             json={"password": PASSWORD})
    assert wrong.status_code == 403


def test_state_changes_need_the_webui_origin(env):
    _set_login(env)
    for headers in ({}, {"Origin": "http://evil.example"}, {"Origin": "http://127.0.0.1:8765"}):
        response = env.browser.post(f"{API_PREFIX}/auth/session", headers=headers,
                                    json={"username": "admin", "password": PASSWORD})
        assert response.status_code == 403
    referer = env.browser.post(f"{API_PREFIX}/auth/session", headers={"Referer": f"{WEB}/web/login"},
                               json={"username": "admin", "password": PASSWORD})
    assert referer.status_code == 200


# -- admin の操作の前の再確認 -------------------------------------------------------------------------------
def test_admin_operations_need_a_recent_reauth(env):
    csrf = _logged_in(env)
    headers = {"Origin": WEB, "X-CSRF-Token": csrf}
    response = env.browser.post(f"{API_PREFIX}/_admin", headers=headers)
    assert response.status_code == 403 and response.json()["error"]["code"] == "E8108"
    bad = env.browser.post(f"{API_PREFIX}/auth/reauth", headers=headers, json={"password": "wrong-password-1"})
    assert bad.status_code == 403 and bad.json()["error"]["code"] == "E8108"
    assert env.browser.post(f"{API_PREFIX}/auth/reauth", headers=headers, json={"password": PASSWORD}).json() == \
        {"reauth_sec": 300}
    assert env.browser.post(f"{API_PREFIX}/_admin", headers=headers).json() == {"role": "admin"}


def test_reauth_failures_count_and_lock_ends_the_session(env):
    csrf = _logged_in(env)
    headers = {"Origin": WEB, "X-CSRF-Token": csrf}
    for _ in range(MAX_FAILURES):
        env.browser.post(f"{API_PREFIX}/auth/reauth", headers=headers, json={"password": "wrong-password-1"})
    assert env.browser.get(f"{API_PREFIX}/web").status_code == 401  # ロックしたらセッションも破棄


# -- セッションの期限・ロックの時間 ---------------------------------------------------------------------------
def test_sessions_expire_after_the_idle_timeout():
    clock = _Clock()
    sessions = WebSessions(lambda: 30, clock=clock)
    session = sessions.create("127.0.0.1")
    clock.now += 29 * 60
    assert sessions.get(session.id) is session  # 操作すると延びる
    clock.now += 29 * 60
    assert sessions.get(session.id) is session
    clock.now += 31 * 60
    assert sessions.get(session.id) is None


def test_reauth_lasts_five_minutes():
    clock = _Clock()
    sessions = WebSessions(lambda: 30, clock=clock)
    session = sessions.create("127.0.0.1")
    sessions.mark_reauthenticated(session)
    clock.now += 299
    assert sessions.is_reauthenticated(session)
    clock.now += 2
    assert not sessions.is_reauthenticated(session)


def test_lock_is_released_after_five_minutes():
    clock = _Clock()
    guard = LoginGuard(clock=clock)
    for _ in range(MAX_FAILURES - 1):
        assert guard.failure("a") is False
    assert guard.failure("a") is True and guard.locked_seconds("a") > 0
    clock.now += LOCK_SEC + 1
    assert guard.locked_seconds("a") == 0


# -- イベントの購読 -----------------------------------------------------------------------------------------
def test_stopping_one_listener_ends_only_its_event_streams():
    hub = EventHub()

    async def scenario():
        internal, web = hub.subscribe("internal"), hub.subscribe("web")
        hub.end_all("web")
        return internal.closed, web.closed, hub.subscriber_count

    assert asyncio.run(scenario()) == (False, True, 1)


# -- 本物の待ち受け -------------------------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_real_webui_listener_starts_and_stops(tmp_path):
    config_service = ConfigService(tmp_path / "config.json")
    model = config_service.load()
    model.api.web_password_hash = web_password.hash_password(PASSWORD)
    model.api.web_port = _free_port()
    model.api.web_enabled = True
    config_service.save(model)
    context = ApiContext(
        config_service=config_service, clamav_service=None, realtime_manager=None, scan_dispatcher=None,
        update_service=None, update_dispatcher=None, scheduler_service=None, quarantine_service=None,
        exclude_service=None, watch_target_service=None, log_reader_service=None,
        invoker=_DirectInvoker(), events=EventHub())
    context.web = web = WebController(context, ApiTokens(tmp_path / "api"))
    web.apply()
    try:
        port = web.server.port
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{API_PREFIX}/auth/session", method="POST",
            data=json.dumps({"username": "admin", "password": PASSWORD}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"})
        with urllib.request.urlopen(request, timeout=10) as response:
            assert "csrf_token" in json.load(response)
            assert "session=" in response.headers["Set-Cookie"]
    finally:
        web.stop()
    assert not web.is_running


# -- clamqtpie-ctl web ------------------------------------------------------------------------------------
def test_ctl_web_commands_change_the_settings_while_the_service_is_stopped(tmp_path, monkeypatch):
    import clamqtpie_ctl

    monkeypatch.setattr(clamqtpie_ctl, "service_running", lambda *a, **k: False)
    data = str(tmp_path / "svc")

    def run(*args, stdin=""):
        out = io.StringIO()
        code = clamqtpie_ctl.main(["--data-dir", data, "web", *args], out=out,
                                  ask_password=lambda prompt: (_ for _ in ()).throw(AssertionError("聞かない")))
        return code, out.getvalue()

    code, _ = run("enable")
    assert code == clamqtpie_ctl.EXIT_ERROR  # パスワードが未設定
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    code, out = run("set-login", "--password-stdin")
    assert code == 0 and PASSWORD not in out
    code, out = run("enable", "--for", "8h")
    assert code == 0 and "WebUI: ON" in out and "OFF にする時刻: なし" not in out
    code, out = run("timer", "clear")
    assert code == 0 and "OFF にする時刻: なし" in out
    code, out = run("disable")
    assert code == 0 and "WebUI: OFF" in out
    code, out = run("set-login", "--generate")
    generated = out.rsplit(": ", 1)[1].strip()
    assert code == 0 and len(generated) == 20
    saved = ConfigService(tmp_path / "svc" / "config.json").load().api
    assert web_password.verify_password(generated, saved.web_password_hash)


def test_ctl_config_hides_the_hash_and_refuses_web_fields(tmp_path, monkeypatch):
    import clamqtpie_ctl

    monkeypatch.setattr(clamqtpie_ctl, "service_running", lambda *a, **k: False)
    data = str(tmp_path / "svc")
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    clamqtpie_ctl.main(["--data-dir", data, "web", "set-login", "--password-stdin"], out=io.StringIO())
    out = io.StringIO()
    clamqtpie_ctl.main(["--data-dir", data, "config", "show"], out=out)
    assert json.loads(out.getvalue())["api"]["web_password_hash"] == "（設定済み）"
    code = clamqtpie_ctl.main(["--data-dir", data, "config", "set", "api.web_password_hash", "x", "--yes"],
                              out=io.StringIO())
    assert code == clamqtpie_ctl.EXIT_ERROR


def test_password_checks_run_at_most_two_at_a_time(monkeypatch):
    # ログインの要求を一度にたくさん送られても、scrypt の計算・メモリを使い切らせない
    import threading
    import time

    stored = web_password.hash_password(PASSWORD)
    running, peak, lock = [0], [0], threading.Lock()
    original = web_password._derive

    def slow_derive(*args):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        try:
            return original(*args)
        finally:
            with lock:
                running[0] -= 1

    monkeypatch.setattr(web_password, "_derive", slow_derive)
    threads = [threading.Thread(target=web_password.verify_password, args=("wrong-password-1", stored))
               for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert peak[0] <= 2
