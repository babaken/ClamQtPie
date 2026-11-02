"""
ジョブ以外のイベントの発生元（サービス化・Web API設計書 5章。段階 12-2b）

- realtime.*: リアルタイム監視のシグナル（検知・検知後の扱い・監視対象の消失・エラー）
- status: 稼働状態が変わったとき（ホーム画面の定期確認の結果から。変わったときだけ出す）
- config.changed: 設定を保存したとき（画面・Web API のどちらで保存しても。app.py から呼ぶ）
- schedule.finished: スケジュールのジョブの最終実行結果を記録したとき（段階 12-3c）
- notification・notification.acked: 通知を保存したとき・既読にしたとき（サービスのみ。段階 12-3c）

リアルタイム監視のシグナルは監視のスレッドから出るため、EventHub.publish（任意のスレッドから呼べる）に直接つなぐ。
"""
from __future__ import annotations

from api.bridge import EventHub
from core.exceptions import (
    ClamdConnectionError,
    ClamonaccTargetError,
    UnsafeExecutablePathError,
    WatchTargetLostError,
)
from core.text_safety import visible_controls


def connect_realtime(events: EventHub, realtime_manager) -> None:
    realtime_manager.detected.connect(
        lambda path, virus_name: events.publish(
            "realtime.detected", {"path": visible_controls(path), "virus_name": visible_controls(virus_name)}))
    realtime_manager.detection_handled.connect(
        lambda path, virus_name, quarantined: events.publish(
            "realtime.handled", {"path": visible_controls(path), "virus_name": visible_controls(virus_name),
                                 "quarantined": quarantined}))
    realtime_manager.watch_target_lost.connect(
        lambda path: events.publish("realtime.target_lost", {"path": visible_controls(path)}))
    realtime_manager.scan_error.connect(
        lambda error: events.publish("realtime.error", {"code": getattr(error, "code", "E2000"),
                                                        "message": getattr(error, "message", ""),
                                                        "reason": error_reason_of(error)}))


def connect_scheduler(events: EventHub, scheduler_service) -> None:
    """スケジュールのジョブの結果を schedule.finished で知らせる（APScheduler のスレッドから呼ばれる）"""
    scheduler_service.on_job_finished = lambda job_id, name, result, note: events.publish(
        "schedule.finished", {"job_id": job_id, "name": visible_controls(name), "result": result, "note": note})


def connect_notifications(events: EventHub, notification_store) -> None:
    """通知の保存・既読を notification・notification.acked で知らせる（保存は検知の集約のスレッドなどから呼ばれる）"""
    notification_store.on_added = lambda item: events.publish("notification", item)
    notification_store.on_acked = lambda ids: events.publish("notification.acked", {"ids": ids})


def error_reason_of(error) -> str:
    """realtime.error の reason。決まった言葉だけを返す（detail には出力の一部やパスが入るため返さない）。
    clamonacc の失敗は ClamonaccError.reason（core/clamonacc_manager.py の REASON_*）、それ以外は例外の種類から"""
    reason = getattr(error, "reason", None)
    if isinstance(reason, str) and reason:
        return reason
    if isinstance(error, ClamdConnectionError):
        return "clamd_unavailable"
    if isinstance(error, ClamonaccTargetError):
        return "target"
    if isinstance(error, UnsafeExecutablePathError):
        return "unsafe_path"
    if isinstance(error, WatchTargetLostError):
        return "target_lost"
    return "other"


class StatusPublisher:
    """稼働状態のうち、変わったら知らせるもの。GET /status の一部（clamd への問い合わせを伴う項目は、
    ホーム画面の定期確認（60 秒ごと）の結果を使う）。全部が要るときは GET /status を読む"""

    def __init__(self, events: EventHub, realtime_manager, scan_dispatcher, update_dispatcher, jobs) -> None:
        self._events = events
        self._realtime = realtime_manager
        self._scan = scan_dispatcher
        self._update = update_dispatcher
        self._jobs = jobs
        self._last: dict | None = None

    def snapshot(self, app_status) -> dict:
        return {
            "clamd": {"reachable": app_status.clamd_running},
            "definitions": {"age_days": app_status.definitions_age_days, "stale": app_status.definitions_stale},
            "realtime": {"running": app_status.realtime_running, "mode": self._realtime.mode_name},
            "scan": {"running": self._scan.is_running, "job_id": self._jobs.running_job_id("scan")},
            "update": {"running": self._update.is_running, "job_id": self._jobs.running_job_id("update")},
        }

    def on_status(self, app_status) -> None:
        """HomeScreen.status_refreshed から呼ぶ（メインスレッド）"""
        data = self.snapshot(app_status)
        if data != self._last:
            self._last = data
            self._events.publish("status", data)


def publish_config_changed(events: EventHub) -> None:
    events.publish("config.changed", {})
