"""
Web API の認証（サービス化・Web API設計書 3.1節・3.2節）

- ロールは user（閲覧・手動スキャンなど）と admin（設定の変更など）の2つ。Bearer トークンで見分ける
- トークンは待ち受けを始めるたびに 32 バイトの乱数から作り直し、比較は hmac.compare_digest で行う
- トークンファイルは、作った本人（Windows は加えて SYSTEM・Administrators）だけが読めるようにし、
  待ち受けを止めたら消す。段階 12-3 まではアプリのフォルダの api_tokens/ に置く
  （サービス化の後は %ProgramData%\\ClamQtPie\\api\\・/var/lib/clamqtpie/api/ に移る）
- 内部 API への要求は、接続元が 127.0.0.1 であること、Host が 127.0.0.1／localhost とこのポートであること、
  状態を変えるメソッドでは Origin・Referer が付いていればこのPC のものであることを確かめる
  （ブラウザに読み込ませたページから、このPC の API を呼ばせる手口を防ぐ）
- 待ち受けは 2 つ（3.1節。段階 12-5a）。同じルーターを使い、どちらの待ち受けかで受け付ける認証を切り替える
  - 内部 API（app.state.listener == "internal"）: Bearer トークンだけ（Cookie セッションは 401）
  - WebUI（"web"）: Cookie セッションだけ（Bearer トークンは 401）。セッションの権限は admin。状態を変える要求は
    セッションに結び付けた CSRF トークン（X-CSRF-Token）と Origin を確かめる。admin の操作（require_admin）は、
    再確認（POST /auth/reauth）から 5 分以内のセッションだけ（E8108）。PUT /web・PUT /web/credentials などは
    Bearer だけ（require_bearer_admin。WebUI からは 403）
  - WebUI の接続元はループバックだけ。LAN からの接続を許可して待ち受けているとき（web.scope。段階 12-5e2）は、
    LAN のサブネットからも受け付け、Host にこのPC の名前・LAN のアドレスも使える
"""
from __future__ import annotations

import hmac
import logging
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

from api.errors import error_body
from core.exceptions import ApiAuthError, ApiForbiddenError, ApiReauthRequiredError, ApiRequestRejectedError
from core.private_files import make_private_dir, write_private_file
from core.text_safety import visible_controls

logger = logging.getLogger("clamav_frontend")

ROLE_USER = "user"
ROLE_ADMIN = "admin"
_ROLES = (ROLE_USER, ROLE_ADMIN)

#: トークンファイルを置くフォルダ名（アプリのフォルダの下）
TOKEN_DIR_NAME = "api_tokens"

_LOOPBACK_ADDRESSES = ("127.0.0.1", "::1")
_LOOPBACK_HOSTNAMES = ("127.0.0.1", "localhost")
_STATE_CHANGING_METHODS = ("POST", "PUT", "PATCH", "DELETE")

#: 待ち受けの種類（app.state.listener）
LISTENER_INTERNAL = "internal"
LISTENER_WEB = "web"
#: WebUI のセッションの Cookie と、CSRF トークンを送るヘッダ。Cookie はポートで分かれないため、このPC で動く
#: ほかのアプリ（127.0.0.1 の別のポート）の Cookie と名前が重ならないよう、アプリの名前を付ける
SESSION_COOKIE = "clamqtpie_session"
#: HTTPS のときの Cookie の名前（段階 12-5e1）。__Host- を付けると、ブラウザは Secure・Path=/・Domain なしのときだけ
#: 受け付ける（HTTP のページや別の名前から上書きさせない）
SECURE_SESSION_COOKIE = "__Host-clamqtpie_session"
CSRF_HEADER = "x-csrf-token"


class ApiTokens:
    """user・admin のトークンを作り、ファイルに書き出し、照合する"""

    def __init__(self, token_dir: Path) -> None:
        self._dir = Path(token_dir)
        self._tokens = {role: secrets.token_urlsafe(32) for role in _ROLES}

    @property
    def directory(self) -> Path:
        return self._dir

    def path_for(self, role: str) -> Path:
        return self._dir / f"{role}.token"

    def token_for(self, role: str) -> str:
        return self._tokens[role]

    def write_files(self) -> None:
        """トークンファイルを書き出す。前回の起動のファイルは上書きする"""
        make_private_dir(self._dir)
        for role, token in self._tokens.items():
            write_private_file(self.path_for(role), token.encode("ascii"))

    def remove_files(self) -> None:
        for role in _ROLES:
            try:
                self.path_for(role).unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning(f"API のトークンファイルを削除できませんでした (detail: {exc})",
                               extra={"error_code": "E8100"})

    def role_of(self, token: str) -> str | None:
        """トークンのロール。どれとも一致しなければ None（すべてのロールと比べ、一致の位置で時間が変わらないようにする）"""
        matched = None
        for role in _ROLES:
            if hmac.compare_digest(token.encode("utf-8"), self._tokens[role].encode("utf-8")):
                matched = role
        return matched


# -- 要求ごとの確認 ---------------------------------------------------------------
def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def listener_of(request: Request) -> str:
    return getattr(request.app.state, "listener", LISTENER_INTERNAL)


def is_web_request(request: Request) -> bool:
    return listener_of(request) == LISTENER_WEB


def session_cookie_name(request: Request) -> str:
    """この要求で使うセッションの Cookie の名前（HTTPS かどうかで分ける。uvicorn の proxy_headers は無効なので、
    scheme は実際の接続のもの）"""
    return SECURE_SESSION_COOKIE if request.url.scheme == "https" else SESSION_COOKIE


