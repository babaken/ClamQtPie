"""
FastAPI のアプリケーションを組み立てる（サービス化・Web API設計書 4章）

ルーターは /api/v1 の下にまとめる。すべての要求について、接続元・Host・Origin を確かめてから処理する
（api.auth.check_request）。ロールの確認は各ルートの Depends（require_user・require_admin）で行う。
自動で作られる説明のページ（/docs・/redoc・/openapi.json）は、
待ち受けに出す必要がなく、使える API の一覧を外に見せることになるため作らない。
"""
from __future__ import annotations

from fastapi import FastAPI, Request

from api.auth import ApiTokens, check_request
from api.context import ApiContext
from api.errors import install_error_handlers
from api.routes import info, read, write
from core.version import APP_NAME, APP_VERSION

API_PREFIX = "/api/v1"


def create_app(tokens: ApiTokens, context: ApiContext | None = None) -> FastAPI:
    """context: API が使うサービス（None なら読み取りの API も付けない。骨組みのテスト用）。
    context.invoker があるときだけ、状態を変える API を付ける"""
    app = FastAPI(
        title=f"{APP_NAME} API",
        version=APP_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.tokens = tokens
    app.state.context = context
    install_error_handlers(app)

    @app.middleware("http")
    async def _check_every_request(request: Request, call_next):
        rejected = check_request(request)
        if rejected is not None:
            return rejected
        return await call_next(request)

    app.include_router(info.router, prefix=API_PREFIX)
    if context is not None:
        app.include_router(read.router, prefix=API_PREFIX)
        if context.can_write:
            app.include_router(write.router, prefix=API_PREFIX)
    return app
