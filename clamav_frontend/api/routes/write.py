"""
状態を変える API（サービス化・Web API設計書 6章。段階 12-2）

すべて admin のロールが要る。処理は api/actions.py の関数を QtInvoker でメインスレッドに渡して行う（8章）。
時間のかかる処理（clamd の停止、診断、テストメール、スケジュールの「今すぐ実行」）はメインスレッドを止めないよう、
FastAPI のスレッドプール（または専用のスレッド）で行う。
入力は余計な項目・型の違う値（"1" を数として扱うなど）を受け付けない。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import asdict

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from api import actions
from api.auth import require_admin
from api.routes.read import exclude_item
from core.diagnostics_service import mask_report
from core.exceptions import ApiConflictError, AppError
from core.logger import CATEGORY_SYSTEM

logger = logging.getLogger("clamav_frontend")

router = APIRouter(dependencies=[Depends(require_admin)])

#: パス・名前などの文字列の長さの上限（DB・画面に入れる値として十分な長さ）
_MAX_TEXT = 4096


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RestoreBody(_Body):
    overwrite: bool = False


class EnabledBody(_Body):
    enabled: bool


class ExcludeBody(_Body):
    type: str = Field(max_length=16)
    value: str = Field(max_length=_MAX_TEXT)
    enabled: bool = True


class TargetBody(_Body):
    path: str = Field(max_length=_MAX_TEXT)


class ScheduleBody(_Body):
    name: str = Field(max_length=200)
    scan_type: str = Field(max_length=16)
    cron: str = Field(max_length=200)
    target: str = Field("", max_length=_MAX_TEXT)


class PasswordBody(_Body):
    password: str = Field(min_length=1, max_length=1024)


def _ctx(request: Request):
    return request.app.state.context


async def _on_main(request: Request, fn, *args):
    ctx = _ctx(request)
    return await ctx.invoker.run(fn, ctx, *args)


def _no_content() -> Response:
    return Response(status_code=204)


# -- 6.1 clamd・診断 ---------------------------------------------------------------------
@router.post("/clamd/start", status_code=202)
async def start_clamd(request: Request) -> dict:
    ctx = _ctx(request)
    if not await ctx.invoker.run(ctx.start_clamd):
        raise ApiConflictError(detail="clamd の起動を試みています")
    return {"accepted": True}  # 結果は状態（GET /status）で確かめる


@router.post("/clamd/stop", status_code=204)
async def stop_clamd(request: Request) -> Response:
    ctx = _ctx(request)
    # 停止の完了ではなく要求の時点で、停止検知時の自動起動を抑止する（エンジン サービス画面と同じ）
    await ctx.invoker.run(ctx.on_clamd_stop_requested)
    try:
        await run_in_threadpool(ctx.clamd_controller.stop)
    except BaseException:
        await ctx.invoker.run(ctx.on_clamd_stop_failed)
        raise
    actions.audit(CATEGORY_SYSTEM, "clamd を停止しました")
    if ctx.notify_changed is not None:
        await ctx.invoker.run(ctx.notify_changed, actions.CHANGED_CLAMD)
    return _no_content()


@router.post("/diagnostics")
async def run_diagnostics(request: Request) -> dict:
    report = await run_in_threadpool(_ctx(request).diagnostics_service.run)
    # 問い合わせ等に貼る前提の、伏せ字にした版を返す（ホームフォルダ・ユーザー名・ソケットのパスなど）
    return {"has_problem": report.has_problem, "report": mask_report(report.to_text(), extra=report.sensitive)}


# -- 6.3 隔離 ---------------------------------------------------------------------------
@router.post("/quarantine/{quarantine_id}/restore", status_code=204)
async def restore_quarantine(request: Request, quarantine_id: int, body: RestoreBody) -> Response:
    await _on_main(request, actions.restore_quarantine, quarantine_id, body.overwrite)
    return _no_content()


@router.delete("/quarantine/{quarantine_id}", status_code=204)
async def delete_quarantine(request: Request, quarantine_id: int) -> Response:
    await _on_main(request, actions.delete_quarantine, quarantine_id)
    return _no_content()


# -- 6.4 除外 ---------------------------------------------------------------------------
@router.post("/excludes", status_code=201)
async def add_exclude(request: Request, body: ExcludeBody) -> dict:
    rule = await _on_main(request, actions.add_exclude, body.type, body.value, body.enabled)
    return exclude_item(rule)


@router.patch("/excludes/{exclude_id}", status_code=204)
async def set_exclude_enabled(request: Request, exclude_id: int, body: EnabledBody) -> Response:
    await _on_main(request, actions.set_exclude_enabled, exclude_id, body.enabled)
    return _no_content()


@router.delete("/excludes/{exclude_id}", status_code=204)
async def remove_exclude(request: Request, exclude_id: int) -> Response:
    await _on_main(request, actions.remove_exclude, exclude_id)
    return _no_content()


# -- 6.5 リアルタイム監視 -----------------------------------------------------------------
@router.put("/realtime")
async def set_realtime(request: Request, body: EnabledBody) -> dict:
    running = await _on_main(request, actions.set_realtime, body.enabled)
    return {"running": running}


@router.post("/realtime/targets", status_code=201)
async def add_watch_target(request: Request, body: TargetBody) -> dict:
    return asdict(await _on_main(request, actions.add_watch_target, body.path))


@router.patch("/realtime/targets/{target_id}", status_code=204)
async def set_watch_target_enabled(request: Request, target_id: int, body: EnabledBody) -> Response:
    await _on_main(request, actions.set_watch_target_enabled, target_id, body.enabled)
    return _no_content()


@router.delete("/realtime/targets/{target_id}", status_code=204)
async def remove_watch_target(request: Request, target_id: int) -> Response:
    await _on_main(request, actions.remove_watch_target, target_id)
    return _no_content()


# -- 6.6 スケジュール ---------------------------------------------------------------------
@router.post("/schedules", status_code=201)
async def add_schedule(request: Request, body: ScheduleBody) -> dict:
    record = await _on_main(request, actions.add_schedule, body.name, body.scan_type, body.cron, body.target)
    return asdict(record)


@router.put("/schedules/{job_id}")
async def update_schedule(request: Request, job_id: int, body: ScheduleBody) -> dict:
    record = await _on_main(
        request, actions.update_schedule, job_id, body.name, body.scan_type, body.cron, body.target
    )
    return asdict(record)


@router.patch("/schedules/{job_id}", status_code=204)
async def set_schedule_enabled(request: Request, job_id: int, body: EnabledBody) -> Response:
    await _on_main(request, actions.set_schedule_enabled, job_id, body.enabled)
    return _no_content()


@router.delete("/schedules/{job_id}", status_code=204)
async def remove_schedule(request: Request, job_id: int) -> Response:
    await _on_main(request, actions.remove_schedule, job_id)
    return _no_content()


@router.post("/schedules/{job_id}/run", status_code=202)
async def run_schedule(request: Request, job_id: int) -> dict:
    ctx = _ctx(request)
    await _on_main(request, actions.check_schedule_runnable, job_id)
    # スキャンは長くかかるため、スケジュール画面の「今すぐ実行」と同じく専用のスレッドで実行する
    threading.Thread(target=_run_schedule_job, args=(ctx, job_id), name="ApiScheduleRun", daemon=True).start()
    return {"accepted": True}


def _run_schedule_job(ctx, job_id: int) -> None:
    try:
        ctx.scheduler_service.run_now(job_id)
    except AppError as exc:
        # 実行の失敗はスケジュールの処理が記録・通知する。ここでは受け付けた後に重なった場合などを残す
        logger.warning(f"Web API から依頼したジョブを実行できませんでした: {exc.message}",
                       extra={"error_code": exc.code})
    finally:
        if ctx.notify_changed is not None:
            ctx.invoker.call(ctx.notify_changed, actions.CHANGED_SCHEDULES)


# -- 6.8 設定 ---------------------------------------------------------------------------
@router.put("/config", status_code=204)
async def put_config(request: Request, body: dict) -> Response:
    await _on_main(request, actions.save_config, body)
    return _no_content()


@router.put("/config/smtp-password", status_code=204)
async def put_smtp_password(request: Request, body: PasswordBody) -> Response:
    await _on_main(request, actions.set_smtp_password, body.password)
    return _no_content()


@router.delete("/config/smtp-password", status_code=204)
async def delete_smtp_password(request: Request) -> Response:
    await _on_main(request, actions.delete_smtp_password)
    return _no_content()


@router.post("/notifications/test-mail", status_code=204)
async def send_test_mail(request: Request) -> Response:
    await run_in_threadpool(_ctx(request).notification_service.send_test_mail)
    return _no_content()


# -- 6.9 ログ ---------------------------------------------------------------------------
@router.delete("/logs")
async def clear_logs(request: Request) -> dict:
    return {"removed_files": await _on_main(request, actions.clear_logs)}
