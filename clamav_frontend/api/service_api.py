"""
サービス（service_main.py）の内部 API（サービス化・Web API設計書 9章・9.4節。段階 12-3a）

従来の app.py の API のつなぎ込み（段階 12-1〜12-2）から、画面への反映を除いたもの。サービスでは API が必ず要る
（画面・WebUI はこの API を通して操作する）ため、config.api.enabled に関係なく待ち受け、待ち受けられなければ
起動を失敗にする（9.4節）。トークンファイルはデータの置き場所の api/ に書き出す。
user.token を「ClamQtPie Users」グループに読ませるのは、グループを作るインストーラ（段階 12-4）とあわせて行う。
"""
from __future__ import annotations

import logging
import threading
from types import SimpleNamespace

from PySide6.QtCore import QTimer

from api.app import create_app
from api.auth import ApiTokens
from api.bridge import EventHub, QtInvoker
from api.context import ApiContext
from api.events import StatusPublisher, connect_realtime, connect_scheduler, publish_config_changed
from api.jobs import JobTracker
from api.server import LOOPBACK_HOST, ApiServer
from core.logger import CATEGORY_SYSTEM, log_event, setup_logger
from core.runtime import Runtime

logger = logging.getLogger("clamav_frontend")

#: 稼働状態のイベント（status）を確かめる間隔。clamd への問い合わせは別のスレッドで、より長い間隔で行う
_STATUS_INTERVAL_MS = 2000
_CLAMD_CHECK_INTERVAL_SEC = 60


class ServiceApi:
    """内部 API とイベントの発生元。メインスレッド（QCoreApplication のスレッド）で作る"""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.invoker = QtInvoker()
        self.events = EventHub()
        self.jobs = JobTracker(self.events)
        self.jobs.connect(runtime.scan_dispatcher, runtime.update_dispatcher)
        connect_realtime(self.events, runtime.realtime_manager)
        connect_scheduler(self.events, runtime.scheduler_service)
        self._clamd_running: bool | None = None
        self._stop = threading.Event()
        self._status = StatusPublisher(self.events, runtime.realtime_manager, runtime.scan_dispatcher,
                                       runtime.update_dispatcher, self.jobs)
        self._timer = QTimer()
        self._timer.timeout.connect(self._publish_status)
        self._clamd_thread = threading.Thread(target=self._watch_clamd, name="ClamdStatus", daemon=True)
        self.server: ApiServer | None = None

    def start(self) -> ApiServer:
        """待ち受けを始める。待ち受けられなければ OSError・RuntimeError（呼び出し側でサービスの起動を失敗にする）"""
        rt = self.runtime
        config = rt.config_service.current
        context = ApiContext(
            config_service=rt.config_service, clamav_service=rt.clamav_service,
            realtime_manager=rt.realtime_manager, scan_dispatcher=rt.scan_dispatcher,
            update_service=rt.update_service, update_dispatcher=rt.update_dispatcher,
            scheduler_service=rt.scheduler_service, quarantine_service=rt.quarantine_service,
            exclude_service=rt.exclude_service, watch_target_service=rt.watch_target_service,
            log_reader_service=rt.log_reader_service, scan_history_service=rt.scan_history_service,
            invoker=self.invoker, events=self.events, jobs=self.jobs,
            notification_service=rt.notification_service, diagnostics_service=rt.diagnostics_service,
            apply_config=self._apply_config, start_clamd=rt.clamd_starter.start,
            clamd_controller=rt.clamd_controller,
            on_clamd_stop_requested=lambda: None, on_clamd_stop_failed=lambda: None,
        )
        tokens = ApiTokens(rt.paths.api_dir)
        server = ApiServer(create_app(tokens, context), config.api.port, tokens=tokens)
        try:
            tokens.write_files()
            server.start()
        except (OSError, RuntimeError):
            tokens.remove_files()
            raise
        self.server = server
        log_event(CATEGORY_SYSTEM, f"Web API の待ち受けを開始しました（{LOOPBACK_HOST}:{server.port}）")
        self._timer.start(_STATUS_INTERVAL_MS)
        self._clamd_thread.start()
        return server

    def stop(self) -> None:
        self._stop.set()
        self._timer.stop()
        if self.server is not None:
            self.server.stop()
            self.server = None

    def _apply_config(self, saved) -> None:
        """保存した設定を反映する（app.py の _on_settings_saved から画面の処理を除いたもの）"""
        rt = self.runtime
        setup_logger(rt.paths.log_dir, level=saved.logging.level, retention_days=saved.logging.retention_days)
        rt.scheduler_service.set_auto_update(rt.run_update, saved.update.auto_update, saved.update.interval_hours,
                                             last_run=rt.update_service.last_executed_at())
        rt.update_service.freshclam_path = saved.update.freshclam_path
        rt.clamd_controller.config = saved.clamd
        publish_config_changed(self.events)

    def _watch_clamd(self) -> None:
        while not self._stop.is_set():
            try:
                self._clamd_running = bool(self.runtime.clamav_service.ping(retry=False))
            except Exception:  # noqa: BLE001 - 応答しない clamd は「停止」
                self._clamd_running = False
            self._stop.wait(_CLAMD_CHECK_INTERVAL_SEC)

    def _publish_status(self) -> None:
        rt = self.runtime
        self._status.on_status(SimpleNamespace(
            clamd_running=self._clamd_running, definitions_age_days=None, definitions_stale=False,
            realtime_running=rt.realtime_manager.is_running))
