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
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

from api.errors import error_body
from core.exceptions import ApiAuthError, ApiForbiddenError, ApiRequestRejectedError
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

# Windows のよく知られた SID（SYSTEM・Administrators）
_SID_SYSTEM = "S-1-5-18"
_SID_ADMINISTRATORS = "S-1-5-32-544"


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
        _make_private_dir(self._dir)
        for role, token in self._tokens.items():
            _write_private_file(self.path_for(role), token)

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


def _make_private_dir(path: Path) -> None:
    # リンクに置き換えられていたら、リンク先の権限を変えたり、そこへ書き出したりしない
    if path.is_symlink():
        raise OSError(f"API のトークンのフォルダがリンクになっています: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        _restrict_windows_acl(path)
    else:
        os.chmod(path, 0o700)


def _write_private_file(path: Path, content: str) -> None:
    """本人だけが読めるファイルとして書き出す（中身を書く前に権限を絞り、os.replace で置き換える）"""
    tmp = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        if sys.platform == "win32":
            _restrict_windows_acl(tmp)
        else:
            os.fchmod(fd, 0o600)
        os.write(fd, content.encode("ascii"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _restrict_windows_acl(path: Path) -> None:
    """アクセス権を「本人・SYSTEM・Administrators のフルコントロール」だけにし、親からの継承を切る"""
    import ntsecuritycon
    import win32api
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    owner_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    dacl = win32security.ACL()
    inherit = 0
    if path.is_dir():
        inherit = win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE
    for sid in (
        owner_sid,
        win32security.ConvertStringSidToSid(_SID_SYSTEM),
        win32security.ConvertStringSidToSid(_SID_ADMINISTRATORS),
    ):
        dacl.AddAccessAllowedAceEx(win32security.ACL_REVISION, inherit, ntsecuritycon.FILE_ALL_ACCESS, sid)
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )


# -- 要求ごとの確認 ---------------------------------------------------------------
def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def _role(request: Request) -> str:
    token = _bearer_token(request)
    role = request.app.state.tokens.role_of(token) if token else None
    if role is None:
        raise ApiAuthError()
    return role


def require_user(request: Request) -> str:
    """user 以上のロールを求める（FastAPI の Depends で使う）"""
    return _role(request)


def require_admin(request: Request) -> str:
    """admin のロールを求める（FastAPI の Depends で使う）"""
    if _role(request) != ROLE_ADMIN:
        raise ApiForbiddenError()
    return ROLE_ADMIN


def _is_local_origin(value: str, port: int) -> bool:
    parts = urlsplit(value)
    return parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTNAMES and parts.port == port


def check_request(request: Request) -> JSONResponse | None:
    """内部 API に来た要求の接続元・Host・Origin を確かめる。拒否するときはその応答を返す"""
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


def _reject(request: Request, status: int, reason: str) -> JSONResponse:
    client = request.client.host if request.client else "不明"
    logger.warning(
        f"API の要求を拒否しました: {reason}（接続元: {visible_controls(client)}、"
        f"{request.method} {visible_controls(request.url.path)}）",
        extra={"error_code": ApiRequestRejectedError.default_code},
    )
    error = ApiRequestRejectedError()
    return JSONResponse(error_body(error.code, error.message), status_code=status)
