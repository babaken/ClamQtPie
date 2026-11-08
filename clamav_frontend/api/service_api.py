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
from api.events import (
    StatusPublisher,
    connect_notifications,
    connect_realtime,
    connect_scheduler,
    publish_config_changed,
)
from api.jobs import JobTracker
from api.server import LOOPBACK_HOST, ApiServer
from api.web_control import WebController
from core.definitions import definitions_age_days
from core.logger import CATEGORY_SYSTEM, log_event, setup_logger
from core.peer_identity import requester_home
from core.runtime import Runtime
from core.service_endpoint import write_endpoint

logger = logging.getLogger("clamav_frontend")

#: 稼働状態のイベント（status）を確かめる間隔。clamd への問い合わせは別のスレッドで、より長い間隔で行う
_STATUS_INTERVAL_MS = 2000
_CLAMD_CHECK_INTERVAL_SEC = 60


def _share_with_users(paths) -> None:
    """user.token・endpoint.json を ClamQtPie Users・clamqtpie のグループに読ませる（トレイGUI がつなぐため。段階 12-4d）。
    できなくてもサービスは止めない（トレイGUI からつなげないだけ。clamqtpie-ctl・WebUI は使える）"""
    from core.service_access import AccessError, share_with_users

    try:
        share_with_users(paths.data_dir, paths.api_dir)
    except (AccessError, OSError) as exc:
        logger.warning(f"トークンをトレイGUI の利用者のグループに読ませられませんでした (detail: {exc})",
                       extra={"error_code": "E8100"})


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
        connect_notifications(self.events, runtime.notification_store)
        self._clamd_running: bool | None = None
        #: 定義の経過日数（clamd の VERSION 応答から。分からなければ None）と、古いと知らせたか
        self._definitions_age: int | None = None
        self._stale_notified = False
        self._stop = threading.Event()
        self._status = StatusPublisher(self.events, runtime.realtime_manager, runtime.scan_dispatcher,
                                       runtime.update_dispatcher, self.jobs)
        self._timer = QTimer()
        self._timer.timeout.connect(self._publish_status)
        self._clamd_thread = threading.Thread(target=self._watch_clamd, name="ClamdStatus", daemon=True)
        self.server: ApiServer | None = None
        self.web: WebController | None = None

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
            notification_service=rt.notification_service, notification_store=rt.notification_store,
            diagnostics_service=rt.diagnostics_service,
            apply_config=self._apply_config, start_clamd=rt.clamd_starter.start,
            clamd_controller=rt.clamd_controller,
            on_clamd_stop_requested=lambda: None, on_clamd_stop_failed=lambda: None,
            service_mode=True, requester_home=requester_home,
        )
        tokens = ApiTokens(rt.paths.api_dir)
        context.web = self.web = WebController(context, tokens, tls_dir=rt.paths.tls_dir)
        server = ApiServer(create_app(tokens, context), config.api.port, tokens=tokens)
        try:
            tokens.write_files()
            server.start()
            # 待ち受けたポートを知らせる（トレイGUI・clamqtpie-ctl が読む。止まっている間も残す。9.4節）
            write_endpoint(rt.paths.api_dir, server.port)
            _share_with_users(rt.paths)
        except (OSError, RuntimeError):
            tokens.remove_files()
            server.stop()
            raise
        self.server = server
        log_event(CATEGORY_SYSTEM, f"Web API の待ち受けを開始しました（{LOOPBACK_HOST}:{server.port}）")
        # WebUI（段階 12-5a）: ON なら待ち受ける。失敗してもサービスは止めない（OFF にして通知する。9.4節）
        self.web.check_timer()  # 止まっている間に OFF にする時刻を過ぎていれば、待ち受けずに OFF にする
        self.web.apply()
        self._timer.start(_STATUS_INTERVAL_MS)
        self._clamd_thread.start()
        return server

    def stop(self) -> None:
        self._stop.set()
        self._timer.stop()
        if self.web is not None:
            self.web.stop()
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
            self.check_clamd()
            self._stop.wait(_CLAMD_CHECK_INTERVAL_SEC)

    def check_clamd(self) -> None:
        """clamd の応答と定義の古さを確かめる（60 秒ごと、別のスレッドで）。定義が古くなった時点で1回知らせる"""
        rt = self.runtime
        try:
            self._clamd_running = bool(rt.clamav_service.ping(retry=False))
            if self._clamd_running:
                self._definitions_age = definitions_age_days(rt.clamav_service.get_version()[2])
        except Exception:  # noqa: BLE001 - 応答しない clamd は「停止」
            self._clamd_running = False
        if not self._clamd_running:
            return  # 応答しない間は定義の日付が分からないため、古さの判定を変えない
        stale = self._definitions_stale()
        if stale and not self._stale_notified:
            try:
                rt.notification_service.notify_definitions_stale(self._definitions_age, mail=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"定義が古いことを知らせられませんでした (detail: {exc})")
        self._stale_notified = stale

    def _definitions_stale(self) -> bool:
        age = self._definitions_age
        return age is not None and age > self.runtime.config_service.current.update.stale_days

    def _publish_status(self) -> None:
        web = getattr(self, "web", None)
        if web is not None:
            web.check_timer()  # 決めた時刻になったら WebUI を OFF にする（2 秒ごと。日時の比べだけ）
            web.check_tls()  # 1 日 1 回、HTTPS のサーバー証明書の名前・期限を確かめる
            web.check_lan()  # LAN から待ち受けていれば、1 分ごとに LAN のアドレスの変化を確かめる
        rt = self.runtime
        reachable = self._clamd_running
        self._status.on_status(SimpleNamespace(
            clamd_running=reachable, definitions_age_days=self._definitions_age if reachable else None,
            definitions_stale=bool(reachable) and self._definitions_stale(),
            realtime_running=rt.realtime_manager.is_running))
