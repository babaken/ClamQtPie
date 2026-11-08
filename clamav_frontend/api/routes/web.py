"""
WebUI の設定とログインの API（サービス化・Web API設計書 6.11節・3.3節。段階 12-5a）

- GET /web（user）: WebUI の状態。パスワードのハッシュは返さない
- PUT /web・PUT /web/credentials（admin の Bearer だけ）: ON/OFF・自動で OFF にする時刻・ログインの設定。
  WebUI のセッションからは変えられない（403）。処理は api/web_control.py をメインスレッドで呼ぶ
- POST /auth/session（WebUI の待ち受けだけ）: ログイン。成功したら新しいセッションの Cookie と、セッションに
  結び付けた CSRF トークン（JSON）を返す。失敗はどちらが違ったかを区別しない。同じ接続元から 5 回続けて失敗したら
  5 分間 429（E8106）
- DELETE /auth/session: ログアウト。POST /auth/reauth: admin の操作の前の再確認（二段階認証を設定していれば確認コード、
  していなければパスワード）
- POST /web/totp・DELETE /web/totp（admin の Bearer だけ）: 二段階認証の設定・解除（段階 12-5d）。設定していれば、
  ログインにはパスワードに加えて確認コード（予備のコードも可）が要る。一度受け付けたコードは二度と受け付けない
- PUT /web/tls・POST /web/tls/reset-ca・POST /web/tls/renew（admin の Bearer だけ）: HTTPS の方式、ローカル CA の作り直し
  （段階 12-5e1）、サーバー証明書だけの作り直し（段階 12-5e2）。
  HTTPS のときは、セッションの Cookie を __Host-clamqtpie_session（Secure）にする
- ログイン・ログアウト・失敗・ロック・再確認を、接続元付きでログに残す
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from api.auth import require_bearer_admin, require_user, require_web_session, session_cookie_name
from core.exceptions import ApiAuthError, ApiLoginLockedError, ApiReauthRequiredError
from core.logger import CATEGORY_SYSTEM, log_event
from core.text_safety import visible_controls
from core.web_password import usernames_match, verify_password
from api.web_control import login_stopped
from api.web_sessions import GLOBAL_MAX_FAILURES
from core.web_lan import is_loopback, session_timeout_min

logger = logging.getLogger("clamav_frontend")

web_router = APIRouter()
auth_router = APIRouter()


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class WebBody(_Body):
    enabled: bool | None = None
    lan: bool | None = None
    off_after_min: int | None = None
    off_at: str | None = Field(None, max_length=64)


class CredentialsBody(_Body):
    username: str = Field(max_length=64)
    password: str = Field(max_length=1024)


class TotpBody(_Body):
    secret: str = Field(max_length=128)
    code: str = Field(max_length=32)


class TlsBody(_Body):
    mode: str = Field(max_length=16)


class LoginBody(_Body):
    username: str = Field(max_length=64)
    password: str = Field(max_length=1024)
    #: 二段階認証のコード（設定していなければ使わない。予備のコードも可）
    code: str | None = Field(None, max_length=32)


class ReauthBody(_Body):
    password: str | None = Field(None, max_length=1024)
    code: str | None = Field(None, max_length=32)


def _ctx(request: Request):
    return request.app.state.context


def _web(request: Request):
    return _ctx(request).web


def _client(request: Request) -> str:
    return request.client.host if request.client else "不明"


# -- 6.11 WebUI の設定 ------------------------------------------------------------------------------
@web_router.get("/web", dependencies=[Depends(require_user)])
def web_status(request: Request) -> dict:
    return _web(request).status()


@web_router.put("/web", dependencies=[Depends(require_bearer_admin)])
async def web_update(request: Request, body: WebBody) -> dict:
    values = {name: getattr(body, name) for name in body.model_fields_set}
    web = _web(request)
    return await _ctx(request).invoker.run(web.update, values)


@web_router.put("/web/credentials", dependencies=[Depends(require_bearer_admin)])
async def web_credentials(request: Request, body: CredentialsBody) -> dict:
    web = _web(request)
    return await _ctx(request).invoker.run(web.set_credentials, body.username, body.password)


@web_router.post("/web/totp", dependencies=[Depends(require_bearer_admin)])
async def web_totp_enable(request: Request, body: TotpBody) -> dict:
    web = _web(request)
    return await _ctx(request).invoker.run(web.enable_totp, body.secret, body.code)


@web_router.post("/web/unlock", dependencies=[Depends(require_bearer_admin)])
async def web_unlock(request: Request) -> dict:
    """確認コードの間違いが続いて止めたログインを解く"""
    return await _ctx(request).invoker.run(_web(request).unlock)


@web_router.put("/web/tls", dependencies=[Depends(require_bearer_admin)])
async def web_tls(request: Request, body: TlsBody) -> dict:
    """HTTPS の方式（off・local_ca。段階 12-5e1）"""
    return await _ctx(request).invoker.run(_web(request).set_tls, body.mode)


@web_router.post("/web/tls/renew", dependencies=[Depends(require_bearer_admin)])
async def web_tls_renew(request: Request) -> dict:
    """サーバー証明書だけを作り直す（CA はそのまま。端末に残った証明書の例外を使わせなくする）"""
    return await _ctx(request).invoker.run(_web(request).renew_server)


@web_router.post("/web/tls/reset-ca", dependencies=[Depends(require_bearer_admin)])
async def web_tls_reset_ca(request: Request) -> dict:
    """ローカル CA を作り直す（全端末で登録のやり直しが要る）"""
    return await _ctx(request).invoker.run(_web(request).reset_ca)


@web_router.delete("/web/totp", dependencies=[Depends(require_bearer_admin)])
async def web_totp_disable(request: Request) -> dict:
    web = _web(request)
    return await _ctx(request).invoker.run(web.disable_totp)


# -- 3.3 ログイン（WebUI の待ち受けだけ） -------------------------------------------------------------
def _check_login(entered_user: str, password: str, api) -> bool:
    # ユーザー名が違っても、パスワードの照合（scrypt）を必ず行う（どちらが違ったかを時間で見分けさせない）
    user_ok = usernames_match(entered_user, api.web_username)
    password_ok = verify_password(password, api.web_password_hash)
    return user_ok and password_ok


def _raise_if_locked(request: Request) -> None:
    # 残りの時間は書かない（確認コードの間違いによる停止と、応答を同じにするため）
    client = _client(request)
    guard = _web(request).guard
    if guard.locked_seconds(client) or (not is_loopback(client) and guard.global_locked_seconds()):
        raise ApiLoginLockedError()


async def _count_failure(request: Request, what: str) -> None:
    client = _client(request)
    web = _web(request)
    locked = web.guard.failure(client)
    logger.warning(f"WebUI の{what}に失敗しました（接続元: {visible_controls(client)}）",
                   extra={"error_code": ApiAuthError.default_code})
    if locked:
        logger.warning(f"WebUI のログインの失敗が続いたため、この接続元を 5 分間ロックしました（接続元: "
                       f"{visible_controls(client)}）", extra={"error_code": ApiLoginLockedError.default_code})
    if not is_loopback(client) and web.guard.lan_failure():
        logger.error(f"LAN からの WebUI のログインの失敗が 1 時間に {GLOBAL_MAX_FAILURES} 回になったため、LAN からの"
                     "ログインを 15 分間止めました", extra={"error_code": ApiLoginLockedError.default_code})
        await _ctx(request).invoker.run(web.notify_global_lock)


async def _second_factor(request: Request, code):
    """二段階認証のコードを確かめ、合えば使ったことにする（同じコードを二度と受け付けない）。合えば "totp" か
    "recovery"、合わなければ None"""
    web = _web(request)
    factor = await run_in_threadpool(web.check_second_factor, code or "")
    if factor is None or not await _ctx(request).invoker.run(web.consume_second_factor, factor):
        return None
    return factor[0]


def _recovery_notice(request: Request, kind) -> dict:
    """予備のコードを使ったら、画面で知らせるための残りの数"""
    if kind != "recovery":
        return {}
    return {"recovery_used": True,
            "recovery_codes_left": len(_ctx(request).config_service.current.api.web_recovery_code_hashes)}


@auth_router.post("/auth/session")
async def login(request: Request, body: LoginBody) -> JSONResponse:
    _raise_if_locked(request)
    api = _ctx(request).config_service.current.api
    web = _web(request)
    ok = await run_in_threadpool(_check_login, body.username, body.password, api)  # scrypt は重いため
    used = None
    if api.web_totp_enabled:
        # パスワードが違っても確認コードの照合を行う（どちらが違ったかを時間で見分けさせない）。
        # 使ったことにするのは、パスワードも合ったときだけ
        stopped = login_stopped(api)
        factor = await run_in_threadpool(web.check_second_factor, body.code or "")
        if ok and factor is None:
            await _ctx(request).invoker.run(web.record_code_failure, _client(request))
        if stopped and not (ok and factor is not None and factor[0] == "recovery"):
            # 止めている間は、パスワード・確認コードの正否によらず、接続元ごとのロックと同じ応答にする
            # （止まったことから、パスワードが合っていたと見分けさせない）。予備のコードだけは受け付ける
            await _count_failure(request, "ログイン")
            raise ApiLoginLockedError()
        ok = ok and factor is not None and await _ctx(request).invoker.run(web.consume_second_factor, factor)
        used = factor[0] if ok else None
    if not ok:
        await _count_failure(request, "ログイン")
        if api.web_totp_enabled:
            raise ApiAuthError("ユーザー名・パスワード・確認コードのどれかが違います")
        raise ApiAuthError("ユーザー名またはパスワードが違います")
    client = _client(request)
    web.guard.success(client)
    cookie = session_cookie_name(request)
    web.sessions.destroy(request.cookies.get(cookie))  # 付いていた古いセッションは使わない
    session = web.sessions.create(client)
    log_event(CATEGORY_SYSTEM, f"WebUI にログインしました（ユーザー名: {api.web_username}、接続元: {client}）")
    response = JSONResponse({
        "csrf_token": session.csrf_token,
        "username": api.web_username,
        "off_at": api.web_off_at or None,
        "session_timeout_min": session_timeout_min(api),
        "totp": bool(api.web_totp_enabled),
        "lan": web.scope is not None,
        **_recovery_notice(request, used),
    })
    response.set_cookie(cookie, session.id, httponly=True, samesite="strict", path="/",
                        secure=request.url.scheme == "https")
    return response


@auth_router.get("/auth/session")
def current_session(request: Request, session=Depends(require_web_session)) -> dict:
    """画面を読み直したとき（CSRF トークンは画面のメモリにしか持たないため）。Cookie のセッションが有効なら、
    そのセッションの CSRF トークンを返す。別のサイトからは読めない（CORS を許可しない。Cookie は SameSite=Strict）。
    reauthenticated は再確認から 5 分以内か（画面が、パスワードを聞かない場合にも変更の一覧を見せるため）"""
    api = _ctx(request).config_service.current.api
    return {"csrf_token": session.csrf_token, "username": api.web_username, "off_at": api.web_off_at or None,
            "session_timeout_min": session_timeout_min(api),
            "reauthenticated": _web(request).sessions.is_reauthenticated(session),
            "totp": bool(api.web_totp_enabled), "lan": _web(request).scope is not None}


@auth_router.delete("/auth/session")
def logout(request: Request, session=Depends(require_web_session)) -> Response:
    _web(request).sessions.destroy(session.id)
    log_event(CATEGORY_SYSTEM, f"WebUI からログアウトしました（接続元: {_client(request)}）")
    response = Response(status_code=204)
    https = request.url.scheme == "https"
    response.delete_cookie(session_cookie_name(request), path="/", httponly=True, samesite="strict", secure=https)
    return response


@auth_router.post("/auth/reauth")
async def reauth(request: Request, body: ReauthBody, session=Depends(require_web_session)) -> dict:
    _raise_if_locked(request)
    api = _ctx(request).config_service.current.api
    web = _web(request)
    used = None
    if api.web_totp_enabled:
        # 二段階認証を設定していれば確認コードで確かめる（パスワードは使わない。3.3節）。予備のコードも受け付ける
        used = await _second_factor(request, body.code)
        ok = used is not None
    else:
        ok = await run_in_threadpool(verify_password, body.password or "", api.web_password_hash)
    if not ok:
        await _count_failure(request, "再確認")
        if web.guard.locked_seconds(_client(request)):
            web.sessions.destroy(session.id)  # ロックされたらセッションも破棄する（3.3節）
        raise ApiReauthRequiredError("確認コードが違います" if api.web_totp_enabled else "パスワードが違います")
    web.guard.success(_client(request))
    web.sessions.mark_reauthenticated(session)
    log_event(CATEGORY_SYSTEM, f"WebUI で管理者の操作の前の再確認をしました（接続元: {_client(request)}）")
    return {"reauth_sec": 300, **_recovery_notice(request, used)}
