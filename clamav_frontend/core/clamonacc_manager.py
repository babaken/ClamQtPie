"""
ClamonaccRealtimeManager — Linuxリアルタイム監視（clamonacc連携）

参照: 要件定義書2章（Linux: clamonaccを利用しclamdと連携）、
      詳細設計書「リアルタイム監視」（Linux: clamonacc -> clamd -> ClamAVService -> GUI通知）
Phase 11でwatchdog実装（RealtimeManager）と同じインターフェースで実装した。

watchdog（inotify）との違い:
  clamonaccはfanotifyにより、ファイルが開かれる「前」にclamdで検査する（ClamAV公式の
  オンアクセススキャン）。clamd.confでOnAccessPrevention yesを指定すれば感染ファイルへの
  アクセス自体をブロックできる。ただしfanotifyのためroot権限が必要。

プロセス構成:
  GUIアプリは一般ユーザーで動作するため、pkexecでrootのシェルを1つ起動し、その子として
  clamonaccを動かす（認証ダイアログは開始時の1回のみ）。シェルはGUIアプリが保持する
  標準入力パイプのEOFを待ち、EOFになったらclamonaccを停止する。そのため停止時に再認証は
  不要で、GUIアプリがクラッシュした場合もパイプが閉じてclamonaccは自動停止する
  （rootプロセスの取り残しを防ぐ）。

検知の取得:
  clamonaccの--logに指定したファイルを末尾から読み、「<パス>: <ウイルス名> FOUND」行を
  検知として扱う。以降の検知時アクション（自動隔離／通知のみ）はwatchdog実装と共通。

既知の制限:
  - clamonaccの除外はフォルダ単位のみ（--exclude-list）。ファイル・拡張子の除外は
    検知後のアクション（隔離・通知）を行わない形でアプリ側で反映する（検査自体は行われる）
  - 「プロセス」除外は対象外（watchdog実装と同じ）
  - 監視対象の追加・削除はclamonaccの再起動で反映するため、再度認証を求められる
  - clamd.confでOnAccessPrevention yesの場合、感染ファイルを開けないため自動隔離に失敗する
    ことがある（その場合はclamonaccがアクセスを遮断しているため被害は防がれている）
  - clamd.confのOnAccessMaxFileSize（既定5MB）より大きいファイルは検査されない

clamonaccの仕様（ClamAV 1.5.4のソースで確認。Linuxリアルタイム監視詳細設計書11章）:
  - ローカルのclamdではOnAccessExcludeUname等が無いと起動を拒否する → 起動前に確認する
  - 監視の設定（除外・inotify/fanotifyの登録）に失敗しても、プロセスは終了せず監視なしで
    動き続ける → ログの該当行を検出して監視を停止し、E2005を通知する
  - clamdの一時フォルダ（TemporaryDirectory）を--watch-listに書くと無限ループする、
    /は無視される → 監視対象から外してE2006を通知する
  - OnAccessMountPath／OnAccessDisableDDDがあると--watch-list/--exclude-listは使われない
  - --fdpassはclamd.confにLocalSocketが無いと無視され、clamdがパスを開く方式になる
    （clamdのユーザーが読めないホームフォルダ等を検査できない）→ その場合は--streamを使う
"""
from __future__ import annotations

import logging
import os
import posixpath
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Signal

from core.config_model import RealtimeConfig
from core.clamd_conf import (
    DEFAULT_CLAMD_CONF_CANDIDATES,
    DEFAULT_TEMPORARY_DIRECTORY,
    ClamdConf,
    load_clamd_conf,
)
from core.exceptions import ClamonaccError, ClamonaccTargetError
from core.exclude_service import ExcludeService, is_under_any_folder
from core.logger import CATEGORY_REALTIME, log_event
from core.quarantine_service import QuarantineService
from core.watch_target_service import WatchTargetService

logger = logging.getLogger("clamav_frontend")

#: clamonaccのログ中の検知行。「Mon Oct 19 ... -> /path/to/file: Eicar-Signature FOUND」のように
#: 先頭に時刻等が付く場合があるため、最初の"/"からをパスとして扱う（パス内の": "にも耐える）。
#: clamonaccはLinux専用だが、テストをWindows上でも実行できるようドライブレター付きパスも許容する
_FOUND_RE = re.compile(r"(?P<path>(?:[A-Za-z]:)?[\\/].*): (?P<virus>\S+) FOUND\s*$")

