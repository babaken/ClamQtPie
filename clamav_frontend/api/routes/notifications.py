"""
通知の未読・既読の API（サービス化・Web API設計書 6.10節・7章。段階 12-3c）

user 以上のロールで呼べる（トレイGUI・WebUI のどちらからも使う）。サービスの通知の保存
（core/notification_store.py）がある場合だけ付ける。
- GET /notifications/pending: 未読の通知（新しい順、最大 MAX_PENDING 件）と未読の全体の件数
- POST /notifications/ack: {"ids": [...]} か {"all": true} で既読にし、notification.acked を送る。
  通知を消されたことに後から気づけるよう、既読にした件数・ロール・接続元をログに残す（12章 24）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from api.actions import audit
from api.auth import require_user
from core.logger import CATEGORY_SYSTEM

router = APIRouter(dependencies=[Depends(require_user)])

#: 一度に既読にできる ID の数の上限
_MAX_IDS = 1000


class AckBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ids: list[int] | None = Field(None, max_length=_MAX_IDS)
    all: bool = False


def _store(request: Request):
    return request.app.state.context.notification_store


@router.get("/notifications/pending")
def pending_notifications(request: Request) -> dict:
    items, total = _store(request).pending()
    return {"notifications": items, "total": total}


@router.post("/notifications/ack")
async def ack_notifications(request: Request, body: AckBody, role: str = Depends(require_user)) -> dict:
    if body.all == (body.ids is not None):
        raise ValueError("ids か all のどちらか一方を指定してください")
    acked = await run_in_threadpool(_store(request).ack, None if body.all else body.ids)
    client = request.client.host if request.client else "不明"
    scope = "すべて" if body.all else "指定"
    audit(CATEGORY_SYSTEM, f"通知を既読にしました: {len(acked)}件（{scope}、ロール: {role}、接続元: {client}）")
    return {"acked": acked}
