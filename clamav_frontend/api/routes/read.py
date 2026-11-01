"""
読み取りの API（サービス化・Web API設計書 6章。段階 12-1）

すべて user 以上のロールで呼べる。一覧は ?limit=（既定 100、最大 1000）・?offset= を受け付け、
{"items": [...], "total": n} を返す（4.1節）。処理はブロッキング（DB・ログファイル・clamd への問い合わせ）のため、
async にせず FastAPI のスレッドプールで動かす。
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, time

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response

from api.actions import config_view
from api.auth import require_user
from core.definitions import definitions_age_days
from core.text_safety import visible_controls

router = APIRouter(dependencies=[Depends(require_user)])

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


def _ctx(request: Request):
    return request.app.state.context


def _page(items: list, limit: int, offset: int) -> dict:
    return {"items": items[offset:offset + limit], "total": len(items)}


Limit = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
Offset = Query(0, ge=0)


# -- 6.1 状態 ------------------------------------------------------------------------
@router.get("/status")
def get_status(request: Request) -> dict:
    ctx = _ctx(request)
    config = ctx.config_service.current

    clamd = {"reachable": False, "engine_version": "", "database_version": "", "database_date": ""}
    try:
        if ctx.clamav_service.ping(retry=False):
            engine, db_version, db_date = ctx.clamav_service.get_version()
            clamd = {"reachable": True, "engine_version": engine, "database_version": db_version,
                     "database_date": db_date}
    except Exception:  # noqa: BLE001 - 応答しない clamd は「停止」として返す（ホーム画面と同じ）
        pass
    # clamd が応答しない間は定義の日付が分からないため、古さも判定しない
    age = definitions_age_days(clamd["database_date"]) if clamd["reachable"] else None

    last_update = ctx.update_service.last_executed_at()
    next_run, next_job_name = "", ""
    for record in ctx.scheduler_service.list_all():
        if record.enabled and record.next_run and (not next_run or record.next_run < next_run):
            next_run, next_job_name = record.next_run, record.name

    return {
        "clamd": clamd,
        "definitions": {"age_days": age, "stale": age is not None and age > config.update.stale_days},
        "realtime": {"running": ctx.realtime_manager.is_running, "mode": ctx.realtime_manager.mode_name},
        "scan": {"running": ctx.scan_dispatcher.is_running},
        "update": {
            "running": ctx.update_dispatcher.is_running,
            "last_executed_at": last_update.isoformat() if last_update else None,
            "auto_update": config.update.auto_update,
        },
        "schedule": {"next_run": next_run or None, "next_job_name": next_job_name or None},
        "quarantine": {"count": sum(1 for q in ctx.quarantine_service.list_all() if not q.restored and not q.deleted)},
        "web": {"enabled": False},  # WebUI は段階 12-5
    }


# -- 6.3〜6.7 一覧 ---------------------------------------------------------------------
def _quarantine_status(row) -> str:
    if row.restored:
        return "restored"
    if row.deleted:
        return "deleted"
    return "quarantined"


@router.get("/quarantine")
def list_quarantine(request: Request, limit: int = Limit, offset: int = Offset) -> dict:
    items = [
        {
            "id": row.id,
            "original_path": row.original_path,
            "virus_name": row.virus_name,
            "sha256": row.sha256,
            "size": row.size,
            "detected_at": row.detected_at,
            "status": _quarantine_status(row),
        }
        for row in _ctx(request).quarantine_service.list_all()
    ]
    return _page(items, limit, offset)


def exclude_item(row) -> dict:
    return {"id": row.id, "type": row.type, "value": row.value, "enabled": bool(row.enabled)}


@router.get("/excludes")
def list_excludes(request: Request, limit: int = Limit, offset: int = Offset) -> dict:
    items = [exclude_item(row) for row in _ctx(request).exclude_service.list_all()]
    return _page(items, limit, offset)


@router.get("/realtime")
def get_realtime(request: Request) -> dict:
    ctx = _ctx(request)
    return {
        "running": ctx.realtime_manager.is_running,
        "mode": ctx.realtime_manager.mode_name,
        "default_action": ctx.config_service.current.realtime.default_action,
    }


@router.get("/realtime/targets")
def list_watch_targets(request: Request, limit: int = Limit, offset: int = Offset) -> dict:
    items = [asdict(record) for record in _ctx(request).watch_target_service.list_all()]
    return _page(items, limit, offset)


@router.get("/schedules")
def list_schedules(request: Request, limit: int = Limit, offset: int = Offset) -> dict:
    items = [asdict(record) for record in _ctx(request).scheduler_service.list_all()]
    return _page(items, limit, offset)


@router.get("/updates/history")
def list_update_history(request: Request, limit: int = Limit, offset: int = Offset) -> dict:
    items = [asdict(record) for record in _ctx(request).update_service.list_history()]
    for item in items:
        item.pop("detail", None)  # DB には保存しない項目（常に空）
    return _page(items, limit, offset)


# -- 6.8 設定 ---------------------------------------------------------------------------
@router.get("/config")
def get_config(request: Request) -> dict:
    # API 自身の設定は返さない。SMTP のパスワードは有無だけ。Web API で変えられない項目は protected に並べる
    return config_view(_ctx(request))


# -- 6.9 ログ ---------------------------------------------------------------------------
def _log_entries(request: Request, since: date | None, category: str | None, level: str | None, q: str):
    service = _ctx(request).log_reader_service
    entries = service.read_entries(since=since)
    entries = service.filter_entries(
        entries,
        levels={level.upper()} if level else None,
        # read_entries の since はファイル単位の絞り込みのため、行の日時でも絞る
        date_from=datetime.combine(since, time.min) if since else None,
        categories={category} if category else None,
        keyword=q,
    )
    return service, sorted(entries, key=lambda e: e.datetime, reverse=True)  # 新しい順（ログ画面と同じ）


@router.get("/logs")
def list_logs(
    request: Request,
    since: date | None = None,
    category: str | None = None,
    level: str | None = None,
    q: str = Query("", max_length=200),
    limit: int = Limit,
    offset: int = Offset,
) -> dict:
    _, entries = _log_entries(request, since, category, level, q)
    items = [
        {
            # ログの時刻はこのPC のローカル時刻（タイムゾーン付きにして返す）
            "datetime": entry.datetime.astimezone().isoformat(),
            "level": entry.level,
            "category": entry.category,
            "error_code": entry.error_code or None,
            # 1件の記録が複数行に見えないよう、制御文字を見える形にする（12章 41）
            "message": visible_controls(entry.message),
        }
        for entry in entries
    ]
    return _page(items, limit, offset)


@router.get("/logs/export.csv")
def export_logs_csv(
    request: Request,
    since: date | None = None,
    category: str | None = None,
    level: str | None = None,
    q: str = Query("", max_length=200),
) -> Response:
    service, entries = _log_entries(request, since, category, level, q)
    return Response(
        content=service.csv_bytes(entries),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="clamqtpie_log.csv"'},
    )
