"""
UpdateService — freshclamによる定義DB更新と更新履歴管理

参照: 画面設計書6章、要件定義書、エラーコード一覧・例外設計書E8004
- freshclamをサブプロセスで実行し、標準出力をストリームでコールバック通知
- 実行結果をupdate_historyテーブルへ記録
- 成功時はClamAVService経由でclamdへRELOADを指示する

設計メモ: コア処理（update_now）は同期メソッドとして実装し、
SchedulerService（Phase6、APSchedulerのワーカースレッド内）からは
直接同期呼び出しする。GUI（定義更新画面）からはUIスレッドをブロック
しないよう、UpdateDispatcher（QThreadラッパー、ScanDispatcherと同様の
パターン）経由で呼び出す。
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from PySide6.QtCore import QObject, QThread, Signal

from core.clamav_service import ClamAVService
from core.exceptions import UnsafeExecutablePathError
from core.exec_path import (
    child_env,
    find_program,
    find_system_tool,
    system_tool,
    validate_executable,
    windows_clamav_dirs,
)
from core.logger import CATEGORY_UPDATE, log_event
from db.database import DatabaseService
from db.models import UpdateHistory

logger = logging.getLogger("clamav_frontend")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

_FRESHCLAM_NOT_FOUND_MESSAGE = "freshclamが見つかりません。ClamAVがインストールされているか確認してください"
#: Linuxで定義更新を行うOSのサービス（Debian/Ubuntu/Arch/Fedora共通の名前）
_FRESHCLAM_SERVICE = "clamav-freshclam"
_LINUX_SERVICE_HINT = (
    "Linuxでは定義更新をOSのサービスに任せてください"
    f"（sudo systemctl enable --now {_FRESHCLAM_SERVICE}.service）"
)
#: サービス稼働中に手動更新するときのログ出力先（サービスのログファイルのロックと競合させない）
_FRESHCLAM_LOG_OVERRIDE = "--log=/dev/null"
#: pkexecの認証キャンセル・認証失敗時の終了コード
_PKEXEC_AUTH_FAILED_CODES = (126, 127)
#: 失敗時の詳細に含めるfreshclamのエラー行の最大数
_MAX_ERROR_LINES = 3


@dataclass
class UpdateHistoryRecord:
    id: int
    executed_at: str
    result: str  # "success" | "failure" | "no_change" | "skipped"（skippedは履歴に記録しない）
    before_version: str
    after_version: str
    detail: str = ""  # 失敗・スキップの理由（DBには保存しない。画面・通知・ログ用）


def _to_record(row: UpdateHistory) -> UpdateHistoryRecord:
    return UpdateHistoryRecord(
        id=row.id,
        executed_at=row.executed_at,
        result=row.result,
        before_version=row.before_version,
        after_version=row.after_version,
    )


class UpdateService:
    """freshclam実行と履歴管理の同期コア処理"""

    def __init__(
        self,
        db: DatabaseService,
        clamav_service: ClamAVService,
        freshclam_path: str = "freshclam",
        system: str | None = None,
        which: Callable[[str], str | None] = shutil.which,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        validate_exec: Callable[..., str] = validate_executable,
    ) -> None:
        """system/which/runner/validate_exec: テスト用の差し替え口（OS判定・コマンド検索・systemctlの実行・
        実行ファイルの場所の検証）"""
        self.db = db
        self.clamav_service = clamav_service
        self.freshclam_path = freshclam_path
        self._system = system or platform.system()
        self._which = which
        self._runner = runner
        self._validate_exec = validate_exec

    def update_now(
        self, on_output: Callable[[str], None] | None = None, interactive: bool = True
    ) -> UpdateHistoryRecord:
        """freshclamを実行し、標準出力を1行ずつon_outputへ通知する（同期処理）

        参照: 画面設計書6章「実行ログ」欄へのストリーム表示
        interactive: 利用者の操作による実行か（定義更新画面・ホーム画面・トレイ）。Falseはスケジュール・自動更新。
          Linuxの一般ユーザーでは/var/lib/clamav等へ書き込めないため（Linux実機で発見）、
          - interactive=True: pkexec freshclam で管理者認証してから実行する
          - interactive=False: 無人実行で認証を求められないため実行しない。OSのclamav-freshclamサービスが
            動いていればそちらが更新するのでスキップ、動いていなければサービスの有効化を案内して失敗とする
        失敗時は例外を送出せず、result="failure"・detail（freshclamのエラー行等）を返す
        （以前はfreshclam未検出で例外を送出し、定義更新画面が実行中表示のまま止まっていた）。
        """
        before_version = self._safe_get_db_version()

        try:
            command, reason = self._freshclam_command(interactive)
        except UnsafeExecutablePathError as exc:
            # 管理者・rootの権限で実行するfreshclamが、一般ユーザーの書き換えられる場所にある（E6005）
            return self._fail(before_version, f"{exc.message} ({exc.detail})")
        if command is None:
            if reason == "not_found":
                return self._fail(before_version, f"{_FRESHCLAM_NOT_FOUND_MESSAGE} ({self.freshclam_path})")
            if reason == "service":
                message = f"定義DBの更新はOSの{_FRESHCLAM_SERVICE}サービスが行っているため、アプリからの自動更新は行いません"
                log_event(CATEGORY_UPDATE, message)
                return UpdateHistoryRecord(
                    0, _now_iso(), "skipped", before_version, before_version, detail=message
                )
            return self._fail(before_version, f"一般ユーザーでは自動更新を実行できません。{_LINUX_SERVICE_HINT}")

        log_event(CATEGORY_UPDATE, f"定義DBの更新を開始しました（現在のバージョン: {before_version or '不明'}）")
        try:
            process = subprocess.Popen(
                command,
                # pkexecが端末からパスワードを読もうとして止まらないよう、標準入力は渡さない
                # （GUIでは認証ダイアログ（polkitの認証エージェント）で認証する）
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                # Windowsでは管理者権限のfreshclamに、利用者が変えられる環境変数を引き継がない
                env=child_env(system=self._system),
            )
        except OSError as exc:
            return self._fail(before_version, f"{_FRESHCLAM_NOT_FOUND_MESSAGE} ({exc})")

        assert process.stdout is not None
        error_lines: list[str] = []
        for line in process.stdout:
            line = line.rstrip("\n")
            if "ERROR" in line or "WARNING" in line:
                error_lines = (error_lines + [line.strip()])[-_MAX_ERROR_LINES:]
            if on_output is not None:
                on_output(line)
        return_code = process.wait()

        if return_code != 0:
            if command[0] != self.freshclam_path and return_code in _PKEXEC_AUTH_FAILED_CODES and not error_lines:
                detail = "管理者認証がキャンセルされたか、失敗しました"
            else:
                detail = f"freshclamの終了コード {return_code}"
                if error_lines:
                    detail += ": " + " / ".join(error_lines)
                if self._is_linux_user() and command[0] == self.freshclam_path:
                    detail += f"。{_LINUX_SERVICE_HINT}"
            return self._fail(before_version, detail)

        # 成功時はclamdへ再読込を指示する（clamdソケット通信プロトコル設計書のRELOAD）
        try:
            self.clamav_service.update_database()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"clamdへの再読込指示に失敗しました (detail: {exc})")

        after_version = self._safe_get_db_version()
        result = "no_change" if after_version == before_version else "success"
        if result == "success":
            log_event(CATEGORY_UPDATE, f"定義DBを更新しました: {before_version} → {after_version}")
        else:
            log_event(CATEGORY_UPDATE, f"定義DBは最新です（バージョン: {after_version}）")
        return self._record_history(result, before_version, after_version)

    def _is_linux_user(self) -> bool:
        return self._system == "Linux" and not (hasattr(os, "geteuid") and os.geteuid() == 0)

    def _freshclam_service_active(self) -> bool:
        try:
            completed = self._runner(
                [system_tool("systemctl", self._system), "is-active", "--quiet", f"{_FRESHCLAM_SERVICE}.service"],
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0

    def _freshclam_command(self, interactive: bool) -> tuple[list[str] | None, str]:
        """実行するコマンドを決める。実行しない場合は(None, 理由)。理由: "service" | "no_permission" | "not_found" """
        if self._system != "Linux":
            # 名前だけの場合も相対名では起動しない（WindowsのCreateProcessはexeのフォルダやカレント
            # ディレクトリからも探すため）。ClamAVの導入先→PATHの絶対パスの項目の順に探す（core/exec_path.py）
            freshclam = find_program(self.freshclam_path, windows_clamav_dirs(), system=self._system)
            if freshclam is None and os.path.isabs(self.freshclam_path):
                freshclam = self.freshclam_path  # 絶対パスの指定はそのまま起動し、無ければ起動の失敗として知らせる
            if freshclam is None or not os.path.isfile(freshclam):
                return None, "not_found"
            # アプリは管理者権限で動くため、管理者だけが書き込める場所にあるfreshclamだけを実行する
            return [self._validate_exec(freshclam, ("freshclam",), self._system)], ""
        service_active = self._freshclam_service_active()
        if self._is_linux_user() and not interactive:
            return None, "service" if service_active else "no_permission"
        # clamav-freshclamサービス（freshclam -d）がログファイルをロックしているため、
        # 同じログファイルでは「Failed to lock the log file」で失敗する（Arch Linux実機で発見）。
        # ログは別ファイルにする（出力はアプリのログ・画面に残る）
        log_args = [_FRESHCLAM_LOG_OVERRIDE] if service_active else []
        if not self._is_linux_user():
            return [self._trusted_freshclam(), *log_args], ""  # アプリ自体がrootで動いている
        # 昇格に使うpkexecは、PATH（利用者が変えられる）からは探さず決まった場所だけを見る
        pkexec = find_system_tool("pkexec", self._system)
        if pkexec is None:
            return [self.freshclam_path, *log_args], ""  # 失敗時の詳細でサービスの有効化を案内する
        # pkexecは実行ファイルのフルパスが必要。rootで実行するため、root以外が書き換えられる場所のものは実行しない
        return [pkexec, self._trusted_freshclam(), *log_args], ""

    def _trusted_freshclam(self) -> str:
        """rootで実行するfreshclamの絶対パス（場所を検証したもの）。問題があればUnsafeExecutablePathError"""
        path = self._which(self.freshclam_path) or self.freshclam_path
        return self._validate_exec(path, ("freshclam",), self._system)

    def _fail(self, before_version: str, detail: str) -> UpdateHistoryRecord:
        record = self._record_history("failure", before_version, before_version)
        record.detail = detail
        log_event(
            CATEGORY_UPDATE,
            f"定義DBの更新に失敗しました ({detail})",
            level=logging.ERROR,
            error_code="E8004",
        )
        return record

    def _safe_get_db_version(self) -> str:
        try:
            return self.clamav_service.get_database_version()
        except Exception:  # noqa: BLE001 - clamd未起動時等は空文字扱い
            return ""

    def _record_history(self, result: str, before_version: str, after_version: str) -> UpdateHistoryRecord:
        with self.db.session() as session:
            row = UpdateHistory(
                executed_at=_now_iso(),
                result=result,
                before_version=before_version,
                after_version=after_version,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return _to_record(row)

    def last_executed_at(self) -> datetime | None:
        """最後に定義更新を実行した日時（自動更新の初回の時刻を決めるため。SchedulerService.set_auto_update）

        手動・自動・スケジュールのどれでも、失敗も含めて最後に実行した日時。履歴が無い・読めない場合はNone。
        """
        with self.db.session() as session:
            row = session.query(UpdateHistory).order_by(UpdateHistory.executed_at.desc()).first()
            executed_at = row.executed_at if row is not None else None
        if not executed_at:
            return None
        try:
            value = datetime.fromisoformat(executed_at)
        except ValueError:
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    def list_history(self) -> list[UpdateHistoryRecord]:
        with self.db.session() as session:
            rows = session.query(UpdateHistory).order_by(UpdateHistory.executed_at.desc()).all()
            return [_to_record(r) for r in rows]


class _UpdateWorker(QObject):
    log_line = Signal(str)
    finished = Signal(object)  # UpdateHistoryRecord

    def __init__(self, update_service: UpdateService) -> None:
        super().__init__()
        self._update_service = update_service

    def run(self) -> None:
        # 利用者の操作による実行（Linuxの一般ユーザーではpkexecで認証して実行する）
        record = self._update_service.update_now(on_output=self.log_line.emit, interactive=True)
        self.finished.emit(record)


class UpdateDispatcher(QObject):
    """GUI（定義更新画面）用のQThreadラッパー（ScanDispatcherと同様のパターン）"""

    log_line = Signal(str)
    finished = Signal(object)  # UpdateHistoryRecord

    def __init__(self, update_service: UpdateService) -> None:
        super().__init__()
        self._update_service = update_service
        self._thread: QThread | None = None
        self._worker: _UpdateWorker | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self) -> None:
        if self.is_running:
            return
        self._thread = QThread()
        self._worker = _UpdateWorker(self._update_service)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.log_line.connect(self.log_line)
        self._worker.finished.connect(self._on_finished)

        self._thread.start()

    def _on_finished(self, record: UpdateHistoryRecord) -> None:
        self.finished.emit(record)
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
        self._thread = None
        self._worker = None
