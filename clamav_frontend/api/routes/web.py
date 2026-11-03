"""
WebUI の設定とログインの API（サービス化・Web API設計書 6.11節・3.3節。段階 12-5a）

- GET /web（user）: WebUI の状態。パスワードのハッシュは返さない
- PUT /web・PUT /web/credentials（admin の Bearer だけ）: ON/OFF・自動で OFF にする時刻・ログインの設定。
  WebUI のセッションからは変えられない（403）。処理は api/web_control.py をメインスレッドで呼ぶ
- POST /auth/session（WebUI の待ち受けだけ）: ログイン。成功したら新しいセッションの Cookie と、セッションに
  結び付けた CSRF トークン（JSON）を返す。失敗はどちらが違ったかを区別しない。同じ接続元から 5 回続けて失敗したら
  5 分間 429（E8106）
- DELETE /auth/session: ログアウト。POST /auth/reauth: admin の操作の前の再確認（パスワード。二段階認証は 12-5d）
- ログイン・ログアウト・失敗・ロック・再確認を、接続元付きでログに残す
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from api.auth import SESSION_COOKIE, require_bearer_admin, require_user, require_web_session
from core.exceptions import ApiAuthError, ApiLoginLockedError, ApiReauthRequiredError
from core.logger import CATEGORY_SYSTEM, log_event
from core.text_safety import visible_controls
from core.web_password import usernames_match, verify_password

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


class LoginBody(_Body):
    username: str = Field(max_length=64)
    password: str = Field(max_length=1024)
    #: 二段階認証のコード（段階 12-5d。それまでは受け取っても使わない）
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


# -- 3.3 ログイン（WebUI の待ち受けだけ） -------------------------------------------------------------
def _check_login(entered_user: str, password: str, api) -> bool:
    # ユーザー名が違っても、パスワードの照合（scrypt）を必ず行う（どちらが違ったかを時間で見分けさせない）
    user_ok = usernames_match(entered_user, api.web_username)
    password_ok = verify_password(password, api.web_password_hash)
    return user_ok and password_ok


def _raise_if_locked(request: Request) -> None:
    remaining = _web(request).guard.locked_seconds(_client(request))
    if remaining:
        raise ApiLoginLockedError(detail=f"約 {max(1, remaining // 60)} 分後にやり直してください")


def _count_failure(request: Request, what: str) -> None:
    client = _client(request)
    locked = _web(request).guard.failure(client)
    logger.warning(f"WebUI の{what}に失敗しました（接続元: {visible_controls(client)}）",
                   extra={"error_code": ApiAuthError.default_code})
    if locked:
        logger.warning(f"WebUI のログインの失敗が続いたため、この接続元を 5 分間ロックしました（接続元: "
                       f"{visible_controls(client)}）", extra={"error_code": ApiLoginLockedError.default_code})


@auth_router.post("/auth/session")
async def login(request: Request, body: LoginBody) -> JSONResponse:
    _raise_if_locked(request)
    api = _ctx(request).config_service.current.api
    ok = await run_in_threadpool(_check_login, body.username, body.password, api)  # scrypt は重いため
    if not ok:
        _count_failure(request, "ログイン")
        raise ApiAuthError("ユーザー名またはパスワードが違います")
    web = _web(request)
    client = _client(request)
    web.guard.success(client)
    web.sessions.destroy(request.cookies.get(SESSION_COOKIE))  # 付いていた古いセッションは使わない
    session = web.sessions.create(client)
    log_event(CATEGORY_SYSTEM, f"WebUI にログインしました（ユーザー名: {api.web_username}、接続元: {client}）")
    response = JSONResponse({
        "csrf_token": session.csrf_token,
        "username": api.web_username,
        "off_at": api.web_off_at or None,
        "session_timeout_min": api.session_timeout_min,
    })
    response.set_cookie(SESSION_COOKIE, session.id, httponly=True, samesite="strict", path="/")
    return response


@auth_router.delete("/auth/session")
def logout(request: Request, session=Depends(require_web_session)) -> Response:
    _web(request).sessions.destroy(session.id)
    log_event(CATEGORY_SYSTEM, f"WebUI からログアウトしました（接続元: {_client(request)}）")
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return response


@auth_router.post("/auth/reauth")
async def reauth(request: Request, body: ReauthBody, session=Depends(require_web_session)) -> dict:
    _raise_if_locked(request)
    api = _ctx(request).config_service.current.api
    ok = await run_in_threadpool(verify_password, body.password or "", api.web_password_hash)
    web = _web(request)
    if not ok:
        _count_failure(request, "再確認")
        if web.guard.locked_seconds(_client(request)):
            web.sessions.destroy(session.id)  # ロックされたらセッションも破棄する（3.3節）
        raise ApiReauthRequiredError("パスワードが違います")
    web.guard.success(_client(request))
    web.sessions.mark_reauthenticated(session)
    log_event(CATEGORY_SYSTEM, f"WebUI で管理者の操作の前の再確認をしました（接続元: {_client(request)}）")
    return {"reauth_sec": 300}