#: rootシェルとして実行するスクリプト。$@ がclamonaccのコマンドライン。
#: 非対話シェルでは「&」で起動したコマンドの標準入力が/dev/nullになるため、
#: 元の標準入力をfd3へ複製してから監視用サブシェルに渡す。
_SUPERVISOR_SCRIPT = (
    'exec 3<&0; "$@" & pid=$!; '
    '( read _unused <&3; kill "$pid" 2>/dev/null ) & '
    'wait "$pid"; echo "__CLAMONACC_EXIT__ $?"'
)
_EXIT_MARKER = "__CLAMONACC_EXIT__"

#: pkexecの認証キャンセル・認証失敗時の終了コード
_PKEXEC_AUTH_FAILED_CODES = (126, 127)

#: clamonaccが監視を設定できなかったことを示すログ行（ClamAV 1.5.4のinotif.c・utils.c）。
#: これらの後もclamonaccのプロセスは終了せず、監視が設定されないまま動き続ける
#: （以前は画面上「監視中」のままになっていた。Arch Linux実機で発見）
_MONITORING_FAILED_MARKERS = (
    "ClamInotif: can't include",
    "ClamInotif: can't exclude",
    "ClamInotif: could not watch path",
    "ClamInotif: issue when adding watch",
    "ClamInotif: could not add element to hash table",
    "ClamInotif: failed to initialize DDD system",
    "ClamInotif: could not parse include list",
    "ClamInotif: could not parse exclude list",
    "ClamInotif: Not watching path",
    "ClamInotif: Please specify at least one path",
    "ClamMisc: could not open path list file",
)

#: 同じファイルの検知をまとめる時間（秒）。隔離時のSHA256計算でアプリ自身がファイルを開くと、
#: clamonaccがそれも検査して同じFOUND行を再度出力するため（通知の重複・隔離失敗のエラーを防ぐ）
_DUPLICATE_DETECTION_WINDOW_SEC = 10.0

_GLIBC_FTS_HINT = (
    "clamonaccがフォルダ構成を正しく読み取れていない可能性があります。"
    "新しいglibc（2.44で確認）では、clamonaccの不具合（ClamAV Issue #1797）でサブフォルダを認識できず、"
    "除外フォルダの指定やサブフォルダの監視に失敗します。"
    "config.jsonのrealtime.linux_backendを\"watchdog\"にすると、watchdog監視（変更後の検査）で回避できます"
)
_INOTIFY_LIMIT_HINT = (
    "inotifyの監視数の上限に達しました。"
    "「echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf && sudo sysctl -p」で上限を増やしてください"
)


def _exclude_user_hint(clamd_user: str) -> str:
    return (
        "clamd.confに「OnAccessExcludeUname " + clamd_user + "」を追加し、clamdを再起動してください"
        "（clamonaccは、clamd自身のファイルアクセスを検査対象から外す設定が無いと起動しません）"
    )


def _hint_for(text: str) -> str:
    """clamonaccの出力から、利用者向けの対処方法を返す（該当なしは空文字）"""
    if "must be specified" in text and "OnAccessExclude" in text:
        return _exclude_user_hint("clamav")
    if "No space left on device" in text:
        return _INOTIFY_LIMIT_HINT
    if any(
        marker in text
        for marker in ("can't exclude", "could not watch path", "issue when adding watch", "could not add element")
    ):
        return _GLIBC_FTS_HINT
    if "Not watching path" in text:
        return "clamdの一時フォルダ（clamd.confのTemporaryDirectory）は監視対象にできません"
    return ""


def find_clamonacc(config: RealtimeConfig, which: Callable[[str], str | None] = shutil.which) -> str | None:
    return which(config.clamonacc_path)


def is_clamonacc_available(
    config: RealtimeConfig, which: Callable[[str], str | None] = shutil.which
) -> tuple[bool, str]:
    """clamonacc方式が使えるか判定する。(使えるか, 使えない理由)"""
    if find_clamonacc(config, which) is None:
        return False, f"clamonaccが見つかりません ({config.clamonacc_path})"
    if not _is_root() and which("pkexec") is None:
        return False, "pkexecが見つかりません（clamonaccの実行にはroot権限が必要です）"
    return True, ""


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


