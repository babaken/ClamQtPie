"""
トレイGUI（サービスのクライアント。サービス化・Web API設計書 10章。段階 12-4b）

一般の利用者の権限で動き、ClamQtPie サービスの内部 API だけを使って全画面を動かす。監視・スキャン・スケジュール・
定義の更新・隔離はサービスが行い、この画面は表示と操作の受け付けだけを行う。
- 起動時: user.token を読めない（ClamQtPie Users・clamqtpie グループに入っていない）なら案内して終わる。
  サービスにつながらないときは E8105 を表示し、トレイをエラーの状態にして、つながるまで試し続ける
- 多重起動防止はログオンのセッションごと（Local\\ のミューテックス。12章 42）
- admin の操作は、昇格ヘルパー（elevate_main.py）に頼む。UAC・pkexec と、ヘルパーの確認画面で確かめてから実行する
  （api_client/elevation.py。admin のトークンはこのプロセスに渡さない）
- 通知: サービスが保存した未読の通知をデスクトップに出し、トーストのクリックで開いた画面の通知・トレイの
  「未読の通知」の一覧で既読にする（gui/tray_notifications.py。設計書 7.2節）

従来のアプリ（app.py、1 プロセス）は段階 12-4e で削除する。
"""
from __future__ import annotations

import sys
from pathlib import Path

from core.lazy_import_fix import reify_concurrent_futures

reify_concurrent_futures()

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon  # noqa: E402

from api_client.client import ApiClient  # noqa: E402
from api_client.elevation import ElevatedCaller, ElevationRunner  # noqa: E402
from api_client.events import EventStream  # noqa: E402
from api_client.proxies import ServiceProxies  # noqa: E402
from api_client.sync import SyncCaller  # noqa: E402
from core.activation import ActivationWatcher, helper_command, register_protocol, user_state_dir  # noqa: E402
from core.autostart_service import AutostartService  # noqa: E402
from core.exceptions import AppError, ServicePermissionError  # noqa: E402
from core.logger import setup_logger  # noqa: E402
from core.service_paths import ServicePaths  # noqa: E402
from core.single_instance import TRAY_MUTEX_NAME, SingleInstanceLock  # noqa: E402
from core.version import APP_NAME  # noqa: E402
from gui.error_dialog import set_default_detail_handler, show_confirm_dialog  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402
from gui.notification_dialog import NotificationListDialog  # noqa: E402
from gui.theme import apply_theme  # noqa: E402
from gui.tray import TrayIcon, TrayIconState  # noqa: E402
from gui.tray_notifications import DesktopToaster, TrayNotifier  # noqa: E402

import logging  # noqa: E402

logger = logging.getLogger("clamav_frontend")

if getattr(sys, "frozen", False):
    _APP_DIR = Path(sys.executable).resolve().parent
    _RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", _APP_DIR))
else:
    _APP_DIR = Path(__file__).resolve().parent
    _RESOURCE_DIR = _APP_DIR

#: サービスにつながらないときに、つなぎ直す間隔
_RECONNECT_MS = 5_000
_STATUS_INTERVAL_MS = 60_000


class ClamdControllerProxy:
    """エンジン サービス画面の停止（ClamdServiceController の代わり）。サービスに頼む（admin）"""

    def __init__(self, proxies: ServiceProxies) -> None:
        self._proxies = proxies

    @property
    def service_name(self) -> str:
        return self._proxies.config.current.clamd.service_name or ("clamd" if sys.platform == "win32"
                                                                    else "clamav-daemon")

    def stop(self) -> None:
        self._proxies.api.call("POST", "/clamd/stop")


class DiagnosticsProxy:
    """診断（DiagnosticsService の代わり）。サービスが行い、伏せ字にした結果の文章を受け取る（admin）"""

    def __init__(self, proxies: ServiceProxies) -> None:
        self._proxies = proxies

    def run(self):
        from core.diagnostics_service import DiagnosticsReport, CheckResult, LEVEL_NG, LEVEL_OK
        from datetime import datetime

        data = self._proxies.api.call("POST", "/diagnostics")
        report = DiagnosticsReport(created_at=datetime.now())
        text = data.get("report", "")
        report.to_text = lambda: text  # サービスが作った文章をそのまま表示する
        report.checks.append(CheckResult(LEVEL_NG if data.get("has_problem") else LEVEL_OK,
                                         "問題があります" if data.get("has_problem") else "問題はありません"))
        return report


