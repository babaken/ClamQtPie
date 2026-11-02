"""
API から呼ぶサービスの置き場（app.py が作ったものをそのまま渡す。API 側では作らない）

画面（gui/）と同じサービスを共有する。
- 読み取りの API は、DB・ログファイルの読み取りと、状態の参照（実行中かどうか）だけを行い、
  FastAPI のスレッドプールから直接呼ぶ（サービス化・Web API設計書 8章）
- 状態を変える API（段階 12-2）は、invoker でメインスレッドに渡して api/actions.py の処理を呼ぶ。
  画面に表示している内容は notify_changed で app.py に知らせて読み直させる
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ApiContext:
    config_service: Any
    clamav_service: Any
    realtime_manager: Any
    scan_dispatcher: Any
    update_service: Any
    update_dispatcher: Any
    scheduler_service: Any
    quarantine_service: Any
    exclude_service: Any
    watch_target_service: Any
    log_reader_service: Any
    #: core.scan_history_service.ScanHistoryService（段階 12-2c。None なら履歴の API は空を返す）
    scan_history_service: Any = None
    # -- 状態を変える API（段階 12-2）で使う。None なら状態を変える API は付けない ------------------
    #: api.bridge.QtInvoker（メインスレッドで作ったもの）
    invoker: Any = None
    #: api.bridge.EventHub（SSE で配るイベント）と api.jobs.JobTracker（スキャン・定義の更新のジョブ。段階 12-2b）
    events: Any = None
    jobs: Any = None
    notification_service: Any = None
    #: core.notification_store.NotificationStore（サービスの通知の保存。段階 12-3c。None なら通知の API は付けない）
    notification_store: Any = None
    diagnostics_service: Any = None
    #: 変更した対象の名前（"excludes"・"targets"・"quarantine"・"schedules"・"config"・"realtime"・"logs"）を受け取り、
    #: 画面を読み直す（メインスレッドで呼ぶ）
    notify_changed: Callable[[str], None] | None = None
    #: 保存した設定を各サービスに反映する（app.py の _on_settings_saved と同じ処理）
    apply_config: Callable[[Any], None] | None = None
    #: clamd の起動を試みる（バックグラウンド）。試行を始めたら True、既に試行中なら False
    start_clamd: Callable[[], bool] | None = None
    #: core.clamd_process.ClamdServiceController（stop() は時間がかかるため、スレッドプールで呼ぶ）
    clamd_controller: Any = None
    #: clamd の停止を要求したとき・停止に失敗したとき（停止検知時の自動起動の抑止とその取り消し。メインスレッドで呼ぶ）
    on_clamd_stop_requested: Callable[[], None] | None = None
    on_clamd_stop_failed: Callable[[], None] | None = None

    @property
    def can_write(self) -> bool:
        return self.invoker is not None
