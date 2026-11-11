"""トレイの「WebUI」サブメニューのテスト（サービス化・Web API設計書 3.3節・10章。段階 12-5c）"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtWidgets import QMenu

from api.app import API_PREFIX
from api_client.admin_ops import Operation, describe, is_admin, parse_operations
from core import web_password
from core.exceptions import AppError, ElevationCancelledError
from gui.dialogs.webui_dialogs import WebUiLoginDialog, WebUiTimerDialog
from gui.webui_menu import WebUiMenu, state_text
from tests.unit.test_webui_login import PASSWORD, env  # noqa: F401 - フィクスチャを使う


# -- 昇格ヘルパーの操作の一覧と説明 ----------------------------------------------------------------------
def test_webui_settings_go_through_the_elevation_helper():
    assert is_admin("PUT", "/web") and is_admin("PUT", "/web/credentials")
    assert not is_admin("GET", "/web")  # 状態は user のトークンで読む
    parse_operations({"operations": [{"method": "PUT", "path": "/web", "body": {"enabled": False}}]})


def test_confirmation_never_shows_the_password():
    lines = describe(Operation("PUT", "/web/credentials", {"username": "admin", "password": "secret-pass-1"}))
    assert "admin" in lines[0] and not any("secret-pass-1" in line for line in lines)
    assert any("ログアウト" in line for line in lines)


@pytest.mark.parametrize("body, expected", [
    ({"enabled": True, "off_after_min": 480}, "8 時間後に自動で OFF"),
    ({"enabled": True, "off_after_min": None}, "自動では OFF にしません"),
    ({"enabled": False}, "WebUI を OFF にします"),
    ({"off_after_min": 90}, "1 時間 30 分後に自動で OFF"),
])
def test_confirmation_explains_the_webui_change(body, expected):
    assert expected in "\n".join(describe(Operation("PUT", "/web", body)))


# -- 状態の行 ------------------------------------------------------------------------------------------
def test_state_text():
    assert state_text(None) == "WebUI: 状態を読めません"
    assert state_text({"enabled": False}) == "WebUI: OFF"
    assert state_text({"enabled": True, "running": True, "off_at": None}) == "WebUI: ON（OFF にするまで）"
    soon = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="seconds")
    assert state_text({"enabled": True, "running": True, "off_at": soon}).startswith("WebUI: ON（")
    assert "待ち受けていません" in state_text({"enabled": True, "running": False})


# -- メニュー（本物の API に送る） -------------------------------------------------------------------------
class _Caller:
    """ElevatedCaller の代わり。admin の操作は、昇格ヘルパーと同じく admin のトークンで内部 API に送る"""

    def __init__(self, env, cancel=False):  # noqa: F811
        self.env, self.cancel, self.batches = env, cancel, []

    def call(self, method, path, body=None):
        response = self.env.internal.request(method, f"{API_PREFIX}{path}", headers=self.env.admin, json=body)
        assert response.status_code < 400, response.text
        return response.json()

    def batch(self, operations):
        self.batches.append([(op.method, op.path) for op in operations])
        if self.cancel:
            raise ElevationCancelledError()
        return [self.call(op.method, op.path, op.body) for op in operations]


def _menu(env, qapp, monkeypatch, login=None, cancel=False):  # noqa: F811
    caller = _Caller(env, cancel)
    menu = WebUiMenu(QMenu(), caller)
    monkeypatch.setattr(menu, "_ask_login", lambda first_time: login)
    shown = []
    monkeypatch.setattr("gui.webui_menu.GeneratedPasswordDialog.exec", lambda self: shown.append(True))
    monkeypatch.setattr("gui.webui_menu.show_error_dialog", lambda parent, exc: shown.append(exc))
    menu.refresh()
    return menu, caller, shown


def test_turning_on_without_a_password_sets_the_login_first_in_one_elevation(env, qapp, monkeypatch):  # noqa: F811
    menu, caller, shown = _menu(env, qapp, monkeypatch, login=("admin", "generated-pass-12345", True))
    assert menu.state_action.text() == "WebUI: OFF" and not menu.open_action.isEnabled()
    assert not menu.off_action.isEnabled() and menu.on_menu.isEnabled()
    menu.duration_actions[1].trigger()  # 8 時間
    assert caller.batches == [[("PUT", "/web/credentials"), ("PUT", "/web")]]
    status = env.internal.get(f"{API_PREFIX}/web", headers=env.admin).json()
    assert status["enabled"] and status["password_set"] and status["off_at"]
    assert shown == [True]  # 自動で作ったパスワードを 1 回だけ見せる
    assert menu.state_action.text().startswith("WebUI: ON（") and menu.off_action.isEnabled()
    assert not menu.on_menu.isEnabled()  # ON の間は「OFF にする時刻を変更...」を使う
    saved = env.config.load().api
    assert web_password.verify_password("generated-pass-12345", saved.web_password_hash)


def test_turning_on_with_a_password_only_turns_it_on_and_off_again(env, qapp, monkeypatch):  # noqa: F811
    env.internal.put(f"{API_PREFIX}/web/credentials", headers=env.admin, json={"username": "admin", "password": PASSWORD})
    menu, caller, _ = _menu(env, qapp, monkeypatch)
    menu.forever_action.trigger()
    assert caller.batches == [[("PUT", "/web")]]
    assert menu.state_action.text() == "WebUI: ON（OFF にするまで）"
    menu.off_action.trigger()
    assert menu.state_action.text() == "WebUI: OFF"


def test_cancelling_the_login_dialog_or_the_elevation_changes_nothing(env, qapp, monkeypatch):  # noqa: F811
    menu, caller, _ = _menu(env, qapp, monkeypatch, login=None)
    menu.duration_actions[0].trigger()
    assert caller.batches == []
    menu, caller, shown = _menu(env, qapp, monkeypatch, login=("admin", PASSWORD, False), cancel=True)
    menu.duration_actions[0].trigger()
    assert caller.batches and isinstance(shown[0], ElevationCancelledError)  # 取り消しはエラーを出さない（show_error_dialog が無視する）
    assert not env.internal.get(f"{API_PREFIX}/web", headers=env.admin).json()["enabled"]


def test_status_that_cannot_be_read_disables_the_items(qapp):
    class _Down:
        def call(self, *args, **kwargs):
            raise AppError("つながりません", code="E8105")

    menu = WebUiMenu(QMenu(), _Down())
    menu.refresh()
    assert menu.state_action.text() == "WebUI: 状態を読めません"
    assert not menu.on_menu.isEnabled() and not menu.login_action.isEnabled()


def test_tray_has_a_webui_submenu_hidden_until_connected(qapp, tmp_path):
    from gui.tray import TrayIcon

    tray = TrayIcon(tmp_path)
    assert tray.webui_menu.title() == "WebUI" and not tray.webui_menu.menuAction().isVisible()


# -- ダイアログ ----------------------------------------------------------------------------------------
def test_timer_dialog_reads_a_duration_or_a_date(qapp):
    dialog = WebUiTimerDialog()
    dialog.duration_edit.setText("1d12h")
    dialog._try_accept()
    assert dialog.body() == {"off_after_min": 36 * 60}
    dialog = WebUiTimerDialog()
    dialog.duration_edit.setText("8 hours")
    dialog._try_accept()
    assert dialog.body() is None and "長さの書き方" in dialog.error_label.text()
    dialog = WebUiTimerDialog()
    dialog.at_radio.setChecked(True)
    dialog._try_accept()  # 既定は 1 時間ほど先
    assert set(dialog.body()) == {"off_at"}


def test_login_dialog_checks_the_input(qapp):
    dialog = WebUiLoginDialog(username="admin")
    dialog.password_edit.setText("short")
    dialog.confirm_edit.setText("short")
    dialog._try_accept()
    assert dialog.result_value() is None and "8 文字" in dialog.error_label.text()
    dialog.password_edit.setText("long-enough-1")
    dialog.confirm_edit.setText("long-enough-2")
    dialog._try_accept()
    assert "一致しません" in dialog.error_label.text()
    dialog.generate_check.setChecked(True)
    assert not dialog.password_edit.isEnabled()
    dialog._try_accept()
    username, password, generated = dialog.result_value()
    assert username == "admin" and generated and len(password) == 20


def test_dialog_buttons_are_in_japanese(qapp):
    from PySide6.QtWidgets import QDialogButtonBox, QPushButton

    from gui.dialogs.webui_dialogs import GeneratedPasswordDialog

    for dialog in (WebUiTimerDialog(), WebUiLoginDialog()):
        labels = {b.text() for b in dialog.findChild(QDialogButtonBox).buttons()}
        assert "キャンセル" in labels and "Cancel" not in labels
    dialog = GeneratedPasswordDialog(None, "admin", "x" * 20)
    assert "閉じる" in {b.text() for b in dialog.findChildren(QPushButton)}



def test_service_explanation_is_the_error_message(caplog):
    # トレイGUI のエラーのダイアログはメッセージとコードだけを出すため、サービスの説明はメッセージで返す。
    # 断った要求はログにも残す（ダイアログの「詳細を見る」でコードから探せるように）
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.errors import install_error_handlers

    app = FastAPI()
    install_error_handlers(app)

    @app.put("/realtime")
    def start():
        raise ValueError("監視するフォルダがありません。先に監視するフォルダを追加してください")

    caplog.set_level("WARNING", logger="clamav_frontend")
    logging_logger = __import__("logging").getLogger("clamav_frontend")
    logging_logger.addHandler(caplog.handler)
    try:
        response = TestClient(app).put("/realtime")
    finally:
        logging_logger.removeHandler(caplog.handler)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "E8100" and error["message"].startswith("監視するフォルダがありません")
    assert any(getattr(r, "error_code", "") == "E8100" for r in caplog.records)