class TrayApp:
    def __init__(self, app: QApplication, paths: ServicePaths) -> None:
        self.app = app
        self.client = ApiClient(paths.api_dir)
        self.tray_icon = TrayIcon(_RESOURCE_DIR / "icons")
        self.window: MainWindow | None = None
        self.proxies: ServiceProxies | None = None
        self.notifier: TrayNotifier | None = None
        self._notification_dialog: NotificationListDialog | None = None
        self._activation: ActivationWatcher | None = None
        #: サービスにつながらないときのエラーコード（つながっている間は None）
        self._down_code: str | None = None
        self.elevation = ElevationRunner()
        self._reconnect = QTimer()
        self._reconnect.timeout.connect(self._retry)
        self._status_timer = QTimer()

    def shutdown(self) -> None:
        """タイマーとイベントの受信を止め、画面を閉じる（終了時・テストの後片づけ）"""
        self._reconnect.stop()
        self._status_timer.stop()
        if self.proxies is not None:
            self.proxies.events.stop()
        if self.notifier is not None:
            self.notifier.shutdown()
        self.elevation.shutdown()
        if self.window is not None:
            self.window.hide()
            self.window.deleteLater()
            self.window = None
        self.tray_icon.hide()

    # -- 接続 -----------------------------------------------------------------------------------
    def try_connect(self) -> bool:
        """サービスにつなぐ。つながったら画面を作る。権限が無ければ ServicePermissionError を送出する"""
        try:
            self.client.prepare()
            api = SyncCaller(self.client)
            version = api.call("GET", "/version")
            from api_client.client import API_VERSION

            if not isinstance(version, dict) or version.get("api_version") != API_VERSION:
                from core.exceptions import ServiceVersionMismatchError

                raise ServiceVersionMismatchError(detail=str(version)[:200])
        except ServicePermissionError:
            raise
        except AppError as exc:
            self._show_unreachable(exc)
            return False
        self._reconnect.stop()
        if self.window is None:
            events = EventStream(self.client)
            self.proxies = ServiceProxies(ElevatedCaller(api, self.elevation), events)
            notify_icon = _RESOURCE_DIR / "icons" / "app.png"
            self.notifier = TrayNotifier(api, events,
                                         DesktopToaster(str(notify_icon) if notify_icon.exists() else None))
            self._build_window()
            events.disconnected.connect(self._show_unreachable)
            events.connected.connect(self._on_connected)
            events.start()
        return True

    def _retry(self) -> None:
        """つなぎ直し（タイマー）。起動の後に権限が無くなった（グループから外された・トークンを作り直した）ときは、
        終わらずにエラーの状態のままつなぎ直す"""
        try:
            self.try_connect()
        except ServicePermissionError as exc:
            self._show_unreachable(exc)

    def _show_unreachable(self, error) -> None:
        code = getattr(error, "code", "E8105")
        if code != self._down_code:  # つなぎ直すたび（5 秒ごと）には残さない
            logger.warning(f"ClamQtPie サービスにつながりません ({code}: {getattr(error, 'detail', error)})")
        self._down_code = code
        self.tray_icon.set_state(TrayIconState.ERROR, True)
        self.tray_icon.set_warning(f"サービスにつながりません（{code}）")
        if self.window is not None:
            self.window.get_screen("home").set_service_down(code)
        if self.window is None and not self._reconnect.isActive():
            self._reconnect.start(_RECONNECT_MS)

    def _on_connected(self) -> None:
        if self._down_code is not None:
            logger.info("ClamQtPie サービスにつながりました")
        self._down_code = None
        self.tray_icon.set_warning(None)
        self.tray_icon.set_state(TrayIconState.ERROR, False)
        if self.window is not None:
            self.window.get_screen("home").set_service_down(None)
            self.window.get_screen("home").refresh_status()
        if self.notifier is not None:
            self.notifier.sync_pending()  # 止まっていた間・つながる前の通知

    def enable_activation(self) -> None:
        """トーストのクリックで画面を開く（Windows。core/activation.py。開いた画面の通知は既読にする）"""
        self._activation = ActivationWatcher(user_state_dir())
        register_protocol(helper_command(_APP_DIR, frozen=getattr(sys, "frozen", False),
                                         python_executable=sys.executable))
        self._activation.show_requested.connect(self.on_show_requested)

    def on_show_requested(self, key: str) -> None:
        if self.window is None:
            return
        logger.info(f"通知のクリックにより画面を表示します: {key}")
        self.window.navigate_to(key)
        self.window.show_from_tray()
        if self.notifier is not None:
            self.notifier.ack_screen(key)

    def show_notifications(self) -> None:
        """トレイの「未読の通知」"""
        if self.notifier is None:
            return
        if self._notification_dialog is None:
            self._notification_dialog = NotificationListDialog(self.notifier, self.window)
        self._notification_dialog.reload()
        self._notification_dialog.show()
        self._notification_dialog.raise_()
        self._notification_dialog.activateWindow()

    def _on_pending_changed(self, count: int) -> None:
        self.tray_icon.notifications_action.setText(f"未読の通知（{count} 件）" if count else "未読の通知")
        # メニューを開かなくても分かるよう、ツールチップにも出す
        self.tray_icon.set_note(f"未読の通知 {count} 件（メニューの「未読の通知」）" if count else None)

    # -- 画面 -----------------------------------------------------------------------------------
    def _build_window(self) -> None:
        p = self.proxies
        window = MainWindow(tray_icon=self.tray_icon, config_service=p.config)
        self.window = window
        set_default_detail_handler(window.show_log_for_error_code)

        scan_screen = window.get_screen("scan")
        quarantine_screen = window.get_screen("quarantine")
        realtime_screen = window.get_screen("realtime")
        update_screen = window.get_screen("update")
        home_screen = window.get_screen("home")
        engine_screen = window.get_screen("engine")

        scan_screen.bind_services(p.scan, p.quarantine)
        scan_screen.bind_history(p.scan_history)
        quarantine_screen.bind_services(p.quarantine)
        def action_operation(action: str):
            # 検知時アクションの保存（PUT /config）。ほかの設定は今の値のまま
            import copy

            model = copy.deepcopy(p.config.current)
            model.realtime.default_action = action
            return p.config.operation_for(model)

        # 除外・監視対象・スケジュールの変更は画面の中にためておき、［変更を保存］で 1 回の昇格にまとめる（段階 12-4c）
        realtime_screen.bind_services(p.realtime, p.watch_targets, p.excludes, batch=p.api.batch,
                                      config_operation=action_operation)

        def show_log_for(keyword: str) -> None:
            window.navigate_to("log")
            window.get_screen("log").filter_by_keyword(keyword)

        window.get_screen("schedule").bind_services(p.scheduler, on_show_log=show_log_for, batch=p.api.batch)
        update_screen.bind_services(p.update_service, p.update, p.clamav, config_service=p.config,
                                    on_open_settings=lambda: window.navigate_to("settings"))
        window.get_screen("settings").bind_services(
            p.config, p.notification, AutostartService(_autostart_command()),
            on_saved=lambda _config: (realtime_screen.sync_action_from_config(),
                                      update_screen.reload_auto_update_label()),
            service_mode=True,
        )
        window.get_screen("log").bind_services(p.log_reader)
        window.get_screen("diagnostics").bind_services(DiagnosticsProxy(p))
        window.get_screen("diagnostics").bind_service_info(lambda: service_info(p))

        def quick_scan() -> None:
            window.navigate_to("scan")
            window.show_from_tray()
            scan_screen.start_quick_scan()

        def update_now() -> None:
            window.navigate_to("update")
            window.show_from_tray()
            update_screen.start_update()

        def toggle_realtime() -> None:
            try:
                if p.realtime.is_running:
                    if not show_confirm_dialog(window, "監視停止の確認", "リアルタイム監視を停止しますか？"):
                        self.tray_icon.set_state(TrayIconState.MONITORING, True)
                        return
                    p.realtime.stop()
                else:
                    p.realtime.start()
            except AppError as exc:
                window.show_error(exc)
            self.tray_icon.set_state(TrayIconState.MONITORING, p.realtime.is_running)

        home_screen.bind_services(
            p.clamav, p.realtime, quick_scan, update_now, toggle_realtime, log_reader_service=p.log_reader,
            on_navigate=lambda key: (window.navigate_to(key), window.show_from_tray()),
            stale_days=lambda: p.config.current.update.stale_days,
        )

        def start_clamd() -> bool:
            try:
                p.api.call("POST", "/clamd/start")
            except AppError as exc:
                QTimer.singleShot(0, lambda error=exc: engine_screen.on_start_clamd_finished(False, error))
                return True
            QTimer.singleShot(3000, lambda: engine_screen.on_start_clamd_finished(True, None))
            return True

        engine_screen.bind_services(p.clamav, ClamdControllerProxy(p), on_start_clamd=start_clamd,
                                    on_refresh=home_screen.refresh_status, realtime_manager=p.realtime)

        def on_status(status) -> None:
            window.update_status_bar(status)
            engine_screen.apply_status(status)
            self.tray_icon.set_state(TrayIconState.ERROR, status.clamd_running is False)
            self.tray_icon.set_state(TrayIconState.MONITORING, status.realtime_running)
            if status.definitions_stale:
                self.tray_icon.set_warning(f"ウイルス定義が {status.definitions_age_days} 日更新されていません")
            elif status.clamd_running is not None:
                self.tray_icon.set_warning(None)
            realtime_screen.sync_toggle_state(status.realtime_running)

        home_screen.status_refreshed.connect(on_status)
        p.scan.finished.connect(lambda _summary: (home_screen.refresh_status(), scan_screen.reload_history()))
        p.update.finished.connect(lambda _record: home_screen.refresh_status())
        p.realtime.detected.connect(lambda _path, _virus: (
            self.tray_icon.set_state(TrayIconState.DETECTION_PENDING, True), home_screen.refresh_status()))
        p.scan.started.connect(lambda _label: scan_screen.show_started_elsewhere()
                               if not scan_screen.isVisible() else None)

        def on_event(name: str, _data) -> None:
            # サービスで変わった内容を画面に反映する（ほかのトレイGUI・WebUI・スケジュールが変えたもの）
            if name == "config.changed":
                window.get_screen("settings").reload_from_config()
                realtime_screen.sync_action_from_config()
            elif name == "status":
                home_screen.refresh_status(check_clamd=False)
            elif name == "schedule.finished":
                window.get_screen("schedule").reload()
                scan_screen.reload_history()

        p.events.event.connect(on_event)

        def on_screen_changed(key: str) -> None:
            if key == "quarantine":
                quarantine_screen.reload()
                if window.isVisible():
                    self.tray_icon.set_state(TrayIconState.DETECTION_PENDING, False)
            elif key == "settings":
                window.get_screen("settings").reload_from_config()
            elif key == "log":
                window.get_screen("log").on_shown()
            elif key == "scan":
                scan_screen.reload_history()
            elif key == "diagnostics":
                window.get_screen("diagnostics").refresh_service_info()

        window.screen_changed.connect(on_screen_changed)

        self.tray_icon.open_action.triggered.connect(window.show_from_tray)
        self.tray_icon.scan_now_action.triggered.connect(quick_scan)
        self.tray_icon.update_now_action.triggered.connect(update_now)
        self.tray_icon.toggle_realtime_action.triggered.connect(toggle_realtime)
        self.tray_icon.notifications_action.setVisible(True)
        self.tray_icon.notifications_action.triggered.connect(self.show_notifications)
        self.notifier.pending_changed.connect(self._on_pending_changed)

        self._status_timer.timeout.connect(home_screen.refresh_status)
        self._status_timer.start(_STATUS_INTERVAL_MS)
        home_screen.refresh_status()
        if not p.config.current.general.start_minimized:
            window.show()


