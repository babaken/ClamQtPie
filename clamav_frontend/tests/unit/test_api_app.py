"""Web API の骨組み（エラー応答・版数）のテスト（サービス化・Web API設計書 4.2節・6.1節）"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import API_PREFIX, create_app
from api.auth import ApiTokens
from core.exceptions import (
    ApiAuthError,
    ApiConflictError,
    ApiForbiddenError,
    ApiLoginLockedError,
    ApiReauthRequiredError,
    ApiRequestRejectedError,
    AppError,
    ClamdConnectionError,
    ConfigValidationError,
    JobAlreadyRunningError,
    QuarantineNotFoundError,
    QuarantineRestoreError,
)
from core.version import APP_VERSION


def local_client(app, **kwargs) -> TestClient:
    """このPC（127.0.0.1）から内部 API のポートに接続したときと同じ要求を送るクライアント"""
    return TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000), **kwargs)


def _app():
    return create_app(ApiTokens(Path("unused-token-dir")))


def _client_with(route_func) -> TestClient:
    """例外を送出する確認用の API を足したクライアント（500 も応答として受け取る）"""
    app = _app()
    app.add_api_route(f"{API_PREFIX}/_test", route_func, methods=["GET"])
    return local_client(app, raise_server_exceptions=False)


def test_version_returns_app_and_api_version():
    response = local_client(_app()).get(f"{API_PREFIX}/version")
    assert response.status_code == 200
    assert response.json() == {"app_version": APP_VERSION, "api_version": "v1"}


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (ApiAuthError(), 401),
        (ApiForbiddenError(), 403),
        (ApiRequestRejectedError(), 403),
        (ApiReauthRequiredError(), 403),
        (ApiConflictError(), 409),
        (JobAlreadyRunningError(), 409),
        (ApiLoginLockedError(), 429),
        (QuarantineNotFoundError(), 404),
        (ConfigValidationError(), 400),
        (ClamdConnectionError(), 503),
    ],
)
def test_app_error_is_returned_with_its_code_and_status(error, status):
    def raise_error():
        raise error

    response = _client_with(raise_error).get(f"{API_PREFIX}/_test")
    assert response.status_code == status
    assert response.json()["error"]["code"] == error.code
    assert response.json()["error"]["message"] == error.message


def test_client_error_keeps_detail():
    def raise_error():
        raise QuarantineNotFoundError(detail="id=3")

    assert _client_with(raise_error).get(f"{API_PREFIX}/_test").json()["error"]["detail"] == "id=3"


def test_server_error_does_not_return_detail():
    # 500 になるエラーは、手がかり（ファイルのパスなど）を応答に含めない
    def raise_error():
        raise QuarantineRestoreError(detail=r"C:\Users\a\secret.exe")

    response = _client_with(raise_error).get(f"{API_PREFIX}/_test")
    assert response.status_code == 500
    assert response.json()["error"] == {"code": "E3002", "message": QuarantineRestoreError.default_message,
                                        "detail": None}
    assert "secret" not in response.text


def test_unexpected_exception_is_returned_as_e9001_without_its_text():
    def raise_error():
        raise RuntimeError("内部の状態 /var/lib/secret")

    response = _client_with(raise_error).get(f"{API_PREFIX}/_test")
    assert response.status_code == 500
    assert response.json()["error"] == {"code": "E9001", "message": AppError.default_message, "detail": None}
    assert "secret" not in response.text


def test_invalid_input_is_400_and_does_not_echo_the_value():
    def take_number(limit: int):
        return {"limit": limit}

    response = _client_with(take_number).get(f"{API_PREFIX}/_test", params={"limit": "<script>"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "E8100"
    assert "<script>" not in response.text


def test_unknown_path_and_method_use_the_same_error_format():
    client = local_client(_app())
    not_found = client.get(f"{API_PREFIX}/no-such-path")
    assert not_found.status_code == 404
    assert not_found.json()["error"]["code"] == "E8100"
    assert client.post(f"{API_PREFIX}/version").status_code == 405


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_description_pages_are_not_served(path):
    # 使える API の一覧を外に見せない
    assert local_client(_app()).get(path).status_code == 404
