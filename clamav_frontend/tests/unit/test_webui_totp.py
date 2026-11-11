"""WebUI の二段階認証（TOTP）のテスト（サービス化・Web API設計書 3.3節・6.11節。段階 12-5d）"""
from __future__ import annotations

import base64
import io
import json
import sys
import time

import pytest
from PySide6.QtWidgets import QMenu

from api.app import API_PREFIX
from api_client.admin_ops import Operation, describe, is_admin
from core import totp
from core.config_service import ConfigService
from core.secret_store import FileSecretStore
from tests.unit.test_webui_login import PASSWORD, WEB, _login, _set_login, env  # noqa: F401 - フィクスチャを使う

#: RFC 6238 の付録 B の秘密鍵（"12345678901234567890" の Base32）
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode("ascii")


# -- 計算（RFC 6238） -----------------------------------------------------------------------------------
@pytest.mark.parametrize("unix_time, expected", [
    (59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037"),
])
def test_codes_match_the_rfc_6238_test_vectors(unix_time, expected):
    assert totp.code_at(RFC_SECRET, totp.current_step(unix_time)) == expected


def test_a_code_is_accepted_within_one_step_and_never_twice():
    now = 1_700_000_000.0
    step = totp.current_step(now)
    for offset in (-1, 0, 1):
        assert totp.match_step(RFC_SECRET, totp.code_at(RFC_SECRET, step + offset), 0, now) == step + offset
    assert totp.match_step(RFC_SECRET, totp.code_at(RFC_SECRET, step + 2), 0, now) is None  # 60 秒以上ずれている
    assert totp.match_step(RFC_SECRET, totp.code_at(RFC_SECRET, step), step, now) is None  # 使ったステップ
    assert totp.match_step(RFC_SECRET, totp.code_at(RFC_SECRET, step - 1), step, now) is None  # それより前
    assert totp.match_step(RFC_SECRET, "12345", 0, now) is None
    assert totp.match_step(RFC_SECRET, "abcdef", 0, now) is None


def test_secret_is_twenty_random_bytes_and_input_is_checked():
    secret = totp.generate_secret()
    assert len(secret) == 32 and secret != totp.generate_secret()
    assert totp.normalize_secret(" ".join(secret[i:i + 4] for i in range(0, 32, 4)).lower()) == secret
    for bad in ("", "ABC", "1" * 32, RFC_SECRET[:-8], None):
        with pytest.raises(ValueError):
            totp.normalize_secret(bad)


def test_otpauth_uri_names_the_app_and_the_pc():
    uri = totp.otpauth_uri(RFC_SECRET, "TESTPC")
    assert uri.startswith("otpauth://totp/ClamQtPie%3ATESTPC?secret=" + RFC_SECRET)
    assert "issuer=ClamQtPie" in uri and "digits=6" in uri and "period=30" in uri


def test_recovery_codes_are_single_use_hashes():
    codes = totp.generate_recovery_codes()
    assert len(codes) == 10 and len(set(codes)) == 10
    assert all(len(code) == 11 and code[5] == "-" for code in codes)
    hashes = totp.hash_recovery_codes(codes[:2])
    assert all(codes[0].replace("-", "") not in h for h in hashes)  # 平文は残さない
    assert totp.match_recovery_code(codes[1].lower().replace("-", " "), hashes) == 1  # 大小・区切りは問わない
    assert totp.match_recovery_code(codes[2], hashes) is None
    assert totp.match_recovery_code("123456", hashes) is None


def test_qr_code_images():
    assert totp.qr_png(totp.otpauth_uri(RFC_SECRET, "admin")).startswith(b"\x89PNG")
    text = totp.qr_text("otpauth://totp/x?secret=" + RFC_SECRET)
    lines = text.splitlines()
    assert len(lines) > 10 and len({len(line) for line in lines}) == 1


# -- API --------------------------------------------------------------------------------------------------
@pytest.fixture
def tenv(env, tmp_path):  # noqa: F811
    env.config._secret_store = FileSecretStore(tmp_path / "secrets")
    _set_login(env)
    return env


def _enable(env, secret=RFC_SECRET, now=None):
    code = totp.code_at(secret, totp.current_step(now))
    response = env.internal.post(f"{API_PREFIX}/web/totp", headers=env.admin, json={"secret": secret, "code": code})
    assert response.status_code == 200, response.text
    return response.json()["recovery_codes"]


def _next_code(secret=RFC_SECRET):
    """今のステップのコードはもう使ったので、次のステップのコード（±1 ステップまで受け付ける）"""
    return totp.code_at(secret, totp.current_step() + 1)


def test_enabling_needs_a_correct_code_and_keeps_the_secret_out_of_the_config(tenv, tmp_path):
    response = tenv.internal.post(f"{API_PREFIX}/web/totp", headers=tenv.admin,
                                  json={"secret": RFC_SECRET, "code": "000000"})
    assert response.status_code == 400 and "確認コードが違います" in response.json()["error"]["message"]
    assert not tenv.config.current.api.web_totp_enabled
    _login(tenv)  # セッションを 1 つ作っておく
    codes = _enable(tenv)
    assert len(codes) == 10
    status = tenv.internal.get(f"{API_PREFIX}/web", headers=tenv.admin).json()
    assert status["totp_set"] and status["recovery_codes_left"] == 10
    saved = (tmp_path / "config.json").read_text(encoding="utf-8")
    assert RFC_SECRET not in saved and not any(code.replace("-", "") in saved for code in codes)
    assert tenv.config.get_totp_secret() == RFC_SECRET
    assert tenv.web.sessions.count == 0  # 設定したらセッションを破棄する


def test_login_needs_the_code_and_the_same_code_is_refused_again(tenv):
    _enable(tenv)
    assert _login(tenv).status_code == 401  # コードなし
    code = _next_code()
    response = tenv.browser.post(f"{API_PREFIX}/auth/session", headers={"Origin": WEB},
                                 json={"username": "admin", "password": "wrong-password", "code": code})
    assert response.status_code == 401
    assert "確認コード" in response.json()["error"]["message"]
    # パスワードが違ったときは、コードを使ったことにしない
    ok = tenv.browser.post(f"{API_PREFIX}/auth/session", headers={"Origin": WEB},
                           json={"username": "admin", "password": PASSWORD, "code": code})
    assert ok.status_code == 200 and ok.json()["totp"] is True
    again = tenv.browser.post(f"{API_PREFIX}/auth/session", headers={"Origin": WEB},
                              json={"username": "admin", "password": PASSWORD, "code": code})
    assert again.status_code == 401  # 使い回しは受け付けない


def test_a_recovery_code_logs_in_once(tenv):
    codes = _enable(tenv)
    body = {"username": "admin", "password": PASSWORD, "code": codes[3]}
    assert tenv.browser.post(f"{API_PREFIX}/auth/session", headers={"Origin": WEB}, json=body).status_code == 200
    assert tenv.browser.post(f"{API_PREFIX}/auth/session", headers={"Origin": WEB}, json=body).status_code == 401
    assert tenv.internal.get(f"{API_PREFIX}/web", headers=tenv.admin).json()["recovery_codes_left"] == 9


def test_reauth_uses_the_code_instead_of_the_password(tenv):
    codes = _enable(tenv)
    login = tenv.browser.post(f"{API_PREFIX}/auth/session", headers={"Origin": WEB},
                              json={"username": "admin", "password": PASSWORD, "code": codes[0]})
    headers = {"Origin": WEB, "X-CSRF-Token": login.json()["csrf_token"]}
    refused = tenv.browser.post(f"{API_PREFIX}/auth/reauth", headers=headers, json={"password": PASSWORD})
    assert refused.status_code == 403 and "確認コード" in refused.json()["error"]["message"]
    assert tenv.browser.get(f"{API_PREFIX}/auth/session").json()["totp"] is True
    ok = tenv.browser.post(f"{API_PREFIX}/auth/reauth", headers=headers, json={"code": _next_code()})
    assert ok.status_code == 200


def test_only_the_bearer_admin_can_change_two_step_verification(tenv):
    csrf = _login(tenv).json()["csrf_token"]
    headers = {"Origin": WEB, "X-CSRF-Token": csrf}
    code = totp.code_at(RFC_SECRET, totp.current_step())
    assert tenv.browser.post(f"{API_PREFIX}/web/totp", headers=headers,
                             json={"secret": RFC_SECRET, "code": code}).status_code == 403
    user = {"Authorization": f"Bearer {tenv.tokens.token_for('user')}"}
    assert tenv.internal.post(f"{API_PREFIX}/web/totp", headers=user,
                              json={"secret": RFC_SECRET, "code": code}).status_code == 403
    assert tenv.internal.delete(f"{API_PREFIX}/web/totp", headers=user).status_code == 403


def test_disabling_removes_the_secret_and_the_codes(tenv):
    _enable(tenv)
    response = tenv.internal.delete(f"{API_PREFIX}/web/totp", headers=tenv.admin)
    assert response.status_code == 200 and not response.json()["totp_set"]
    api = tenv.config.current.api
    assert not api.web_totp_enabled and api.web_recovery_code_hashes == [] and api.web_totp_last_step == 0
    assert tenv.config.get_totp_secret() is None
    assert _login(tenv).status_code == 200  # パスワードだけでログインできる


def test_a_missing_secret_does_not_let_anyone_in(tenv):
    # 秘密鍵を読めないときは、二段階認証を外さずにログインを断る（解除は clamqtpie-ctl web totp reset）
    _enable(tenv)
    tenv.config.delete_totp_secret()
    response = tenv.browser.post(f"{API_PREFIX}/auth/session", headers={"Origin": WEB},
                                 json={"username": "admin", "password": PASSWORD, "code": _next_code()})
    assert response.status_code == 401


def test_broken_settings_keep_two_step_verification_on():
    from core.config_model import ConfigModel, validate_and_fix

    model = ConfigModel()
    model.api.web_totp_enabled = "yes"
    model.api.web_recovery_code_hashes = ["broken"]
    validate_and_fix(model, lambda message: None)
    assert model.api.web_totp_enabled is True and model.api.web_recovery_code_hashes == []


# -- 昇格ヘルパーの確認画面 -----------------------------------------------------------------------------
def test_confirmation_never_shows_the_secret_or_the_code():
    assert is_admin("POST", "/web/totp") and is_admin("DELETE", "/web/totp")
    lines = describe(Operation("POST", "/web/totp", {"secret": RFC_SECRET, "code": "123456"}))
    assert "二段階認証を設定" in lines[0]
    assert not any(RFC_SECRET in line or "123456" in line for line in lines)
    assert "解除" in describe(Operation("DELETE", "/web/totp"))[0]


# -- トレイの「二段階認証の設定...」 ----------------------------------------------------------------------
class _Caller:
    def __init__(self, env):  # noqa: F811
        self.env, self.batches = env, []

    def call(self, method, path, body=None):
        response = self.env.internal.request(method, f"{API_PREFIX}{path}", headers=self.env.admin, json=body)
        assert response.status_code < 400, response.text
        return response.json()

    def batch(self, operations):
        self.batches.append([(op.method, op.path) for op in operations])
        return [self.call(op.method, op.path, op.body) for op in operations]


def test_tray_sets_up_two_step_verification_in_one_elevation(tenv, qapp, monkeypatch):
    from gui.webui_menu import WebUiMenu

    shown = []

    class _Setup:
        DialogCode = type("DialogCode", (), {"Accepted": 1})

        def __init__(self, parent, secret=None, error=None):
            self.secret = RFC_SECRET

        def exec(self):
            return 1

        def code(self):
            return totp.code_at(RFC_SECRET, totp.current_step())

    monkeypatch.setattr("gui.webui_menu.TotpSetupDialog", _Setup)
    monkeypatch.setattr("gui.webui_menu.RecoveryCodesDialog",
                        lambda parent, codes: type("D", (), {"exec": lambda self: shown.append(codes)})())
    caller = _Caller(tenv)
    menu = WebUiMenu(QMenu(), caller)
    menu.refresh()
    assert menu.totp_action.isEnabled() and menu.totp_action.text() == "二段階認証の設定..."
    menu.totp_action.trigger()
    assert caller.batches == [[("POST", "/web/totp")]]  # 秘密鍵とコードを 1 回の昇格で送る
    assert len(shown) == 1 and len(shown[0]) == 10  # 予備のコードを 1 回だけ見せる
    assert menu.totp_action.text().endswith("（設定済み）")
    monkeypatch.setattr(menu, "_ask_totp_action", lambda: "disable")
    menu.totp_action.trigger()
    assert caller.batches[-1] == [("DELETE", "/web/totp")]
    assert not tenv.config.current.api.web_totp_enabled


def test_setup_dialog_shows_a_qr_code_and_checks_the_code(qapp):
    from gui.dialogs.webui_dialogs import RecoveryCodesDialog, TotpSetupDialog

    dialog = TotpSetupDialog(account="TESTPC", secret=RFC_SECRET)
    assert not dialog.qr_label.pixmap().isNull()
    assert dialog.secret_edit.text().replace(" ", "") == RFC_SECRET
    dialog.code_edit.setText("12a456")
    dialog._try_accept()
    assert dialog.code() is None and "6 桁" in dialog.error_label.text()
    dialog.code_edit.setText("123456")
    dialog._try_accept()
    assert dialog.code() == "123456"
    codes = totp.generate_recovery_codes()
    assert RecoveryCodesDialog(None, codes).codes_edit.toPlainText().splitlines() == codes


# -- clamqtpie-ctl web totp ---------------------------------------------------------------------------------
def test_ctl_totp_setup_status_and_reset_while_the_service_is_stopped(tmp_path, monkeypatch):
    import clamqtpie_ctl

    monkeypatch.setattr(clamqtpie_ctl, "service_running", lambda *a, **k: False)
    monkeypatch.setattr(totp, "generate_secret", lambda: RFC_SECRET)
    data = str(tmp_path / "svc")

    def run(*args, answers=()):
        replies = list(answers)
        out = io.StringIO()
        code = clamqtpie_ctl.main(["--data-dir", data, "web", "totp", *args], out=out,
                                  ask=lambda prompt: replies.pop(0))
        return code, out.getvalue()

    code, out = run("setup", answers=["000000"])
    assert code == clamqtpie_ctl.EXIT_ERROR
    code, out = run("setup", answers=[totp.code_at(RFC_SECRET, totp.current_step())])
    assert code == 0 and "GEZD GNBV" in out and "予備のコード" in out
    codes = [line.strip() for line in out.splitlines() if len(line.strip()) == 11 and line.strip()[5] == "-"]
    assert len(codes) == 10
    saved_text = (tmp_path / "svc" / "config.json").read_text(encoding="utf-8")
    assert RFC_SECRET not in saved_text
    code, out = run("status")
    assert code == 0 and "設定済み（予備のコードの残り: 10 個）" in out
    out_show = io.StringIO()
    clamqtpie_ctl.main(["--data-dir", data, "config", "show"], out=out_show)
    assert json.loads(out_show.getvalue())["api"]["web_recovery_code_hashes"] == "（10 個。伏せています）"
    code, _ = run("reset", answers=["n"])
    assert code == clamqtpie_ctl.EXIT_ERROR
    code, out = run("reset", "--yes")
    assert code == 0 and "解除しました" in out
    saved = ConfigService(tmp_path / "svc" / "config.json").load().api
    assert not saved.web_totp_enabled


def test_ctl_refuses_to_set_totp_fields_directly(tmp_path, monkeypatch):
    import clamqtpie_ctl

    monkeypatch.setattr(clamqtpie_ctl, "service_running", lambda *a, **k: False)
    code = clamqtpie_ctl.main(["--data-dir", str(tmp_path / "svc"), "config", "set", "api.web_totp_enabled", "false",
                               "--yes"], out=io.StringIO())
    assert code == clamqtpie_ctl.EXIT_ERROR


def test_terminal_without_block_characters_gets_the_secret_only():
    import clamqtpie_ctl

    class _Ascii(io.StringIO):
        encoding = "ascii"

    out = _Ascii()
    clamqtpie_ctl._print_qr("otpauth://totp/x?secret=" + RFC_SECRET, out)
    assert "QR コードを表示できません" in out.getvalue()


def test_time_is_used_by_default():
    assert totp.current_step() == int(time.time() // 30)


# -- 認証アプリに登録する名前（この PC の名前） ------------------------------------------------------------
def test_the_app_registers_this_pc_by_name(monkeypatch):
    import socket

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("COMPUTERNAME", "TESTPC")
    assert totp.machine_name() == "TESTPC"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(socket, "gethostname", lambda: "test-host\n")
    assert totp.machine_name() == "test-host"
    monkeypatch.setattr(socket, "gethostname", lambda: "")
    assert totp.machine_name() == "PC"


def test_setup_uses_the_pc_name_not_the_user_name(qapp, monkeypatch, tmp_path):
    import clamqtpie_ctl
    from gui.dialogs.webui_dialogs import TotpSetupDialog

    accounts = []
    real = totp.otpauth_uri
    monkeypatch.setattr(totp, "machine_name", lambda: "TESTPC")
    monkeypatch.setattr(totp, "otpauth_uri", lambda secret, account, **k: accounts.append(account) or real(secret, account))
    TotpSetupDialog(secret=RFC_SECRET)
    monkeypatch.setattr(clamqtpie_ctl, "service_running", lambda *a, **k: False)
    monkeypatch.setattr(totp, "generate_secret", lambda: RFC_SECRET)
    clamqtpie_ctl.main(["--data-dir", str(tmp_path / "svc"), "web", "totp", "setup"], out=io.StringIO(),
                       ask=lambda prompt: totp.code_at(RFC_SECRET, totp.current_step()))
    assert accounts == ["TESTPC", "TESTPC"]


# -- 予備のコードを使ったときの知らせ（画面に残りの数を出す） ---------------------------------------------------
def test_using_a_recovery_code_reports_how_many_are_left(tenv):
    codes = _enable(tenv)
    login = tenv.browser.post(f"{API_PREFIX}/auth/session", headers={"Origin": WEB},
                              json={"username": "admin", "password": PASSWORD, "code": codes[0]})
    assert login.json()["recovery_used"] is True and login.json()["recovery_codes_left"] == 9
    headers = {"Origin": WEB, "X-CSRF-Token": login.json()["csrf_token"]}
    reauth = tenv.browser.post(f"{API_PREFIX}/auth/reauth", headers=headers, json={"code": codes[1]})
    assert reauth.status_code == 200 and reauth.json()["recovery_codes_left"] == 8
    by_code = tenv.browser.post(f"{API_PREFIX}/auth/reauth", headers=headers, json={"code": _next_code()})
    assert by_code.status_code == 200 and "recovery_used" not in by_code.json()


def test_the_web_page_tells_where_to_set_up_again():
    from pathlib import Path

    script = (Path(__file__).resolve().parents[2] / "web" / "app.js").read_text(encoding="utf-8")
    assert "showRecoveryNotice(data)" in script and "showRecoveryNotice(reauthData)" in script
    assert "clamqtpie-ctl web totp setup" in script and "予備のコードがなくなりました" in script


# -- 確認コードの間違いが続いたら、管理者が解くまでログインを止める -----------------------------------------------
def _wrong_code():
    return totp.code_at(RFC_SECRET, totp.current_step() - 100)  # 受け付ける時間の外


def _try(env, password=PASSWORD, code=None):
    from api.web_sessions import LoginGuard

    env.web.guard = LoginGuard()  # 接続元ごとのロック（5 回）とは別に確かめる
    return env.browser.post(f"{API_PREFIX}/auth/session", headers={"Origin": WEB},
                            json={"username": "admin", "password": password, "code": code})


def test_wrong_codes_with_the_right_password_stop_logins_until_an_admin_unlocks(tenv, caplog):
    codes = _enable(tenv)
    for _ in range(totp.STOP_FAILURES - 1):
        assert _try(tenv, code=_wrong_code()).status_code == 401
    assert _try(tenv, password="wrong-password", code=_wrong_code()).status_code == 401  # パスワード違いは数えない
    assert tenv.config.current.api.web_totp_failures == totp.STOP_FAILURES - 1
    assert "パスワードが漏れているおそれ" in caplog.text
    assert not tenv.internal.get(f"{API_PREFIX}/web", headers=tenv.admin).json()["login_stopped"]
    assert _try(tenv, code=_wrong_code()).status_code == 401
    assert tenv.internal.get(f"{API_PREFIX}/web", headers=tenv.admin).json()["login_stopped"]
    assert tenv.notices[-1] == "WebUI のログインを止めました"
    # 止めている間は、正しいコードでも入れない。パスワード違いと応答を区別しない
    stopped = _try(tenv, code=_next_code())
    wrong = _try(tenv, password="wrong-password", code=_next_code())
    assert stopped.status_code == wrong.status_code == 429
    assert stopped.json() == wrong.json()
    assert _try(tenv, password="wrong-password", code=codes[0]).status_code == 429
    # 予備のコード（パスワードも正しい）なら入れる
    rescue = _try(tenv, code=codes[0])
    assert rescue.status_code == 200 and rescue.json()["recovery_used"] is True
    # 時間・再起動では戻らない（回数は設定ファイルに残る）
    assert ConfigService(tenv.config.config_path).load().api.web_totp_failures == totp.STOP_FAILURES
    # 解くのは admin の Bearer だけ
    user = {"Authorization": f"Bearer {tenv.tokens.token_for('user')}"}
    assert tenv.internal.post(f"{API_PREFIX}/web/unlock", headers=user).status_code == 403
    csrf = rescue.json()["csrf_token"]
    assert tenv.browser.post(f"{API_PREFIX}/web/unlock", headers={"Origin": WEB, "X-CSRF-Token": csrf}).status_code == 403
    unlocked = tenv.internal.post(f"{API_PREFIX}/web/unlock", headers=tenv.admin)
    assert unlocked.status_code == 200 and not unlocked.json()["login_stopped"]
    assert _try(tenv, code=_next_code()).status_code == 200


def test_only_admin_actions_reset_the_count(tenv):
    _enable(tenv)
    for _ in range(3):
        _try(tenv, code=_wrong_code())
    assert _try(tenv, code=_next_code()).status_code == 200
    assert tenv.config.current.api.web_totp_failures == 3  # ログインに成功しても戻さない
    _set_login(tenv)  # パスワードを変えたら 0 に戻す
    assert tenv.config.current.api.web_totp_failures == 0


def test_a_stopped_login_looks_like_the_per_client_lock(tenv):
    _enable(tenv)
    for _ in range(totp.STOP_FAILURES):
        _try(tenv, code=_wrong_code())
    stopped = _try(tenv, code=_next_code()).json()
    from api.web_sessions import LoginGuard

    tenv.web.guard = LoginGuard()
    for _ in range(5):
        tenv.browser.post(f"{API_PREFIX}/auth/session", headers={"Origin": WEB},
                          json={"username": "admin", "password": "wrong-password", "code": None})
    locked = tenv.browser.post(f"{API_PREFIX}/auth/session", headers={"Origin": WEB},
                               json={"username": "admin", "password": "wrong-password", "code": None})
    assert locked.status_code == 429 and locked.json() == stopped  # 残りの時間も書かない


def test_broken_failure_count_counts_as_stopped():
    from core.config_model import ConfigModel, validate_and_fix

    model = ConfigModel()
    model.api.web_totp_enabled = True
    model.api.web_totp_failures = "x"
    validate_and_fix(model, lambda message: None)
    assert model.api.web_totp_failures == totp.STOP_FAILURES
    model.api.web_totp_enabled = False
    validate_and_fix(model, lambda message: None)
    assert model.api.web_totp_failures == 0


def test_unlock_confirmation_and_tray_item(tenv, qapp):
    from gui.webui_menu import WebUiMenu

    assert is_admin("POST", "/web/unlock")
    assert "ログインの停止を解きます" in describe(Operation("POST", "/web/unlock"))[0]
    _enable(tenv)
    caller = _Caller(tenv)
    menu = WebUiMenu(QMenu(), caller)
    menu.refresh()
    assert not menu.unlock_action.isVisible()
    for _ in range(totp.STOP_FAILURES):
        _try(tenv, code=_wrong_code())
    menu.refresh()
    assert menu.unlock_action.isVisible() and menu.unlock_action.isEnabled()
    menu.unlock_action.trigger()
    assert caller.batches[-1] == [("POST", "/web/unlock")]
    assert not menu.unlock_action.isVisible()


def test_ctl_unlock_while_the_service_is_stopped(tmp_path, monkeypatch):
    import clamqtpie_ctl

    monkeypatch.setattr(clamqtpie_ctl, "service_running", lambda *a, **k: False)
    data = str(tmp_path / "svc")
    path = tmp_path / "svc" / "config.json"
    clamqtpie_ctl.main(["--data-dir", data, "web", "status"], out=io.StringIO())  # 設定ファイルを作る
    service = ConfigService(path)
    model = service.load()
    model.api.web_totp_enabled, model.api.web_totp_failures = True, totp.STOP_FAILURES
    service.save(model)
    out = io.StringIO()
    clamqtpie_ctl.main(["--data-dir", data, "web", "status"], out=out)
    assert "ログイン: 止めています" in out.getvalue() and "web unlock" in out.getvalue()
    out = io.StringIO()
    assert clamqtpie_ctl.main(["--data-dir", data, "web", "unlock"], out=out) == 0
    assert "停止を解きました" in out.getvalue()
    assert ConfigService(path).load().api.web_totp_failures == 0
    code = clamqtpie_ctl.main(["--data-dir", data, "config", "set", "api.web_totp_failures", "0", "--yes"],
                              out=io.StringIO())
    assert code == clamqtpie_ctl.EXIT_ERROR  # 直接は変えさせない


def test_tray_reopens_with_the_same_secret_when_the_code_has_expired(tenv, qapp, monkeypatch):
    """確認コードは昇格の確認の後で確かめるので、時間をかけると切れる。秘密鍵を作り直さずに、コードだけ入れ直させる"""
    from gui.webui_menu import WebUiMenu

    opened, shown = [], []
    codes = iter(["000000", totp.code_at(RFC_SECRET, totp.current_step())])  # 1 回目は切れたコード

    class _Setup:
        DialogCode = type("DialogCode", (), {"Accepted": 1})

        def __init__(self, parent, secret=None, error=None):
            opened.append((secret, error))
            self.secret = secret or RFC_SECRET
            self._code = next(codes)

        def exec(self):
            return 1

        def code(self):
            return self._code

    class _Refusing(_Caller):
        def call(self, method, path, body=None):
            response = self.env.internal.request(method, f"{API_PREFIX}{path}", headers=self.env.admin, json=body)
            if response.status_code >= 400:
                from api_client.client import error_from_body
                raise error_from_body(response.status_code, response.content)
            return response.json()

    monkeypatch.setattr("gui.webui_menu.TotpSetupDialog", _Setup)
    monkeypatch.setattr("gui.webui_menu.RecoveryCodesDialog",
                        lambda parent, codes: type("D", (), {"exec": lambda self: shown.append(codes)})())
    monkeypatch.setattr("gui.webui_menu.show_error_dialog", lambda *a, **k: pytest.fail("エラーのダイアログを出した"))
    caller = _Refusing(tenv)
    menu = WebUiMenu(QMenu(), caller)
    menu.refresh()
    menu.totp_action.trigger()
    assert len(opened) == 2 and opened[0] == (None, None)
    assert opened[1][0] == RFC_SECRET and "確認コードが違います" in opened[1][1]  # 同じ秘密鍵・断られた理由
    assert len(caller.batches) == 2 and len(shown) == 1 and tenv.config.current.api.web_totp_enabled


def test_totp_setup_dialog_shows_why_it_was_reopened(qapp):
    from gui.dialogs.webui_dialogs import TotpSetupDialog

    dialog = TotpSetupDialog(secret=RFC_SECRET, error="確認コードが違います")
    assert dialog.secret == RFC_SECRET and dialog.error_label.text() == "確認コードが違います"


def test_totp_choice_box_title_names_the_app(tenv, qapp, monkeypatch):
    from gui.webui_menu import WebUiMenu

    titles = []

    def exec_box(box):
        titles.append(box.windowTitle())
        return 0

    monkeypatch.setattr("gui.webui_menu.QMessageBox.exec", exec_box)
    WebUiMenu(QMenu(), _Caller(tenv))._ask_totp_action()
    assert titles == ["ClamQtPie: WebUI の二段階認証"]
