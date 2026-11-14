"""
WebUI の画面のファイルを配る（サービス化・Web API設計書 11章。段階 12-5b）

- WebUI の待ち受けにだけ付ける。配るのは web/ の決まったファイル（index.html・app.js・style.css）だけで、
  パスから任意のファイルを読むことはしない
- HTTPS（ローカル CA）のときは、CA 証明書（/web/ca.crt）とそのフィンガープリント（/web/ca-info）もログインの前に配る
  （公開してよい部分だけ。段階 12-5e1）
- 画面は 1 枚の HTML（ログインと各画面を JavaScript で切り替える）。ログインしていなくても配る（中身は API で読む）
- 応答には次のヘッダを付ける（clamav-webui と同じ方針）
  - すべて: X-Content-Type-Options: nosniff、X-Frame-Options: DENY、Referrer-Policy: same-origin、Cache-Control: no-store
  - HTML: Content-Security-Policy（スクリプト・スタイルは自分のファイルだけ。インラインのスクリプト・onclick= は
    動かない。ファイル名などは textContent で表示するため、仮に混ざっても実行されない）
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, RedirectResponse

from core.local_ca import CA_CERT

#: 画面のファイルの置き場所（配布版は PyInstaller の _internal/web）
WEB_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "web"
#: 配るファイルと Content-Type
FILES = {
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}
#: 配るファイルのパス（要求の名前は、この表を引く鍵にだけ使い、パスの組み立てには使わない）
_FILE_PATHS = {name: WEB_DIR / name for name in FILES}
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


def _local_ca(request: Request):
    """HTTPS（ローカル CA）で待ち受けているときの LocalCa。それ以外は 404（段階 12-5e1）"""
    web = getattr(request.app.state, "web", None)
    ca = getattr(web, "ca", None)
    if request.url.scheme != "https" or ca is None or not ca.path(CA_CERT).is_file():
        from core.exceptions import ApiNotFoundError

        raise ApiNotFoundError()
    return ca


@router.get("/web/ca.crt", include_in_schema=False)
def ca_certificate(request: Request) -> Response:
    """ローカル CA の証明書（公開してよい部分だけ。鍵は配らない）。ログインの前でも取れる。端末に登録してもらうため、
    DER の .crt（Android・Windows・macOS でそのまま開ける形）で返す。本物かはフィンガープリントで照合してもらう"""
    der = _local_ca(request).ca_der()
    return Response(der, media_type="application/x-x509-ca-cert",
                    headers={"Content-Disposition": 'attachment; filename="clamqtpie-ca.crt"'})


@router.get("/web/ca-info", include_in_schema=False)
def ca_info(request: Request) -> dict:
    """ログイン画面に出す CA の SHA-256 のフィンガープリント（トレイGUI・clamqtpie-ctl web tls status と照合してもらう）"""
    return {"fingerprint": _local_ca(request).status().ca_fingerprint}


@router.get("/web/{name}", include_in_schema=False)
def static_file(name: str):
    path = _FILE_PATHS.get(name)
    if path is None:
        from core.exceptions import ApiNotFoundError

        raise ApiNotFoundError()
    return FileResponse(path, media_type=FILES[path.name])


def add_security_headers(response) -> None:
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
