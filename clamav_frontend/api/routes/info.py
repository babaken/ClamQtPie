"""状態・情報の API（サービス化・Web API設計書 6.1節）"""
from __future__ import annotations

from fastapi import APIRouter

from core.version import APP_VERSION

#: API の版数。パスの /api/v1 と合わせる
API_VERSION = "v1"

router = APIRouter()


@router.get("/version")
def get_version() -> dict:
    """アプリと API の版数。認証なしで呼べ、接続確認にも使う"""
    return {"app_version": APP_VERSION, "api_version": API_VERSION}
