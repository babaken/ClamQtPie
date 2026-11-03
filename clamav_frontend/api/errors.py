"""
API のエラー応答（サービス化・Web API設計書 4.2節）

AppError を {"error": {"code", "message", "detail"}} の形で返す。HTTP の状態コードは例外の種類で決める。
500 になるものは、原因の手がかり（detail・例外の文字列）を応答に含めず、ログにだけ残す
（ファイルのパスや内部の状態を、API の利用者に見せないため）。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.exceptions import (
    ApiAuthError,
    ApiConflictError,
    ApiError,
    ApiForbiddenError,
    ApiLoginLockedError,
    ApiNotFoundError,
    ApiReauthRequiredError,
    ApiRequestRejectedError,
    AppError,
    ClamdError,
    ConfigValidationError,
    JobAlreadyRunningError,
    JobValidationError,
    QuarantineNotFoundError,
    QuarantineRestoreConflictError,
    UnsafeExecutablePathError,
)

logger = logging.getLogger("clamav_frontend")

#: 例外の種類と HTTP の状態コード（上から順に照合し、最初に当てはまったものを使う）
_STATUS_BY_ERROR: tuple[tuple[type[AppError], int], ...] = (
    (ApiAuthError, 401),
    (ApiForbiddenError, 403),
    (ApiRequestRejectedError, 403),
    (ApiReauthRequiredError, 403),
    (ApiConflictError, 409),
    (JobAlreadyRunningError, 409),
    (ApiLoginLockedError, 429),
    (QuarantineNotFoundError, 404),
    (ApiNotFoundError, 404),
    (QuarantineRestoreConflictError, 400),
    (JobValidationError, 400),
    (ConfigValidationError, 400),
    (UnsafeExecutablePathError, 400),
    (ClamdError, 503),
)

#: 入力の検証に失敗したとき（FastAPI の RequestValidationError・ValueError）のコードとメッセージ
_VALIDATION_CODE = ApiError.default_code
_VALIDATION_MESSAGE = "リクエストの内容が正しくありません"


def status_for(exc: AppError) -> int:
    for error_type, status in _STATUS_BY_ERROR:
        if isinstance(exc, error_type):
            return status
    return 500


def error_body(code: str, message: str, detail: str | None = None) -> dict:
    return {"error": {"code": code, "message": message, "detail": detail}}


def _app_error_response(exc: AppError) -> JSONResponse:
    status = status_for(exc)
    if status >= 500:
        logger.error(f"API の処理でエラーが発生しました: {exc.message} (detail: {exc.detail})",
                     extra={"error_code": exc.code})
        return JSONResponse(error_body(exc.code, exc.message), status_code=status)
    # 401 では認証の方式を示す（RFC 6750）
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse(error_body(exc.code, exc.message, exc.detail), status_code=status, headers=headers)


async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    if isinstance(exc, ApiForbiddenError):
        # 断った要求を残す（トレイGUI のエラーのダイアログの「詳細を見る」で、ログからこのコードを探せるように）
        logger.warning(f"権限が足りないため断りました: {request.method} {request.url.path}（{exc.detail or exc.message}）",
                       extra={"error_code": exc.code})
    return _app_error_response(exc)


async def _handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    # どの項目が正しくないかだけを返す（入力された値そのものは返さない）
    fields = sorted({".".join(str(part) for part in err.get("loc", ())) for err in exc.errors()})
    return JSONResponse(error_body(_VALIDATION_CODE, _VALIDATION_MESSAGE, ", ".join(fields) or None),
                        status_code=400)


async def _handle_value_error(_request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(error_body(_VALIDATION_CODE, _VALIDATION_MESSAGE, str(exc)), status_code=400)


#: FastAPI・Starlette が自分で返すエラー（存在しないパスなど）の表示用メッセージ
_HTTP_MESSAGES = {
    404: "対象が見つかりません",
    405: "このメソッドは使えません",
}


async def _handle_http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # 存在しないパス（404）・許可しないメソッド（405）なども、同じ形で返す
    message = _HTTP_MESSAGES.get(exc.status_code, ApiError.default_message)
    return JSONResponse(error_body(ApiError.default_code, message), status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))


async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
    logger.exception("API の処理で予期しない例外が発生しました", extra={"error_code": AppError.default_code})
    return JSONResponse(error_body(AppError.default_code, AppError.default_message), status_code=500)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(ValueError, _handle_value_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_error)
    app.add_exception_handler(Exception, _handle_unexpected)
