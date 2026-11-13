"""
スキャンを止めるときの言葉をそろえる（README_DEV §103。#79）

止める操作は［停止］、止めた後の状態は「中断」。トレイGUI・WebUI・昇格ヘルパーの確認画面・サービスのログで同じ言葉にする。
"""
from __future__ import annotations

import re
from pathlib import Path

from api.web_static import WEB_DIR
from api_client.admin_ops import Operation, describe

APP_JS = (WEB_DIR / "app.js").read_text(encoding="utf-8")
INDEX = (WEB_DIR / "index.html").read_text(encoding="utf-8")
ACTIONS = (Path(__file__).resolve().parents[2] / "api" / "actions.py").read_text(encoding="utf-8")


def test_webui_scan_stop_button_is_named_stop():
    button = re.search(r'<button[^>]*id="scan-cancel"[^>]*>([^<]*)</button>', INDEX)
    assert button and button.group(1) == "停止"


def test_webui_scan_stop_does_not_ask_for_reauth():
    # 停止は利用者の操作（require_user）で再確認を求めないため、再確認のダイアログの文を持たない
    body = re.search(r"async function cancelScan\(\) \{(.*?)\n\}", APP_JS, re.S).group(1)
    assert "adminCall" not in body
    assert 'api("/scans/current/cancel", "POST")' in body


def test_webui_messages_use_stop_and_interrupted():
    assert "スキャンの停止を頼みました" in APP_JS
    assert "スキャンを中断しました" in APP_JS
    assert "スキャンを中止" not in APP_JS and "スキャンの中止" not in APP_JS


def test_elevation_confirmation_uses_stop():
    assert describe(Operation("POST", "/scans/current/cancel")) == ["実行中のスキャンを停止します"]


def test_service_log_uses_stop():
    assert "スキャンの停止を要求しました" in ACTIONS
    assert "スキャンの中止" not in ACTIONS
