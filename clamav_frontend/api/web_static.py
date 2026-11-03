"""
WebUI の画面のファイルを配る（サービス化・Web API設計書 11章。段階 12-5b）

- WebUI の待ち受けにだけ付ける。配るのは web/ の決まったファイル（index.html・app.js・style.css）だけで、
  パスから任意のファイルを読むことはしない
- 画面は 1 枚の HTML（ログインと各画面を JavaScript で切り替える）。ログインしていなくても配る（中身は API で読む）
- 応答には次のヘッダを付ける（clamav-webui と同じ方針）
  - すべて: X-Content-Type-Options: nosniff、X-Frame-Options: DENY、Referrer-Policy: same-origin、Cache-Control: no-store
  - HTML: Content-Security-Policy（スクリプト・スタイルは自分のファイルだけ。インラインのスクリプト・onclick= は
    動かない。ファイル名などは textContent で表示するため、仮に混ざっても実行されない）
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, RedirectResponse

#: 画面のファイルの置き場所（配布版は PyInstaller の _internal/web）
WEB_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "web"
#: 配るファイルと Content-Type
FILES = {
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

router = APIRouter()


def _index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html", media_type="text/html; charset=utf-8",
                        headers={"Content-Security-Policy": CONTENT_SECURITY_POLICY})


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/web/", status_code=303)


@router.get("/web", include_in_schema=False)
def web_root() -> RedirectResponse:
    return RedirectResponse("/web/", status_code=303)


@router.get("/web/", include_in_schema=False)
def index() -> FileResponse:
    return _index()


@router.get("/web/login", include_in_schema=False)
def login_page() -> FileResponse:
    return _index()


@router.get("/web/{name}", include_in_schema=False)
def static_file(name: str):
    if name not in FILES:
        from core.exceptions import ApiNotFoundError

        raise ApiNotFoundError()
    return FileResponse(WEB_DIR / name, media_type=FILES[name])


def add_security_headers(response) -> None:
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
