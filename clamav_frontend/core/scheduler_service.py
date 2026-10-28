"""
SchedulerService — APScheduler連携によるスケジュールジョブ管理

参照: 画面設計書4章、要件定義書、エラーコード一覧・例外設計書E4001〜E4003
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from core.exceptions import JobAlreadyRunningError, JobExecutionError, JobValidationError
from db.database import DatabaseService
from db.models import Schedule

logger = logging.getLogger("clamav_frontend")

VALID_SCAN_TYPES = ("scan", "update")
_AUTO_UPDATE_JOB_ID = "auto_update"
#: 前回の更新から間隔が過ぎている場合に、起動（登録）から自動更新を始めるまでの待ち（clamdの起動を待つため）
_AUTO_UPDATE_START_DELAY = timedelta(minutes=2)
#: 標準のcron式の曜日（0と7が日曜）→ APSchedulerの曜日名
_CRON_WEEKDAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")


def cron_trigger(cron: str) -> CronTrigger:
    """標準のcron式（曜日は0と7が日曜、1が月曜…6が土曜）からCronTriggerを作る。不正な式はValueError

    APSchedulerのfrom_crontabは曜日欄の数字を0=月曜として解釈するため、そのまま渡すと1日ずれる
    （プリセット「毎週日曜 3:00」（0 3 * * 0）が毎週月曜に実行され、7は不正な値になっていた。コードレビューで発見）。
    画面の表示（schedule.pyのdescribe_cron）とDBに保存済みのcron式は標準の数え方なので、曜日欄だけ名前に変換して渡す。
    """
    fields = cron.split()
    if len(fields) != 5:
        raise ValueError(f"Cron式は「分 時 日 月 曜日」の5つの欄で指定してください: {cron!r}")
    fields[4] = _weekday_field_to_names(fields[4])
    return CronTrigger.from_crontab(" ".join(fields))


def _weekday_field_to_names(field: str) -> str:
    """曜日欄（例: "0"、"1-5"、"*/2"、"sat,sun"）を曜日名の並び（例: "sun"、"mon,tue,wed,thu,fri"）にする"""
    if field == "*":
        return field
    days: set[int] = set()
    for part in field.split(","):
        base, _, step_text = part.partition("/")
        step = int(step_text) if step_text else 1
        if step < 1:
            raise ValueError(f"曜日の間隔が正しくありません: {part!r}")
        if base == "*":
            start, end = 0, 6
        elif "-" in base:
            first, last = base.split("-", 1)
            start, end = _weekday_number(first), _weekday_number(last)
            if start > end:
                raise ValueError(f"曜日の範囲が正しくありません: {part!r}")
        else:
            start = _weekday_number(base)
            end = 6 if step_text else start
        days.update(day % 7 for day in range(start, end + 1, step))
    if len(days) == 7:
        return "*"
    return ",".join(_CRON_WEEKDAY_NAMES[day] for day in sorted(days))


def _weekday_number(text: str) -> int:
    value = text.strip().lower()
    if value in _CRON_WEEKDAY_NAMES:
        return _CRON_WEEKDAY_NAMES.index(value)
    if not value.isdigit() or not 0 <= int(value) <= 7:
        raise ValueError(f"曜日の指定が正しくありません: {text!r}（0〜7、または sun〜sat）")
    return int(value)


@dataclass
class ScheduleRecord:
    id: int
    name: str
    enabled: bool
    cron: str
    target: str
    scan_type: str
    last_run: str
    next_run: str


def _to_record(row: Schedule) -> ScheduleRecord:
    return ScheduleRecord(
        id=row.id,
        name=row.name,
        enabled=bool(row.enabled),
        cron=row.cron,
        target=row.target or "",
        scan_type=row.scan_type,
        last_run=row.last_run or "",
        next_run=row.next_run or "",
    )


class SchedulerService:
    def __init__(
        self,
        db: DatabaseService,
        run_scan: Callable[[str, str], object],
        run_update: Callable[[], None],
        on_job_fail: Callable[[str, str], None] | None = None,
    ) -> None:
        """
        run_scan: スキャンジョブ実行時に呼ばれるコールバック（対象パス, ジョブ名）。
                  戻り値に検知件数（detected属性）があれば最終実行結果に「検知あり」を記録する
        run_update: 定義更新ジョブ実行時に呼ばれるコールバック
        on_job_fail: ジョブ失敗時に呼ばれるコールバック（ジョブ名, 失敗理由）。
                     通知機能詳細仕様書の on_job_fail 通知に接続する想定（Phase7）。
        """
        self.db = db
        self._run_scan = run_scan
        self._run_update = run_update
        self._on_job_fail = on_job_fail
        self._scheduler = BackgroundScheduler()
        self._started = False
        # 同一ジョブの重複実行防止（「今すぐ実行」とスケジュール発火の重なり等）。
        # APSchedulerのmax_instancesはスケジュール発火同士しか防げないため、全経路で共通の判定を持つ
        self._running_job_ids: set[int] = set()
        self._running_lock = threading.Lock()
        #: 登録中の自動更新ジョブの間隔（時間）。同じ間隔での再登録（設定の保存）では次回時刻を変えないために使う
        self._auto_update_hours: int | None = None

    def start(self) -> None:
        if not self._started:
            self._scheduler.start()
            self._started = True
            self._reload_all_jobs()

    def shutdown(self) -> None:
        if self._started:
            self._scheduler.shutdown(wait=False)
            self._started = False

    # -- ジョブCRUD -----------------------------------------------------
    def add_job(self, name: str, scan_type: str, cron: str, target: str = "") -> ScheduleRecord:
        self._validate(name, scan_type, cron, exclude_id=None)

        with self.db.session() as session:
            row = Schedule(
                name=name,
                enabled=1,
                cron=cron,
                target=target,
                scan_type=scan_type,
                last_run=None,
                next_run=None,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            record = _to_record(row)

        if self._started:
            self._schedule_job(record)
        return record

    def update_job(
        self, job_id: int, name: str, scan_type: str, cron: str, target: str = ""
    ) -> ScheduleRecord:
        self._validate(name, scan_type, cron, exclude_id=job_id)

        with self.db.session() as session:
            row = session.get(Schedule, job_id)
            if row is None:
                raise JobValidationError(detail=f"ジョブが見つかりません: id={job_id}")
            row.name = name
            row.scan_type = scan_type
            row.cron = cron
            row.target = target
            session.commit()
            record = _to_record(row)

        if self._started:
            self._unschedule_job(job_id)
            self._schedule_job(record)
        return record

    def remove_job(self, job_id: int) -> None:
        with self.db.session() as session:
            row = session.get(Schedule, job_id)
            if row is not None:
                session.delete(row)
                session.commit()
        if self._started:
            self._unschedule_job(job_id)

    def set_enabled(self, job_id: int, enabled: bool) -> None:
        with self.db.session() as session:
            row = session.get(Schedule, job_id)
            if row is not None:
                row.enabled = 1 if enabled else 0
                record = _to_record(row)
                session.commit()
            else:
                return

        if self._started:
            self._unschedule_job(job_id)
            if enabled:
                self._schedule_job(record)

    def set_auto_update(
        self,
        run_update: Callable[[], object],
        enabled: bool,
        interval_hours: int,
        last_run: datetime | None = None,
    ) -> None:
        """設定画面「定義更新」の自動更新ジョブ（scheduleテーブルとは別管理）を登録／解除する

        起動時と設定保存時に呼ばれ、保存後すぐに反映される（画面設計書8章の即時反映）。
        last_run: 前回の定義更新の日時（UpdateService.last_executed_at()）。初回の実行はその interval_hours 後とし、
            既に過ぎていれば（履歴が無い場合も）登録の少し後（_AUTO_UPDATE_START_DELAY）に実行する。
            以前は登録（起動・設定保存）から interval_hours 後が初回だったため、起動から4時間以内にPCを終了する
            使い方では自動更新が一度も実行されないことがあった（コードレビューで発見）。
        間隔が変わらない再登録（自動更新と関係のない設定の保存等）では、次回の実行時刻を変えない。
        """
        job = self._scheduler.get_job(_AUTO_UPDATE_JOB_ID)
        if not enabled:
            if job is not None:
                self._scheduler.remove_job(_AUTO_UPDATE_JOB_ID)
            self._auto_update_hours = None
            return
        if job is not None and self._auto_update_hours == interval_hours:
            return
        next_run = datetime.now(timezone.utc) + _AUTO_UPDATE_START_DELAY
        if last_run is not None:
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=timezone.utc)
            next_run = max(next_run, last_run + timedelta(hours=interval_hours))
        self._scheduler.add_job(
            run_update,
            trigger="interval",
            hours=interval_hours,
            next_run_time=next_run,
            id=_AUTO_UPDATE_JOB_ID,
            replace_existing=True,
        )
        self._auto_update_hours = interval_hours

    def list_all(self) -> list[ScheduleRecord]:
        with self.db.session() as session:
            rows = session.query(Schedule).order_by(Schedule.id).all()
            records = [_to_record(r) for r in rows]
        # 次回実行日時はAPSchedulerが管理する値を表示する（DBのnext_run列は更新していなかったため常に空だった）
        if self._started:
            for record in records:
                job = self._scheduler.get_job(f"schedule_{record.id}") if record.enabled else None
                next_time = getattr(job, "next_run_time", None)
                record.next_run = next_time.isoformat() if next_time else ""
        return records

    def run_now(self, job_id: int) -> None:
        with self.db.session() as session:
            row = session.get(Schedule, job_id)
            if row is None:
                raise JobValidationError(detail=f"ジョブが見つかりません: id={job_id}")
            record = _to_record(row)
        self._execute(record, raise_if_running=True)

    # -- バリデーション（E4001, E4002） -----------------------------------
    def _validate(self, name: str, scan_type: str, cron: str, exclude_id: int | None) -> None:
        if scan_type not in VALID_SCAN_TYPES:
            raise JobValidationError(detail=f"不正な種別です: {scan_type}")

        with self.db.session() as session:
            query = session.query(Schedule).filter(Schedule.name == name)
            if exclude_id is not None:
                query = query.filter(Schedule.id != exclude_id)
            if query.first() is not None:
                raise JobValidationError(
                    "同名のジョブが既に存在します", code="E4001", detail=f"name={name}"
                )

        try:
            cron_trigger(cron)
        except ValueError as exc:
            raise JobValidationError(
                "実行周期の指定が正しくありません", code="E4002", detail=str(exc)
            ) from exc

    # -- APScheduler連携 -----------------------------------------------------
    def _reload_all_jobs(self) -> None:
        for record in self.list_all():
            if record.enabled:
                self._schedule_job(record)

    def _schedule_job(self, record: ScheduleRecord) -> None:
        trigger = cron_trigger(record.cron)
        self._scheduler.add_job(
            self._execute,
            trigger=trigger,
            args=[record],
            id=f"schedule_{record.id}",
            replace_existing=True,
        )

    def _unschedule_job(self, job_id: int) -> None:
        job = self._scheduler.get_job(f"schedule_{job_id}")
        if job is not None:
            job.remove()

    def _execute(self, record: ScheduleRecord, raise_if_running: bool = False) -> None:
        """ジョブを実行する。同一ジョブが実行中の場合は実行しない

        raise_if_running: Trueなら実行中の場合にJobAlreadyRunningError(E4004)を送出する
                          （「今すぐ実行」でユーザーへ知らせるため）。Falseならログ記録のみで終了する
                          （スケジュール発火時。次回の発火を待てばよいため失敗扱いにしない）。
        """
        with self._running_lock:
            if record.id in self._running_job_ids:
                logger.warning(
                    f"ジョブが実行中のため今回の実行をスキップしました: {record.name}",
                    extra={"error_code": "E4004"},
                )
                if raise_if_running:
                    raise JobAlreadyRunningError(detail=f"name={record.name}")
                return
            self._running_job_ids.add(record.id)

        try:
            self._execute_body(record)
        finally:
            with self._running_lock:
                self._running_job_ids.discard(record.id)

    def _execute_body(self, record: ScheduleRecord) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            if record.scan_type == "scan":
                outcome = self._run_scan(record.target, record.name)
            else:
                outcome = self._run_update()
            # 画面設計書4章「最終実行結果」: 成功／失敗／検知あり
            # - スキャン: 戻り値のdetectedが1件以上なら「検知あり」
            # - 定義更新: 戻り値のresultが"failure"なら失敗（通知はrun_update側で送信済みのため
            #   on_job_failは呼ばない＝二重通知を避ける）。"skipped"（LinuxでOSのサービスが更新する場合）は「スキップ」
            detected = getattr(outcome, "detected", 0) or 0
            result = getattr(outcome, "result", None)
            failed = result == "failure"
            if detected:
                note = f"検知あり {detected}件"
            elif failed:
                note = "失敗"
            elif result == "skipped":
                note = "スキップ（OSのサービスが更新）"
            else:
                note = ""
            # スキャンで検査できなかったファイル（サイズの上限・権限等）があれば件数を残す
            errors = (getattr(outcome, "errors", 0) or 0) if record.scan_type == "scan" else 0
            if errors:
                note = f"{note}、未検査 {errors}件" if note else f"未検査 {errors}件"
            self._update_last_run(record.id, now_iso, success=not failed, note=note)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                f"スケジュールジョブの実行に失敗しました: {record.name} (detail: {exc})",
                extra={"error_code": "E4003"},
            )
            self._update_last_run(record.id, now_iso, success=False)
            if self._on_job_fail is not None:
                try:
                    self._on_job_fail(record.name, str(exc))
                except Exception:  # noqa: BLE001 - 通知処理の失敗でジョブ実行自体を壊さない
                    logger.warning("ジョブ失敗通知のコールバックでエラーが発生しました")
            raise JobExecutionError(detail=str(exc)) from exc

    def _update_last_run(self, job_id: int, executed_at: str, success: bool, note: str = "") -> None:
        if not success:
            note = "失敗"
        with self.db.session() as session:
            row = session.get(Schedule, job_id)
            if row is not None:
                row.last_run = f"{executed_at} ({note})" if note else executed_at
                session.commit()