def web_session(request: Request):
    """WebUI の要求のセッション（api.web_sessions.WebSession）。無い・期限切れなら 401。状態を変える要求は
    CSRF トークンも確かめる（違えば 403）"""
    cached = getattr(request.state, "web_session", None)
    if cached is not None:
        return cached
    if request.headers.get("authorization"):
        raise ApiAuthError("WebUI ではトークンでの認証は使えません")
    session = request.app.state.web.sessions.get(request.cookies.get(session_cookie_name(request)))
    if session is None:
        raise ApiAuthError("ログインしてください")
    if request.method in _STATE_CHANGING_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not hmac.compare_digest(sent.encode("utf-8"), session.csrf_token.encode("utf-8")):
            _log_rejected(request, "CSRF トークンが一致しません")
            raise ApiRequestRejectedError()
    request.state.web_session = session
    return session


def _role(request: Request) -> str:
    if is_web_request(request):
        web_session(request)
        return ROLE_ADMIN  # WebUI のセッションの権限は常に admin（3.3節）
    token = _bearer_token(request)
    role = request.app.state.tokens.role_of(token) if token else None
    if role is None:
        raise ApiAuthError()
    return role


def require_user(request: Request) -> str:
    """user 以上のロールを求める（FastAPI の Depends で使う）"""
    return _role(request)


def require_admin(request: Request) -> str:
    """admin のロールを求める（FastAPI の Depends で使う）。WebUI のセッションは再確認から 5 分以内であること"""
    if _role(request) != ROLE_ADMIN:
        raise ApiForbiddenError()
    if is_web_request(request) and not request.app.state.web.sessions.is_reauthenticated(web_session(request)):
        raise ApiReauthRequiredError()
    return ROLE_ADMIN


def require_bearer_admin(request: Request) -> str:
    """admin の Bearer トークンだけを受け付ける（WebUI の ON/OFF・ログインの設定。WebUI からは変えさせない）"""
    if is_web_request(request):
        raise ApiForbiddenError("この操作は WebUI からはできません（トレイGUI か clamqtpie-ctl で行ってください）")
    return require_admin(request)


def require_web_session(request: Request):
    """WebUI のセッションを求める（ログアウト・再確認）"""
    if not is_web_request(request):
        raise ApiAuthError()
    return web_session(request)


def _is_local_origin(value: str, port: int) -> bool:
    parts = urlsplit(value)
    return parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTNAMES and parts.port == port


def check_request(request: Request) -> JSONResponse | None:
    """要求の接続元・Host・Origin を確かめる。拒否するときはその応答を返す"""
    if is_web_request(request):
        return _check_web_request(request)
    client = request.client.host if request.client else ""
    if client not in _LOOPBACK_ADDRESSES:
        return _reject(request, 403, "接続元がこのPC ではありません")

    server_port = request.scope.get("server", (None, None))[1]
    allowed_hosts = {f"{name}:{server_port}" for name in _LOOPBACK_HOSTNAMES}
    if request.headers.get("host", "").lower() not in allowed_hosts:
        return _reject(request, 400, "Host が正しくありません")

    if request.method in _STATE_CHANGING_METHODS:
        for header in ("origin", "referer"):
            value = request.headers.get(header)
            if value is not None and not _is_local_origin(value, server_port):
                return _reject(request, 403, f"{header} がこのPC のものではありません")
    return None


def _check_web_request(request: Request) -> JSONResponse | None:
    """WebUI に来た要求。接続元はループバック（LAN からの接続を許可して待ち受けているときは LAN のサブネットも）。
    状態を変える要求は Origin（無ければ Referer）が必ず付いていて、Host と同じであること（ブラウザは fetch の POST などに
    Origin を付ける）"""
    client = request.client.host if request.client else ""
    scope = getattr(getattr(request.app.state, "web", None), "scope", None)
    if client not in _LOOPBACK_ADDRESSES and not (scope is not None and scope.allows_client(client)):
        return _reject(request, 403, "接続元がこのPC・LAN ではありません" if scope else "接続元がこのPC ではありません")
    server_port = request.scope.get("server", (None, None))[1]
    host = request.headers.get("host", "").lower()
    if scope is not None:
        allowed = scope.allowed_hosts(server_port)
    else:
        allowed = {f"{name}:{server_port}" for name in _LOOPBACK_HOSTNAMES}
    if host not in allowed:
        return _reject(request, 400, "Host が正しくありません")
    if request.method in _STATE_CHANGING_METHODS:
        origin = request.headers.get("origin")
        if origin is None:
            referer = request.headers.get("referer")
            parts = urlsplit(referer) if referer else None
            origin = f"{parts.scheme}://{parts.netloc}" if parts and parts.scheme else None
        # scheme も同じであること（HTTPS の WebUI に HTTP のページから送らせない）
        if origin is None or origin.lower() != f"{request.url.scheme}://{host}":
            return _reject(request, 403, "Origin がこの WebUI のものではありません")
    return None


def _log_rejected(request: Request, reason: str) -> None:
    client = request.client.host if request.client else "不明"
    logger.warning(
        f"API の要求を拒否しました: {reason}（接続元: {visible_controls(client)}、"
        f"{request.method} {visible_controls(request.url.path)}）",
        extra={"error_code": ApiRequestRejectedError.default_code},
    )


def _reject(request: Request, status: int, reason: str) -> JSONResponse:
    _log_rejected(request, reason)
    error = ApiRequestRejectedError()
    return JSONResponse(error_body(error.code, error.message), status_code=status)