def service_info(p: ServiceProxies) -> list[tuple[str, str, str]]:
    """診断画面の「サービスの状態」（別のスレッドで呼ばれる）"""
    from core.diagnostics_service import LEVEL_NG, LEVEL_OK, LEVEL_WARN
    from core.wake_timer import wake_timer_policy

    status = p.status.get()["clamd"]
    if not status.get("reachable"):
        clamd = ("clamd に接続できません（エンジン サービス画面・ログを確認してください）", LEVEL_NG)
    elif status.get("verified") is True:
        clamd = ("clamd であることを確かめてから接続しています", LEVEL_OK)
    else:
        clamd = ("clamd であることを確かめられません（別の PC の clamd か、ログに理由があります）", LEVEL_WARN)
    return [("clamd の接続先", *clamd), ("スリープから起こす設定", wake_timer_policy(), "")]


def _autostart_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve())]


def main() -> int:
    setup_logger(user_state_dir() / "logs", console=False)
    lock = SingleInstanceLock(mutex_name=TRAY_MUTEX_NAME)
    if not lock.acquire():
        logger.info("トレイGUI は既に動いています")
        return 0

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)
    apply_theme(app)
    app_icon = _RESOURCE_DIR / "icons" / "clamQtPie.ico"
    if app_icon.exists():
        app.setWindowIcon(QIcon(str(app_icon)))

    tray = TrayApp(app, ServicePaths.default())
    tray.enable_activation()
    tray.tray_icon.quit_action.triggered.connect(app.quit)
    tray.tray_icon.show()
    try:
        connected = tray.try_connect()
    except ServicePermissionError as exc:
        user = _current_user()
        logger.error(f"この利用者（{user}）は ClamQtPie を使う権限がないため、終了します ({exc.detail})",
                     extra={"error_code": exc.code})
        QMessageBox.critical(None, APP_NAME,
                             f"{exc.message}（{exc.code}）。\n\n管理者に、次のコマンドでこの利用者を追加してもらってください。\n"
                             f"clamqtpie-ctl users add {user}")
        lock.release()
        return 1
    if not connected:
        tray.tray_icon.showMessage(APP_NAME, "ClamQtPie サービスにつながりません。つながるまで待ちます",
                                   QSystemTrayIcon.MessageIcon.Warning)

    exit_code = app.exec()
    tray.shutdown()
    lock.release()
    return exit_code


def _current_user() -> str:
    import getpass

    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "<利用者名>"


if __name__ == "__main__":
    sys.exit(main())
