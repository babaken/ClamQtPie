"""
ClamQtPie サービスの入口（サービス化・Web API設計書 9章。段階 12-3a）

画面を持たず、監視・スキャン・スケジュール・定義の更新・隔離と内部 API を動かす。

使い方:
    python service_main.py --console [--data-dir DIR]   前面で動かす（開発中の確認用。登録は要らない）
    python service_main.py --systemd                    systemd から起動する（Linux。unit は Type=notify）
    python service_main.py install|remove|start|stop    Windows サービスの登録・削除・開始・停止（管理者で）

- 起動の完了は、内部 API が待ち受けを始めてから OS に伝える（Windows は SERVICE_RUNNING、Linux は READY=1）
- API のポートを使えなければ、起動を失敗にする（9.4節）
- サービスとして動くとき（--systemd・Windows サービス）は、データの置き場所の権限を確かめ、一般の利用者が
  書き込めるようになっていれば起動しない（core/service_paths.py）
- 二重起動の防止は SCM・systemd に任せる。--console のときだけ、データの置き場所のロックで防ぐ
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from core.lazy_import_fix import reify_concurrent_futures

reify_concurrent_futures()

from PySide6.QtCore import QCoreApplication, QLockFile, QMetaObject, Qt, QTimer  # noqa: E402

from core.logger import CATEGORY_SYSTEM, log_event  # noqa: E402
from core.service_paths import ServicePaths, UnsafeDataDirectoryError, prepare_directories  # noqa: E402
from core.systemd_notify import notify as sd_notify  # noqa: E402
from core.version import APP_ID, APP_NAME, APP_VERSION  # noqa: E402

logger = logging.getLogger("clamav_frontend")

#: Windows サービスの名前（設計書 9章）
SERVICE_NAME = APP_ID
SERVICE_DISPLAY_NAME = f"{APP_NAME} サービス"

#: 終了コード
EXIT_OK = 0
EXIT_UNSAFE_DIRECTORY = 2
EXIT_API_FAILED = 3
EXIT_ALREADY_RUNNING = 4


def run(paths: ServicePaths, strict: bool, on_ready=None, app_holder: list | None = None) -> int:
    """サービスを動かし、止められるまで戻らない。on_ready: 起動を完了したときに呼ぶ（OS への通知）
    app_holder: 作った QCoreApplication を入れる（Windows サービスの停止の要求で止めるため）"""
    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    if app_holder is not None:
        app_holder.append(app)
    try:
        prepare_directories(paths, strict=strict)
    except UnsafeDataDirectoryError as exc:
        # ロガーの準備の前なので、標準エラー（systemd のジャーナル・Windows のイベントログ）に出す
        print(f"ClamQtPie サービスを起動できません: {exc}", file=sys.stderr)
        return EXIT_UNSAFE_DIRECTORY
    if strict and getattr(sys, "frozen", False):
        # 自分の実行ファイルを一般の利用者が差し替えられると、次の起動から SYSTEM・root で動いてしまう（12章 33）
        from core.service_access import program_location_problem

        problem = program_location_problem(Path(sys.executable))
        if problem:
            print(f"ClamQtPie サービスを起動できません: 実行ファイルの置き場所を一般の利用者が書き換えられます（{problem}）",
                  file=sys.stderr)
            return EXIT_UNSAFE_DIRECTORY

    from api.service_api import ServiceApi
    from core.runtime import build

    runtime = build(paths)
    api = ServiceApi(runtime)
    try:
        api.start()
    except (OSError, RuntimeError) as exc:
        logger.error(f"Web API の待ち受けを開始できないため、サービスを起動できません (detail: {exc})",
                     extra={"error_code": "E8100"})
        runtime.shutdown()
        return EXIT_API_FAILED

    log_event(CATEGORY_SYSTEM, f"サービスを開始しました（{APP_NAME} {APP_VERSION}）")
    if on_ready is not None:
        on_ready()
    exit_code = app.exec()
    logger.info("サービスの終了シーケンス開始")
    api.stop()
    runtime.shutdown()
    log_event(CATEGORY_SYSTEM, f"サービスを停止しました（終了コード: {exit_code}）")
    return EXIT_OK


def _install_signal_handlers(app_holder: list) -> QTimer:
    """SIGTERM・SIGINT で、終了の処理（トークンファイルの削除など）を通って止まる"""
    def request_quit(_signum, _frame):
        if app_holder:
            app_holder[0].quit()

    signal.signal(signal.SIGTERM, request_quit)
    signal.signal(signal.SIGINT, request_quit)
    # Qt のイベントループの間も Python のシグナルの処理が動くよう、定期的に Python に制御を戻す
    timer = QTimer()
    timer.timeout.connect(lambda: None)
    timer.start(500)
    return timer


def _run_console(data_dir: str | None) -> int:
    paths = ServicePaths.under(data_dir) if data_dir else ServicePaths.default()
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(paths.data_dir / "service.lock"))
    if not lock.tryLock(0):
        print("ClamQtPie サービスは既に動いています", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    holder: list = []
    app = QCoreApplication(sys.argv[:1])
    holder.append(app)
    keep = _install_signal_handlers(holder)  # noqa: F841 - タイマーの参照を持っておく
    try:
        # 開発中の確認では、置き場所の権限の問題は警告だけにする
        return run(paths, strict=False)
    finally:
        lock.unlock()


def _run_systemd() -> int:
    holder: list = []
    app = QCoreApplication(sys.argv[:1])
    holder.append(app)
    keep = _install_signal_handlers(holder)  # noqa: F841
    code = run(ServicePaths.default(), strict=True, on_ready=lambda: sd_notify("READY=1"))
    sd_notify("STOPPING=1")
    return code


# -- Windows サービス ------------------------------------------------------------------------
# クラスはモジュールの直下に置く（pywin32 は「モジュール名.クラス名」を登録し、サービスの起動時に
# pythonservice.exe がそこから読み込むため。関数の中で作ると登録できない）
if sys.platform == "win32":
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    class ClamQtPieService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = "ClamAV を使ったウイルス対策（リアルタイム監視・スキャン・スケジュール・隔離）"

        def __init__(self, args):
            super().__init__(args)
            self._app_holder: list = []
            self._stopped = win32event.CreateEvent(None, 0, 0, None)

        def SvcStop(self):  # noqa: N802 - pywin32 の名前
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            if self._app_holder:
                # 別のスレッドから呼ばれるため、イベントループのスレッドで quit させる
                QMetaObject.invokeMethod(self._app_holder[0], "quit", Qt.ConnectionType.QueuedConnection)
            win32event.SetEvent(self._stopped)

        def SvcDoRun(self):  # noqa: N802
            servicemanager.LogInfoMsg(f"{SERVICE_NAME}: 起動します")
            # 内部 API が待ち受けを始めるまでは START_PENDING のまま（pywin32 が既に報告している）
            code = run(ServicePaths.default(), strict=True,
                       on_ready=lambda: self.ReportServiceStatus(win32service.SERVICE_RUNNING),
                       app_holder=self._app_holder)
            if code != EXIT_OK:
                servicemanager.LogErrorMsg(f"{SERVICE_NAME}: 起動に失敗しました（終了コード {code}）")
                # 失敗を SCM に伝え、回復動作（再起動）の対象にする
                self.ReportServiceStatus(win32service.SERVICE_STOPPED, win32ExitCode=1066,
                                         svcExitCode=code)


#: Windows サービスの登録・操作の命令（pywin32 の HandleCommandLine に渡す。--startup などのオプションは前に付く）
_WINDOWS_COMMANDS = ("install", "remove", "start", "stop", "restart", "update", "debug")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if sys.platform == "win32" and (not argv or any(arg in _WINDOWS_COMMANDS for arg in argv)):
        service_class = ClamQtPieService
        if not argv and getattr(sys, "frozen", False):
            # SCM から起動された（配布版）
            servicemanager.Initialize()
            servicemanager.PrepareToHostSingle(service_class)
            servicemanager.StartServiceCtrlDispatcher()
            return EXIT_OK
        if argv:
            win32serviceutil.HandleCommandLine(service_class, argv=[sys.argv[0], *argv])
            return EXIT_OK

    parser = argparse.ArgumentParser(description=f"{APP_NAME} サービス")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--console", action="store_true", help="前面で動かす（開発中の確認用）")
    mode.add_argument("--systemd", action="store_true", help="systemd から起動する（Linux）")
    parser.add_argument("--data-dir", help="--console のとき、設定・データ・ログをこのフォルダの下に置く")
    args = parser.parse_args(argv)
    if args.systemd:
        return _run_systemd()
    return _run_console(args.data_dir)


if __name__ == "__main__":
    sys.exit(main())
