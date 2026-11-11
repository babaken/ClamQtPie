"""WebUI の画面（web/）と、それを配る部分のテスト（サービス化・Web API設計書 11章。段階 12-5b）

clamav-webui の tests/test_security.py と同じ考え方で、画面のファイルが文字列から HTML を組み立てないこと、
インラインのスクリプトを使わないこと、応答に CSP などのヘッダが付くことを確かめる。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import API_PREFIX
from api.auth import SESSION_COOKIE
from api.web_static import WEB_DIR
from tests.unit.test_webui_login import PASSWORD, WEB, _logged_in, env  # noqa: F401 - フィクスチャを使う

APP_JS = (WEB_DIR / "app.js").read_text(encoding="utf-8")
INDEX = (WEB_DIR / "index.html").read_text(encoding="utf-8")


# -- 配る部分 ------------------------------------------------------------------------------------------
def test_index_is_served_with_csp_and_security_headers(env):
    response = env.browser.get("/web/")
    assert response.status_code == 200 and "text/html" in response.headers["content-type"]
    csp = response.headers["content-security-policy"]
    assert "script-src 'self';" in csp and "style-src 'self';" in csp and "frame-ancestors 'none'" in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert env.browser.get("/web/login").status_code == 200


def test_root_redirects_to_the_page(env):
    response = env.browser.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/web/"


def test_only_the_page_files_are_served(env):
    script = env.browser.get("/web/app.js")
    assert script.status_code == 200 and script.headers["content-type"].startswith("text/javascript")
    assert script.headers["x-content-type-options"] == "nosniff"
    for name in ("index.html.bak", "..%2Fapi%2Fauth.py", "config.json", "%2e%2e/app.py"):
        assert env.browser.get(f"/web/{name}").status_code == 404


def test_api_errors_on_the_webui_also_get_the_headers(env):
    response = env.browser.get(f"{API_PREFIX}/web")  # ログインしていない
    assert response.status_code == 401 and response.headers["x-content-type-options"] == "nosniff"


def test_internal_api_does_not_serve_the_page(env):
    assert env.internal.get("/web/").status_code == 404
    assert env.internal.get("/web/app.js").status_code == 404


def test_page_reload_gets_the_csrf_token_of_the_session(env):
    csrf = _logged_in(env)
    data = env.browser.get(f"{API_PREFIX}/auth/session").json()
    assert data["csrf_token"] == csrf and data["username"] == "admin" and data["reauthenticated"] is False
    headers = {"Origin": WEB, "X-CSRF-Token": csrf}
    assert env.browser.post(f"{API_PREFIX}/auth/reauth", headers=headers, json={"password": PASSWORD}).status_code == 200
    assert env.browser.get(f"{API_PREFIX}/auth/session").json()["reauthenticated"] is True
    fresh = TestClient(env.web_app, base_url=WEB, client=("127.0.0.1", 50010))
    assert fresh.get(f"{API_PREFIX}/auth/session").status_code == 401


def test_quarantine_from_the_webui_needs_a_recent_reauth(env, tmp_path):
    # WebUI のセッションは admin として扱い、どこのファイルでも動かせるため、隔離にも再確認を求める
    from types import SimpleNamespace

    from api.app import create_app

    target = tmp_path / "found.bin"
    target.write_bytes(b"x")
    moved = []
    env.context.jobs = SimpleNamespace(current=lambda kind: None)
    env.context.notification_store = SimpleNamespace()
    env.context.quarantine_service = SimpleNamespace(quarantine=lambda path, name: moved.append(path) or SimpleNamespace(
        id=1, original_path=path, virus_name=name, sha256="00", size=1, detected_at="2026-11-03T00:00:00+00:00"))
    csrf = _logged_in(env)
    browser = TestClient(create_app(env.tokens, env.context, listener="web", web=env.web), base_url=WEB,
                         client=("127.0.0.1", 50012), cookies={SESSION_COOKIE: env.browser.cookies.get(SESSION_COOKIE)})
    headers = {"Origin": WEB, "X-CSRF-Token": csrf}
    body = {"path": str(target), "virus_name": "Eicar-Test-Signature"}
    first = browser.post(f"{API_PREFIX}/quarantine", headers=headers, json=body)
    assert first.status_code == 403 and first.json()["error"]["code"] == "E8108" and moved == []
    assert browser.post(f"{API_PREFIX}/auth/reauth", headers=headers, json={"password": PASSWORD}).status_code == 200
    assert browser.post(f"{API_PREFIX}/quarantine", headers=headers, json=body).status_code == 201
    assert moved == [str(target)]


def test_session_cookie_has_an_app_specific_name(env):
    # このPC のほかのアプリ（127.0.0.1 の別のポート）の Cookie と名前が重ならないように
    _logged_in(env)
    assert SESSION_COOKIE == "clamqtpie_session" and env.browser.cookies.get(SESSION_COOKIE)
    assert "session" not in env.browser.cookies


def test_detections_of_scans_started_from_the_screens_are_notified():
    # 画面を閉じていても、トレイGUI や後で開いた WebUI で気づけるように
    from core.runtime import connect_scan_notifications

    class _Signal:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

        def emit(self, *args):
            for slot in self.slots:
                slot(*args)

    calls = []
    dispatcher = type("D", (), {"file_scanned": _Signal()})()
    notifier = type("N", (), {"notify_detection": lambda self, path, name, quarantined: calls.append(
        (path, name, quarantined))})()
    connect_scan_notifications(dispatcher, notifier)
    dispatcher.file_scanned.emit("/home/a/eicar.txt", "FOUND", "Eicar-Test-Signature", "2026-11-03T00:00:00+00:00")
    dispatcher.file_scanned.emit("/home/a/big.iso", "ERROR", "読めません", "2026-11-03T00:00:00+00:00")
    assert calls == [("/home/a/eicar.txt", "Eicar-Test-Signature", False)]


def test_status_tells_when_the_webui_turns_off(env, monkeypatch):
    env.web.status = lambda: {"enabled": True, "off_at": "2026-11-04T09:00:00+00:00"}
    from api.routes.read import _web_status

    assert _web_status(env.context) == {"enabled": True, "off_at": "2026-11-04T09:00:00+00:00", "lan": False}


# -- 画面のファイル（XSS・CSP） --------------------------------------------------------------------------
def test_app_js_does_not_build_html_from_strings():
    # ファイル名・検知名・ログは攻撃者が決められる（例: "<img src=x onerror=...>.exe"）
    assert "innerHTML" not in APP_JS and "outerHTML" not in APP_JS and "insertAdjacentHTML" not in APP_JS
    assert "document.write" not in APP_JS and not re.search(r"\beval\s*\(|new Function", APP_JS)


def test_index_has_no_inline_scripts_handlers_or_styles():
    assert not re.search(r"\son[a-z]+\s*=", INDEX)
    assert not re.search(r"<script(?![^>]*\bsrc=)", INDEX)
    assert not re.search(r"\sstyle\s*=", INDEX) and "<style" not in INDEX  # style-src 'self'


def test_data_actions_and_forms_have_handlers():
    actions = set(re.findall(r'data-action="(\w+)"', INDEX))
    table = re.search(r"const ACTIONS = \{(.*?)\};", APP_JS, re.S).group(1)
    assert actions and actions <= set(re.findall(r"\w+", table))
    forms = set(re.findall(r'<form id="([\w-]+)"', INDEX))
    form_table = re.search(r"const FORMS = \{(.*?)\};", APP_JS, re.S).group(1)
    assert forms == set(re.findall(r'"([\w-]+)":', form_table))


def test_screens_in_the_menu_have_sections_and_loaders():
    screens = set(re.findall(r'data-screen="(\w+)"', INDEX))
    sections = set(re.findall(r'<section id="screen-(\w+)"', INDEX))
    loaders = set(re.findall(r"^\s+(\w+): \(\) =>", re.search(r"const LOADERS = \{(.*?)\};", APP_JS, re.S).group(1), re.M))
    assert screens == sections == loaders and len(screens) == 10


def test_api_paths_used_by_the_page_exist(env):
    # app.js が呼ぶ API のパスが、サービスの API に実際にあること（名前の打ち間違いで画面が動かないことを防ぐ）。
    # ログインせずに送り、ルートが無ければ 404・405、あれば認証・入力の検査（401・400 など）になることで見分ける
    used = set()
    for match in re.finditer(r'api\(\s*[`"](/[^`"?]*)[^`"]*[`"](?:\s*,\s*"(\w+)")?', APP_JS):
        used.add((match.group(2) or "GET", re.sub(r"\$\{[^}]+\}", "1", match.group(1))))
    assert len(used) > 30
    from types import SimpleNamespace

    from api.app import create_app

    env.context.jobs = SimpleNamespace(current=lambda kind: None)  # ジョブ・通知の API も付ける
    env.context.notification_store = SimpleNamespace()
    browser = TestClient(create_app(env.tokens, env.context, listener="web", web=env.web), base_url=WEB,
                         client=("127.0.0.1", 50011))
    missing = []
    for method, path in sorted(used):
        response = browser.request(method, f"{API_PREFIX}{path}", headers={"Origin": WEB})
        if response.status_code in (404, 405) and response.json().get("error", {}).get("code") not in ("E8109", "E3003"):
            missing.append((method, path, response.status_code))
    assert not missing, missing


def test_events_handled_by_the_page_are_published_by_the_service():
    handled = set(re.findall(r'^\s+"?([\w.]+)"?: \(', re.search(r"const EVENT_HANDLERS = \{(.*?)\n\};", APP_JS, re.S).group(1), re.M))
    sources = "".join(p.read_text(encoding="utf-8") for p in (Path(__file__).resolve().parents[2] / "api").glob("*.py"))
    published = set(re.findall(r'publish\("([\w.]+)"', sources))
    assert handled and handled <= published, sorted(handled - published)


@pytest.mark.skipif(shutil.which("node") is None, reason="node が無い")
def test_app_js_is_valid_javascript():
    result = subprocess.run(["node", "--check", str(WEB_DIR / "app.js")], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_page_files_are_packaged():
    spec = (Path(__file__).resolve().parents[2] / "ClamQtPie.spec").read_text(encoding="utf-8")
    assert '(str(_ROOT / "web"), "web")' in spec