class ClamonaccRealtimeManager(QObject):
    """RealtimeManager（watchdog実装）と同じインターフェースを持つclamonacc実装"""

    detected = Signal(str, str)  # path, virus_name（検知した時点。隔離の前）
    detection_handled = Signal(str, str, bool)  # path, virus_name, 隔離できたか（検知時アクションの後）
    watch_target_lost = Signal(str)  # path
    scan_error = Signal(object)  # AppError

    mode_name = "clamonacc"

    def __init__(
        self,
        watch_target_service: WatchTargetService,
        exclude_service: ExcludeService,
        quarantine_service: QuarantineService,
        config: RealtimeConfig,
        work_dir: Path,
        extra_exclude_dirs: list[Path] | None = None,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        which: Callable[[str], str | None] = shutil.which,
        log_poll_interval_sec: float = 0.5,
        clamd_conf_candidates: tuple[str, ...] = DEFAULT_CLAMD_CONF_CANDIDATES,
    ) -> None:
        """
        work_dir: watch-list/exclude-list/ログファイルを置くディレクトリ（ポータブル構成のアプリ配下）
        extra_exclude_dirs: 常に除外するフォルダ（アプリ自身のログ・隔離フォルダ等。自己検査の防止）
        popen, which, clamd_conf_candidates: テスト用の差し替え口
        """
        super().__init__()
        self._watch_target_service = watch_target_service
        self._exclude_service = exclude_service
        self._quarantine_service = quarantine_service
        self.config = config
        self._work_dir = work_dir
        self._extra_exclude_dirs = extra_exclude_dirs or []
        self._popen = popen
        self._which = which
        self._log_poll_interval_sec = log_poll_interval_sec
        self._clamd_conf_candidates = clamd_conf_candidates
        self._clamd_conf: ClamdConf | None = None
        self._recent_detections: dict[str, float] = {}

        self._process: subprocess.Popen | None = None
        # clamonaccの終了を検知済みか（rootシェル自体の終了より先に立てて、is_runningへ即反映する）
        self._exited = threading.Event()
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._stopping = False

    @property
    def watch_list_path(self) -> Path:
        return self._work_dir / "watch-list.txt"

    @property
    def exclude_list_path(self) -> Path:
        return self._work_dir / "exclude-list.txt"

    @property
    def log_path(self) -> Path:
        return self._work_dir / "clamonacc.log"

    @property
    def is_running(self) -> bool:
        return (
            self._process is not None and self._process.poll() is None and not self._exited.is_set()
        )

    # -- 開始・停止 -----------------------------------------------------
    def start(self) -> bool:
        """監視対象が0件の場合は開始しない（watchdog実装と同じ）"""
        if self.is_running:
            return True
        if self._process is not None:
            # 前回の監視（異常終了・監視設定の失敗）のスレッドが残っていれば片付ける
            self.stop()

        targets = [t for t in self._watch_target_service.list_enabled_paths() if self._check_target(t)]
        if not targets:
            return False

        clamonacc = find_clamonacc(self.config, self._which)
        if clamonacc is None:
            self._emit_error(f"clamonaccが見つかりません ({self.config.clamonacc_path})")
            return False

        # clamd.confの前提設定を確認する（不足していると認証後に失敗するため、認証の前に止める）
        self._clamd_conf = load_clamd_conf(self.config.clamonacc_config_file, self._clamd_conf_candidates)
        problem = self._check_clamd_conf(self._clamd_conf)
        if problem:
            self._emit_error(problem)
            return False
        targets, rejected = self._split_unwatchable(targets, self._clamd_conf)
        if not targets:
            self._emit_target_error(rejected)
            return False

        self._work_dir.mkdir(parents=True, exist_ok=True)
        self._write_lists(targets)
        # ログファイルは事前に一般ユーザー所有で作成しておく（root側は追記のみ行うため所有者が変わらず読める）
        self.log_path.touch(exist_ok=True)
        log_offset = self.log_path.stat().st_size

        command = self.build_command(clamonacc)
        logger.info(f"clamonaccを起動します: {' '.join(command)}")
        try:
            self._process = self._popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            self._process = None
            self._emit_error(f"clamonaccの起動に失敗しました: {exc}")
            return False

        self._stopping = False
        self._exited.clear()
        self._stop_event.clear()
        self._threads = [
            threading.Thread(target=self._tail_log, args=(log_offset,), daemon=True),
            threading.Thread(target=self._watch_process_output, daemon=True),
        ]
        for t in self._threads:
            t.start()
        log_event(CATEGORY_REALTIME, f"リアルタイム監視を開始しました（{self.mode_name}）: {', '.join(targets)}")
        if rejected:
            # 開始後に通知する（開始前に通知すると、画面側が「起動失敗」とみなしてトグルを戻すため）
            self._emit_target_error(rejected)
        return True

    def stop(self) -> None:
        if self.is_running:
            log_event(CATEGORY_REALTIME, f"リアルタイム監視を停止しました（{self.mode_name}）")
        self._stopping = True
        process = self._process
        if process is not None:
            # 標準入力を閉じるとrootシェルがclamonaccを停止して終了する（再認証不要）
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("clamonaccの停止待ちがタイムアウトしました")
        self._stop_event.set()
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=2)
        self._threads = []
        self._process = None

    # -- 監視対象の動的変更 ----------------------------------------------
    def add_target(self, path: str) -> None:
        self._restart_if_running()

    def remove_target(self, path: str) -> None:
        self._restart_if_running()

    def on_excludes_changed(self, exclude_type: str) -> None:
        """除外設定の変更通知。clamonaccが扱うのはフォルダ除外（--exclude-list）のみのため、
        フォルダ除外の変更時だけ再起動して反映する（ファイル・拡張子は検知後にアプリ側で照合）"""
        if exclude_type == "folder":
            self._restart_if_running()

    def _restart_if_running(self) -> None:
        # clamonaccは実行中に監視対象を変更できないため再起動で反映する（再度認証を求められる）
        if self.is_running:
            logger.info("監視対象の変更を反映するためclamonaccを再起動します")
            self.stop()
            self.start()

    # -- コマンド・設定ファイル生成 ------------------------------------------
    def build_command(self, clamonacc: str) -> list[str]:
        args = [
            clamonacc,
            "--foreground",
            f"--log={self.log_path}",
            f"--watch-list={self.watch_list_path}",
            f"--exclude-list={self.exclude_list_path}",
        ]
        if self.config.clamonacc_config_file:
            args.append(f"--config-file={self.config.clamonacc_config_file}")
        args.append("--fdpass" if self._use_fdpass() else "--stream")

        supervisor = ["/bin/sh", "-c", _SUPERVISOR_SCRIPT, "clamonacc-supervisor", *args]
        if _is_root():
            return supervisor
        return [self._which("pkexec") or "pkexec", *supervisor]

    def _use_fdpass(self) -> bool:
        """--fdpassはclamd.confにLocalSocketが無いと無視され、clamdがパスを開く方式になる。
        clamdのユーザー（clamav）は一般ユーザーのホームフォルダ（0700等）を読めず検査に失敗するため、
        fd渡しを使えない場合はファイルの内容を送る--streamにする"""
        if not self.config.clamonacc_fdpass:
            return False
        return self._clamd_conf is None or self._clamd_conf.has_local_socket

    def _check_clamd_conf(self, conf: ClamdConf | None) -> str:
        """clamonaccが起動できない・監視対象を使わない設定なら、その理由と対処を返す（問題なしは空文字）"""
        if conf is None:
            logger.info("clamd.confが見つからないため、clamonaccの前提設定の確認を省略します")
            return ""
        logger.info(
            f"clamd.conf: {conf.path}（OnAccessMaxFileSize {conf.get('OnAccessMaxFileSize') or '5M'}"
            "より大きいファイルはオンアクセススキャンされません）"
        )
        if conf.is_local_daemon and not conf.has_on_access_exclude_user:
            return f"{conf.path} にOnAccessExcludeUnameがありません。" + _exclude_user_hint(conf.get("User") or "clamav")
        mount_paths = conf.get_all("OnAccessMountPath")
        if mount_paths:
            log_event(
                CATEGORY_REALTIME,
                f"clamd.confのOnAccessMountPath（{', '.join(mount_paths)}）が有効なため、clamonaccはマウントポイント全体を"
                "監視します。アプリの監視対象・除外フォルダは使われず、OnAccessPreventionも働きません",
                level=logging.WARNING,
                error_code="E2005",
            )
        elif conf.is_enabled("OnAccessDisableDDD"):
            return (
                f"{conf.path} でOnAccessDisableDDDが有効なため、clamonaccはアプリの監視対象を使えません。"
                "OnAccessDisableDDDを削除（またはno）してください"
            )
        return ""

    def _split_unwatchable(self, targets: list[str], conf: ClamdConf | None) -> tuple[list[str], list[str]]:
        """clamonaccで監視できないフォルダを分ける。(監視するもの, 外したもの)
        - clamdの一時フォルダ: clamonaccが--watch-listの処理で無限ループする（ClamAV 1.5.4のinotif.c）
        - /: clamonaccが無視する（OnAccessMountPathでのみ監視できる）
        clamonaccはLinux専用のため、パスはPOSIXの規則で正規化する
        （os.path.normpathはWindowsで「/」をバックスラッシュに変えてしまい、Windowsのテストで判定できなかった）"""
        tmpdir = posixpath.normpath(conf.temporary_directory if conf else DEFAULT_TEMPORARY_DIRECTORY)
        watchable, rejected = [], []
        for target in targets:
            normalized = posixpath.normpath(target)
            (rejected if normalized in (tmpdir, "/") else watchable).append(target)
        return watchable, rejected

    def _emit_target_error(self, rejected: list[str]) -> None:
        detail = (
            f"{', '.join(rejected)} は監視できないため、監視対象から外しました"
            "（clamdの一時フォルダ（clamd.confのTemporaryDirectory）と/は、clamonaccの制限で監視できません）"
        )
        error = ClamonaccTargetError(detail=detail)
        log_event(CATEGORY_REALTIME, f"{error.message} ({detail})", level=logging.WARNING, error_code=error.code)
        self.scan_error.emit(error)

    def _write_lists(self, targets: list[str]) -> None:
        self.watch_list_path.write_text("\n".join(targets) + "\n", encoding="utf-8")

        exclude_dirs = [str(p) for p in self._extra_exclude_dirs]
        exclude_dirs += [
            r.value for r in self._exclude_service.list_all() if r.enabled and r.type == "folder"
        ]
        self.exclude_list_path.write_text(
            "".join(f"{d}\n" for d in dict.fromkeys(exclude_dirs)), encoding="utf-8"
        )

    def _check_target(self, path: str) -> bool:
        if Path(path).exists():
            return True
        log_event(CATEGORY_REALTIME, f"監視対象フォルダが見つからないため監視を停止しました: {path}", level=logging.WARNING, error_code="E2001")
        self.watch_target_lost.emit(path)
        self._watch_target_service.disable_by_path(path)
        return False

    # -- バックグラウンド処理 ---------------------------------------------
    def _tail_log(self, offset: int) -> None:
        """clamonaccのログを追記分だけ読み、検知行を処理する"""
        pending = ""
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                while not self._stop_event.is_set():
                    chunk = f.read()
                    if not chunk:
                        time.sleep(self._log_poll_interval_sec)
                        continue
                    pending += chunk
                    *lines, pending = pending.split("\n")  # 書き込み途中の最終行は次回へ持ち越す
                    for line in lines:
                        self._handle_log_line(line)
        except OSError as exc:
            self._emit_error(f"clamonaccのログを読み取れません: {exc}")

    def _handle_log_line(self, line: str) -> None:
        match = _FOUND_RE.search(line)
        if match is not None:
            self._handle_detection(match.group("path"), match.group("virus"))
        elif any(marker in line for marker in _MONITORING_FAILED_MARKERS):
            self._fail_monitoring(line.strip())
        elif "ERROR" in line:
            logger.warning(f"clamonacc: {line.strip()}", extra={"error_code": "E2005"})

    def _fail_monitoring(self, line: str) -> None:
        """clamonaccが監視を設定できなかった。プロセスは監視なしで動き続けるため停止して通知する"""
        logger.warning(f"clamonacc: {line}", extra={"error_code": "E2005"})
        if self._stopping or self._exited.is_set():
            return
        detail = f"clamonaccが監視を設定できませんでした: {line}"
        hint = _hint_for(line)
        if hint:
            detail += f"。{hint}"
        # 以降の終了マーカーを異常終了として二重に通知しないよう、停止中として扱う
        self._stopping = True
        self._exited.set()
        self._stop_event.set()
        process = self._process
        try:
            if process is not None and process.stdin is not None:
                process.stdin.close()  # rootシェルがclamonaccを停止する
        except OSError:
            pass
        self._emit_error(detail)

    def _watch_process_output(self) -> None:
        """rootシェルの出力を監視し、clamonaccの終了（異常終了・認証キャンセル）を検出する"""
        process = self._process
        if process is None or process.stdout is None:
            return
        exit_code: int | None = None
        output_tail: list[str] = []
        for line in process.stdout:
            line = line.rstrip()
            if line.startswith(_EXIT_MARKER):
                try:
                    exit_code = int(line.split()[1])
                except (IndexError, ValueError):
                    exit_code = -1
                break
            if line:
                output_tail = (output_tail + [line])[-5:]
                logger.info(f"clamonacc: {line}")

        if exit_code is None:
            # マーカー無しで終了 = pkexec段階で終了（認証キャンセル等）
            exit_code = process.wait()
            if exit_code in _PKEXEC_AUTH_FAILED_CODES:
                detail = "管理者認証がキャンセルされたか、失敗しました"
            else:
                detail = f"終了コード {exit_code}"
        else:
            detail = f"clamonaccが終了しました（終了コード {exit_code}）"

        self._exited.set()
        if self._stopping:
            return
        if output_tail:
            detail += " / " + " | ".join(output_tail)
            hint = _hint_for(" ".join(output_tail))
            if hint:
                detail += f"。{hint}"
        # clamonaccだけが終了してもrootシェルの監視サブシェルが残るため、標準入力を閉じて終わらせる
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        self._stop_event.set()
        self._emit_error(detail)

    # -- 検知時アクション（watchdog実装の6.3節と同じ） -----------------------
    def _handle_detection(self, path: str, virus_name: str) -> None:
        if is_under_any_folder(path, self._extra_exclude_dirs):
            # アプリ自身のフォルダは--exclude-listで外しているが、念のため検知後のアクションも行わない
            # （隔離フォルダのファイルを隔離し直すと、隔離が繰り返される）
            logger.info(f"アプリ自身のフォルダのため検知時アクションを行いません: {path} ({virus_name})")
            return
        now = time.monotonic()
        last = self._recent_detections.get(path)
        self._recent_detections = {
            p: t for p, t in self._recent_detections.items() if now - t < _DUPLICATE_DETECTION_WINDOW_SEC
        }
        if last is not None and now - last < _DUPLICATE_DETECTION_WINDOW_SEC:
            logger.info(f"同じファイルの検知が続いたため重複として扱います: {path} ({virus_name})")
            return
        self._recent_detections[path] = now
        # clamonaccの除外はフォルダ単位のみのため、ファイル・拡張子除外はここで反映する
        if self._exclude_service is not None and self._exclude_service.is_excluded(path):
            logger.info(f"除外対象のため検知時アクションを行いません: {path} ({virus_name})")
            return
        log_event(
            CATEGORY_REALTIME,
            f"ウイルスを検知しました: {path} ({virus_name}) [リアルタイム監視]",
            level=logging.WARNING,
        )
        self.detected.emit(path, virus_name)
        quarantined = False
        if self.config.default_action == "quarantine":
            try:
                self._quarantine_service.quarantine(path, virus_name)
                quarantined = True
            except Exception as exc:  # noqa: BLE001 - GUI通知のため広く捕捉
                logger.error(f"リアルタイム検知の自動隔離に失敗しました (detail: {exc})")
        # 通知は隔離を試した後の実際の結果で送る（RealtimeManagerと同じ）
        self.detection_handled.emit(path, virus_name, quarantined)

    def _emit_error(self, detail: str) -> None:
        error = ClamonaccError(detail=detail)
        logger.error(f"{error.message} (detail: {detail})", extra={"error_code": error.code})
        self.scan_error.emit(error)
