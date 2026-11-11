"""
FastAPI のアプリケーションを組み立てる（サービス化・Web API設計書 4章）

ルーターは /api/v1 の下にまとめる。すべての要求について、接続元・Host・Origin を確かめてから処理する
（api.auth.check_request）。ロールの確認は各ルートの Depends（require_user・require_admin）で行う。
自動で作られる説明のページ（/docs・/redoc・/openapi.json）は、
待ち受けに出す必要がなく、使える API の一覧を外に見せることになるため作らない。
WebUI の待ち受け（段階 12-5a）は、同じルーターで listener="web" のアプリを作る（api/auth.py が認証を切り替える）。
ログイン（/auth/*）と画面のファイル（/web/*。api/web_static.py）は WebUI の待ち受けにだけ付ける。
WebUI の応答にはすべて nosniff などのヘッダを付ける。
"""
from __future__ import annotations

from fastapi import FastAPI, Request

from api.auth import LISTENER_INTERNAL, LISTENER_WEB, ApiTokens, check_request
from api.context import ApiContext
from api.errors import install_error_handlers
from api.routes import info, jobs, notifications, read, write
from api import web_static
from api.routes import web as web_routes
from core.version import APP_NAME, APP_VERSION

API_PREFIX = "/api/v1"


def create_app(tokens: ApiTokens, context: ApiContext | None = None, listener: str = LISTENER_INTERNAL,
               web=None) -> FastAPI:
    """context: API が使うサービス（None なら読み取りの API も付けない。骨組みのテスト用）。
    context.invoker があるときだけ、状態を変える API を付ける。
    listener: "internal"（内部 API、Bearer）か "web"（WebUI、Cookie セッション）。
    web: api.web_control.WebController（None なら context.web。どちらも無ければ WebUI の API は付けない）"""
    app = FastAPI(
        title=f"{APP_NAME} API",
        version=APP_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.tokens = tokens
    app.state.context = context
    app.state.listener = listener
    app.state.web = web if web is not None else getattr(context, "web", None)
    install_error_handlers(app)

    @app.middleware("http")
    async def _check_every_request(request: Request, call_next):
        rejected = check_request(request)
        if rejected is None:
            response = await call_next(request)
        else:
            response = rejected
        if listener == LISTENER_WEB:
            web_static.add_security_headers(response)
        return response

    app.include_router(info.router, prefix=API_PREFIX)
    if context is not None:
        app.include_router(read.router, prefix=API_PREFIX)
        if context.can_write:
            app.include_router(write.router, prefix=API_PREFIX)
            if context.jobs is not None:
                app.include_router(jobs.router, prefix=API_PREFIX)
            if context.notification_store is not None:
                app.include_router(notifications.router, prefix=API_PREFIX)
            if app.state.web is not None:
                app.include_router(web_routes.web_router, prefix=API_PREFIX)
                if listener == LISTENER_WEB:
                    app.include_router(web_routes.auth_router, prefix=API_PREFIX)
                    app.include_router(web_static.router)
    return app
