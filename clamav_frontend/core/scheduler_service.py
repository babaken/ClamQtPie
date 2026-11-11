"""
SchedulerService — APScheduler連携によるスケジュールジョブ管理

参照: 画面設計書4章、要件定義書、エラーコード一覧・例外設計書E4001〜E4003

取りこぼし対策（サービス化・Web API設計書 9章。段階 12-3c）:
- 同じジョブの重なった回は1回にまとめ（coalesce）、予定の時刻を過ぎても misfire_grace_sec 秒までは実行する
- 電源オフ・スリープ中に過ぎた回は、起動時とスリープからの復帰時（catch_up_missed）に1回だけ取り返す。
  APScheduler はジョブを覚えていない（メモリだけ）ため、各ジョブの最終実行日時（last_run）と実行周期から判断する
- 復帰時は APScheduler の待ち時間も計算し直させる（wakeup。Linux では待ち時間にスリープ中の時間が数えられず、
  そのままでは次の回がスリープしていた時間だけ遅れる）
- inhibit_sleep なら、ジョブの実行中はスリープを防ぐ（core/sleep_inhibit.py）
- wake が有効なジョブのうち、次の実行時刻が最も早いものの WAKE_LEAD 前に、スリープから起こすタイマーを
  1つだけ設定する（core/wake_timer.py。9.1節）。ジョブの追加・変更・削除・有効無効の切り替え・実行の後と、
  復帰時に設定し直す。定義の自動更新では起こさない
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
from core.sleep_inhibit import prevent_sleep
from core.wake_timer import WakeTimer
from db.database import DatabaseService
from db.models import Schedule

logger = logging.getLogger("clamav_frontend")

VALID_SCAN_TYPES = ("scan", "update")
_AUTO_UPDATE_JOB_ID = "auto_update"
#: 前回の更新から間隔が過ぎている場合に、起動（登録）から自動更新を始めるまでの待ち（clamdの起動を待つため）
_AUTO_UPDATE_START_DELAY = timedelta(minutes=2)
#: 取りこぼした回を取り返すまでの待ち（起動直後・復帰直後は、clamd やネットワークの準備を待つ）
CATCH_UP_DELAY = timedelta(minutes=1)
#: 予定の時刻を過ぎても実行する猶予（秒）の既定（設定の schedule.misfire_grace_sec）
DEFAULT_MISFIRE_GRACE_SEC = 3600
#: スリープから起こす時刻を実行時刻より早める長さ（ネットワークや clamd の準備を待つため）
WAKE_LEAD = timedelta(minutes=2)
#: 起こす時刻をまだ OS に設定していない印（起動の直後は、前回の異常終了で残ったタイマーも消すため必ず設定する）
_WAKE_UNKNOWN = object()
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
    #: スリープ中でも起こして実行する（段階 12-3c）
    wake: bool = False


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
        wake=bool(row.wake),
    )


class SchedulerService:
    def __init__(
        self,
        db: DatabaseService,
        run_scan: Callable[[str, str], object],
        run_update: Callable[[], None],
        on_job_fail: Callable[[str, str], None] | None = None,
        misfire_grace_sec: int = DEFAULT_MISFIRE_GRACE_SEC,
        inhibit_sleep: bool = False,
        wake_timer: WakeTimer | None = None,
    ) -> None:
        """
        run_scan: スキャンジョブ実行時に呼ばれるコールバック（対象パス, ジョブ名）。
                  戻り値に検知件数（detected属性）があれば最終実行結果に「検知あり」を記録する
        run_update: 定義更新ジョブ実行時に呼ばれるコールバック
        on_job_fail: ジョブ失敗時に呼ばれるコールバック（ジョブ名, 失敗理由）。
                     通知機能詳細仕様書の on_job_fail 通知に接続する想定（Phase7）。
        misfire_grace_sec: 予定の時刻を過ぎても実行する猶予（秒）。重なった回は1回にまとめる
        inhibit_sleep: ジョブの実行中はスリープを防ぐ（サービス）
        wake_timer: wake が有効なジョブのためにスリープから起こすタイマー（サービス。None なら起こさない）
        on_job_finished: 最終実行結果を記録したときに呼ばれる（ジョブID, ジョブ名, 結果, 補足）。
                         結果は "success" | "failure"。Web API の schedule.finished に使う
        """
        self.db = db
        self._run_scan = run_scan
        self._run_update = run_update
        self._on_job_fail = on_job_fail
        self._scheduler = BackgroundScheduler(
            job_defaults={"coalesce": True, "misfire_grace_time": misfire_grace_sec})
        self._inhibit_sleep = inhibit_sleep
        self._wake_timer = wake_timer
        #: 設定中の起こす時刻（同じ時刻で設定し直さないため）
        self._wake_at: datetime | None | object = _WAKE_UNKNOWN
        self._wake_lock = threading.Lock()
        self.on_job_finished: Callable[[int, str, str, str], None] | None = None
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
            self.refresh_wake()

    def shutdown(self) -> None:
        if self._started:
            self._scheduler.shutdown(wait=False)
            self._started = False
            # サービスが止まっている間に起こしても実行できないため消す（起動時に設定し直す）
            self._apply_wake(None)
            self._wake_at = _WAKE_UNKNOWN

    # -- ジョブCRUD -----------------------------------------------------
    def add_job(self, name: str, scan_type: str, cron: str, target: str = "", wake: bool = False) -> ScheduleRecord:
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
                wake=1 if wake else 0,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            record = _to_record(row)

        if self._started:
            self._schedule_job(record)
            self.refresh_wake()
        return record

    def update_job(
        self, job_id: int, name: str, scan_type: str, cron: str, target: str = "", wake: bool | None = None
    ) -> ScheduleRecord:
        """wake: None なら変えない（従来のアプリの編集画面には、まだこの項目が無い）"""
        self._validate(name, scan_type, cron, exclude_id=job_id)

        with self.db.session() as session:
            row = session.get(Schedule, job_id)
            if row is None:
                raise JobValidationError(detail=f"ジョブが見つかりません: id={job_id}")
            row.name = name
            row.scan_type = scan_type
            row.cron = cron
            row.target = target
            if wake is not None:
                row.wake = 1 if wake else 0
            session.commit()
            record = _to_record(row)

        if self._started:
            self._unschedule_job(job_id)
            if record.enabled:
                self._schedule_job(record)
            self.refresh_wake()
        return record

    def remove_job(self, job_id: int) -> None:
        with self.db.session() as session:
            row = session.get(Schedule, job_id)
            if row is not None:
                session.delete(row)
                session.commit()
        if self._started:
            self._unschedule_job(job_id)
            self.refresh_wake()

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
            self.refresh_wake()

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
            self._guarded(run_update, "定義の自動更新"),
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

    # -- 取りこぼし対策 -----------------------------------------------------
    def catch_up_missed(self, now: datetime | None = None) -> list[str]:
        """電源オフ・スリープ中に過ぎた回があるジョブを、少し後（CATCH_UP_DELAY）に1回だけ実行する。
        取り返すジョブの名前を返す。最終実行日時の無いジョブ（一度も実行していない）は対象にしない"""
        if not self._started:
            return []
        now = now or datetime.now(timezone.utc)
        names = []
        for record in self.list_all():
            if not record.enabled or self.is_job_running(record.id):
                continue
            last = _parse_last_run(record.last_run)
            if last is None:
                continue
            try:
                missed = cron_trigger(record.cron).get_next_fire_time(None, last)
            except ValueError:
                continue
            if missed is None or missed > now:
                continue
            # 実行周期のジョブは次の回から数え直し（過ぎた回を APScheduler が重ねて実行しないよう）、
            # 取り返しの1回を別に登録する
            self._unschedule_job(record.id)
            self._schedule_job(record)
            self._scheduler.add_job(
                self._execute, trigger="date", run_date=now + CATCH_UP_DELAY, args=[record],
                id=f"catch_up_{record.id}", replace_existing=True,
            )
            names.append(record.name)
            logger.info(f"電源オフ・スリープ中に過ぎた回を取り返します: {record.name}（予定 {missed.isoformat()}）")
        return names

    def on_resume(self, slept_sec: float = 0.0) -> list[str]:
        """スリープからの復帰時に呼ぶ（core/resume_watcher.py）"""
        if not self._started:
            return []
        logger.info(f"スリープからの復帰を検出しました（約 {int(slept_sec)} 秒）。スケジュールを確かめます")
        names = self.catch_up_missed()
        self._scheduler.wakeup()
        self.refresh_wake()
        return names

    # -- スリープから起こす ---------------------------------------------------
    def next_wake_time(self) -> datetime | None:
        """wake が有効なジョブの次の実行時刻のうち、最も早いものの WAKE_LEAD 前。無ければ None"""
        if not self._started:
            return None
        times = [datetime.fromisoformat(record.next_run) for record in self.list_all()
                 if record.enabled and record.wake and record.next_run]
        return min(times) - WAKE_LEAD if times else None

    def refresh_wake(self) -> None:
        """スリープから起こすタイマーを設定し直す。起こす時刻が過ぎていれば（実行の直前）消す"""
        if self._wake_timer is None:
            return
        try:
            when = self.next_wake_time()
        except Exception as exc:  # noqa: BLE001 - 起こせなくても、復帰後に取り返す
            logger.warning(f"スリープから起こす時刻を決められませんでした (detail: {exc})")
            return
        if when is not None and when <= datetime.now(timezone.utc):
            when = None
        self._apply_wake(when)

    def _apply_wake(self, when: datetime | None) -> None:
        if self._wake_timer is None:
            return
        with self._wake_lock:
            if when == self._wake_at:
                return
            try:
                if when is None:
                    self._wake_timer.clear()
                    logger.info("スリープから起こす予定を消しました")
                else:
                    self._wake_timer.set(when)
                    logger.info(f"スリープから起こす予定を設定しました: {when.isoformat()}")
                self._wake_at = when
            except Exception as exc:  # noqa: BLE001 - 起こせなくても、復帰後に取り返す
                logger.warning(f"スリープから起こすタイマーを設定できませんでした (detail: {exc})")

    def is_job_running(self, job_id: int) -> bool:
        """ジョブを実行中か（Web API の「今すぐ実行」で、別のスレッドに渡す前に 409 を返すため）"""
        with self._running_lock:
            return job_id in self._running_job_ids

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
            if self._inhibit_sleep:
                with prevent_sleep(f"スケジュールのジョブを実行中: {record.name}"):
                    self._execute_body(record)
            else:
                self._execute_body(record)
        finally:
            with self._running_lock:
                self._running_job_ids.discard(record.id)
            # 次の回に合わせて起こす時刻を設定し直す
            if record.wake:
                self.refresh_wake()

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
        name = None
        with self.db.session() as session:
            row = session.get(Schedule, job_id)
            if row is not None:
                row.last_run = f"{executed_at} ({note})" if note else executed_at
                name = row.name
                session.commit()
        if name is not None and self.on_job_finished is not None:
            try:
                self.on_job_finished(job_id, name, "success" if success else "failure", note)
            except Exception as exc:  # noqa: BLE001 - 知らせる処理の失敗でジョブの結果を変えない
                logger.warning(f"スケジュールの結果を知らせられませんでした (detail: {exc})")

    def _guarded(self, func: Callable[[], object], reason: str) -> Callable[[], object]:
        """inhibit_sleep なら、実行中はスリープを防ぐ"""
        if not self._inhibit_sleep:
            return func

        def run():
            with prevent_sleep(reason):
                return func()

        return run


def _parse_last_run(text: str) -> datetime | None:
    """最終実行日時（"<ISO 8601>" または "<ISO 8601> (補足)"）の日時"""
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text.split(" ", 1)[0])
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
