"""
ジョブ型の API とイベント配信（サービス化・Web API設計書 4.3節・5章・6.2節・6.7節。段階 12-2b）

すべて user 以上のロールで呼べる（スキャン・定義の更新は、画面のボタンと同じく利用者が行う操作のため）。
- 開始の要求はすぐ返す（202 とジョブ）。進み具合はイベント（GET /events）か GET .../current で取る
- 開始・中止は QtInvoker でメインスレッドに渡す（実行中かどうかの判定もメインスレッドで行う）
- GET /events は text/event-stream。各イベントに連番の id を付け、再接続のときは Last-Event-ID より後を送り直す
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from api import actions
from api.auth import require_user
from api.bridge import END
from core.exceptions import ApiConflictError

router = APIRouter(dependencies=[Depends(require_user)])

#: 一度に受け付けるスキャンの対象の数・パスの長さの上限
_MAX_TARGETS = 100
_MAX_PATH = 4096
#: イベントがないときに送る空のコメントの間隔（秒）。途中の機器やクライアントに切られないように
KEEPALIVE_SEC = 15.0
#: 同時につなげるイベントの購読の数（つなぎっぱなしの接続で資源を使い切らないように）
MAX_SUBSCRIBERS = 16


class ScanBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: str = Field(max_length=16)
    targets: list[str] | None = Field(None, max_length=_MAX_TARGETS)
    include_subfolders: bool = True
    apply_exclude: bool = True


def _ctx(request: Request):
    return request.app.state.context


async def _on_main(request: Request, fn, *args):
    ctx = _ctx(request)
    return await ctx.invoker.run(fn, ctx, *args)


# -- 6.2 スキャン -----------------------------------------------------------------------
@router.post("/scans", status_code=202)
async def start_scan(request: Request, body: ScanBody) -> dict:
    if body.targets is not None and any(len(t) > _MAX_PATH for t in body.targets):
        raise ValueError("パスが長すぎます")
    job = await _on_main(request, actions.start_scan, body.type, body.targets, body.include_subfolders,
                         body.apply_exclude)
    return {"job": job}


@router.get("/scans/current")
def current_scan(request: Request) -> dict:
    return {"job": _ctx(request).jobs.current("scan")}


@router.post("/scans/current/cancel", status_code=202)
async def cancel_scan(request: Request) -> dict:
    await _on_main(request, actions.cancel_scan)
    return {"accepted": True}  # 中止の結果は scan.finished（cancelled）で届く


# -- 6.7 定義の更新 ---------------------------------------------------------------------
@router.post("/updates", status_code=202)
async def start_update(request: Request) -> dict:
    return {"job": await _on_main(request, actions.start_update)}


@router.get("/updates/current")
def current_update(request: Request) -> dict:
    return {"job": _ctx(request).jobs.current("update")}


# -- 5章 イベント配信 --------------------------------------------------------------------
def _format(event) -> str:
    # data は JSON（改行を含まない）。event はこちらで決めた名前だけ
    return f"id: {event.id}\nevent: {event.name}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n"


def _parse_last_id(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        last_id = int(value)
    except ValueError:
        return None
    return last_id if last_id >= 0 else None


@router.get("/events")
async def events(request: Request, last_event_id: str | None = Header(None, max_length=20)) -> StreamingResponse:
    hub = _ctx(request).events
    if hub.subscriber_count >= MAX_SUBSCRIBERS:
        raise ApiConflictError("イベントの購読の数が上限に達しています")
    # 先に購読してから再送分を読む（その間に出たイベントを取りこぼさないように。重なった分は id で除く）
    subscription = hub.subscribe()
    last_id = _parse_last_id(last_event_id)
    backlog = hub.events_since(last_id) if last_id is not None else []

    async def stream():
        sent = last_id or 0
        try:
            for event in backlog:
                sent = event.id
                yield _format(event)
            while not subscription.closed:
                try:
                    event = await asyncio.wait_for(subscription.queue.get(), timeout=KEEPALIVE_SEC)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": keepalive\n\n"
                    continue
                if event is END:
                    break  # 待ち受けを止める
                if event.id <= sent:
                    continue
                sent = event.id
                yield _format(event)
        finally:
            subscription.close()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
