"""監視するフォルダの案内と、断った要求のログのカテゴリ

- WebUI でも、監視するフォルダが無ければ、パスワードの再確認の前に案内する（トレイGUI と同じ）
- 画面・確認画面の呼び方を「監視するフォルダ」にそろえ、案内が指すボタンが画面にあること
- 断った要求のログは、操作した機能のカテゴリに入れる（ログ画面のカテゴリの絞り込みで探せるように）
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from api_client.admin_ops import Lookup, Operation, describe

WEB = Path(__file__).resolve().parents[2] / "web"


def _refused(path: str, error: Exception, caplog) -> logging.LogRecord:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.errors import install_error_handlers

    app = FastAPI()
    install_error_handlers(app)

    @app.put(path)
    def refuse():
        raise error

    app_logger = logging.getLogger("clamav_frontend")
    app_logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger="clamav_frontend")
    try:
        TestClient(app).put(path)
    finally:
        app_logger.removeHandler(caplog.handler)
    return next(r for r in caplog.records if "断りました" in r.getMessage())


@pytest.mark.parametrize("path,category", [
    ("/api/v1/realtime", "リアルタイム"),
    ("/api/v1/realtime/targets/3", "リアルタイム"),
    ("/api/v1/scans", "スキャン"),
    ("/api/v1/updates/freshclam-service", "更新"),
    ("/api/v1/quarantine/1/restore", "隔離"),
    ("/api/v1/config", "システム"),
    ("/api/v1/web", "システム"),
])
def test_refused_request_is_logged_in_the_category_of_the_feature(caplog, path, category):
    record = _refused(path, ValueError("監視するフォルダがありません"), caplog)
    assert record.category == category and record.error_code == "E8100"
    assert "監視するフォルダがありません" in record.getMessage()


def test_forbidden_request_is_logged_in_the_category_of_the_feature(caplog):
    from core.exceptions import ApiForbiddenError

    record = _refused("/api/v1/realtime", ApiForbiddenError(), caplog)
    assert record.category == "リアルタイム" and record.error_code == "E8102"


def test_webui_guides_before_asking_for_the_password():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    body = js[js.index("async function toggleRealtime()"):]
    body = body[:body.index("\n}\n")]
    # 監視するフォルダの確かめが、再確認（adminCall）より前にある
    assert body.index("hasEnabledTarget()") < body.index("adminCall(")
    assert "監視するフォルダがありません" in body
    # 案内が指すボタンが、リアルタイム保護の画面にある
    html = (WEB / "index.html").read_text(encoding="utf-8")
    section = html[html.index("監視するフォルダ</h3>"):]
    assert re.search(r'<button type="submit" class="primary">追加</button>', section[:600])


def test_tray_realtime_screen_uses_one_name_and_the_guided_button_exists(qtbot):
    from PySide6.QtWidgets import QLabel, QPushButton

    from gui.screens.realtime import RealtimeScreen

    screen = RealtimeScreen()
    qtbot.addWidget(screen)
    texts = [w.text() for w in screen.findChildren(QLabel)] + [b.text() for b in screen.findChildren(QPushButton)]
    assert not [t for t in texts if "監視対象" in t or "フォルダー" in t]
    assert "「フォルダを追加」" in screen.no_target_hint_label.text()
    assert screen.add_target_button.text() == "フォルダを追加"


def test_confirmation_screen_uses_the_same_name():
    lookup = Lookup(targets={3: {"path": "/home/a/Downloads"}})
    lines = describe(Operation("POST", "/realtime/targets", {"path": "/home/a/Downloads"}), lookup)
    lines += describe(Operation("DELETE", "/realtime/targets/3", None), lookup)
    assert all("監視するフォルダ" in line and "監視対象" not in line for line in lines)
