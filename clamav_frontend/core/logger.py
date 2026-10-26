"""
ロギング基盤

参照: エラーコード一覧・例外設計書5.2節、非機能要件詳細5章（ログ保持期間）
- TimedRotatingFileHandlerで日次ローテーション
- AppError発生時はエラーコードをログに含める（[E1001]形式）
- retention_days（config.logging.retention_days）に応じて古いログを削除
- ログ画面の「ログをクリア」（clear_logs）
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import stat
from pathlib import Path

from core.exceptions import AppError

_LOG_FORMAT = "%(asctime)s [%(levelname)s]%(error_code_tag)s%(category_tag)s %(message)s"

#: ログファイルの権限（所有者のみ読み書き）。ログには検知ファイルのパス・ウイルス名・監視対象等が
#: 平文で記録されるため、マルチユーザー環境で他ユーザーに読まれないようにする（セキュリティレビューで発見）
_LOG_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0o600
_LOG_DIR_MODE = stat.S_IRWXU  # 0o700


class _OwnerOnlyTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """ログファイルを開くたびに所有者のみ読み書き可（0600）にするハンドラ。

    初回の作成時とローテーション後の新ファイル作成時の両方で効く。ローテーションで退避される
    過去ログ（app.log.<日付>）は0600の現ログをrenameしたものなので、同じく0600になる。
    """

    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, _LOG_FILE_MODE)
        except OSError:
            pass
        return stream


def _restrict_log_permissions(log_dir: Path) -> None:
    """ログのフォルダを0700、既存のログファイル（app.log*）を0600にする（POSIXのみ、best-effort）"""
    if platform.system() == "Windows":
        return
    try:
        os.chmod(log_dir, _LOG_DIR_MODE)
    except OSError:
        pass
    for path in log_dir.glob("app.log*"):
        try:
            os.chmod(path, _LOG_FILE_MODE)
        except OSError:
            pass

#: ログ画面のカテゴリ（画面設計書7章: スキャン／リアルタイム／更新／隔離／システム）
CATEGORY_SCAN = "スキャン"
CATEGORY_REALTIME = "リアルタイム"
CATEGORY_UPDATE = "更新"
CATEGORY_QUARANTINE = "隔離"
CATEGORY_SYSTEM = "システム"


class _ErrorCodeFilter(logging.Filter):
    """エラーコード・カテゴリをログレコードへ付与するフィルタ

    extra={"error_code": "E1001"} → " [E1001]"、extra={"category": "スキャン"} → " <スキャン>"。
    カテゴリ未指定の行はログ画面側でエラーコード帯から推定する（log_reader_service）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # 元の属性（error_code/category）は書き換えず、表示用の属性に整形する。
        # 以前はerror_codeを上書きしていたため、出力先（ファイル・コンソール）ごとにフィルタが通ると
        # 2つ目の出力先で「[ [E8004]]」と二重に装飾されていた（Linux実機で発見）
        code = getattr(record, "error_code", None)
        record.error_code_tag = f" [{code}]" if code else ""
        category = getattr(record, "category", None)
        record.category_tag = f" <{category}>" if category else ""
        return True


def setup_logger(
    log_dir: Path,
    level: str = "INFO",
    retention_days: int = 90,
    logger_name: str = "clamav_frontend",
) -> logging.Logger:
    """アプリ共通ロガーを初期化する。

    log_dir: ログファイルの出力先ディレクトリ（インストーラ/パッケージング設計書2.1節の logs/ を想定）
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    # フォルダを0700・既存ログを0600に制限してから書き込む（世界から読める瞬間を減らす）
    _restrict_log_permissions(log_dir)
    logger = logging.getLogger(logger_name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    # 設定画面での保存時にも呼ばれるため、既存の出力先は閉じてから入れ替える（ファイルを開いたままにしない）
    for old_handler in list(logger.handlers):
        logger.removeHandler(old_handler)
        old_handler.close()
    # ルートロガー（Alembicのlogging設定でコンソール出力が付く）へ伝播させない。
    # 伝播すると同じ行がコンソールに2回表示されていた（Linux実機で発見）
    logger.propagate = False

    handler = _OwnerOnlyTimedRotatingFileHandler(
        log_dir / "app.log",
        when="midnight",
        backupCount=max(retention_days, 1),
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handler.addFilter(_ErrorCodeFilter())
    logger.addHandler(handler)

    # コンソール出力（開発時の確認用。配布物ではconsole=Falseのため実害なし）
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_LOG_FORMAT))
    console.addFilter(_ErrorCodeFilter())
    logger.addHandler(console)

    return logger


def clear_logs(log_dir: Path, logger_name: str = "clamav_frontend") -> int:
    """アプリのログ（app.logと日付付きの過去のファイル）をすべて消去する（ログ画面の「ログをクリア」）

    app.logはアプリが書き込み中のため、削除せず中身を空にする（Windowsでは開いているファイルを
    削除できない）。日付付きの過去のファイル（app.log.YYYY-MM-DD）は削除する。
    消去の後、いつログを消したかが分かるよう「ログをクリアしました」を記録する。
    戻り値: 削除した過去のファイルの数
    """
    current = log_dir / "app.log"
    logger = logging.getLogger(logger_name)
    handler = next(
        (
            h
            for h in logger.handlers
            if isinstance(h, logging.FileHandler) and h.baseFilename == os.path.abspath(current)
        ),
        None,
    )
    if handler is not None and handler.stream is not None:
        # 書き込みと競合しないよう出力先のロックを取ってから空にする
        handler.acquire()
        try:
            handler.stream.flush()
            handler.stream.seek(0)
            handler.stream.truncate()
        finally:
            handler.release()
    elif current.exists():
        current.write_text("", encoding="utf-8")

    removed = 0
    for old in sorted(log_dir.glob("app.log.*")):
        try:
            old.unlink()
            removed += 1
        except OSError as exc:
            logger.warning(f"過去のログファイルを削除できませんでした: {old} (detail: {exc})")

    log_event(CATEGORY_SYSTEM, f"ログをクリアしました（過去のログファイル {removed}件を削除）", logger_name=logger_name)
    return removed


def log_event(
    category: str,
    message: str,
    level: int = logging.INFO,
    error_code: str | None = None,
    logger_name: str = "clamav_frontend",
) -> None:
    """カテゴリ付きの業務ログ（検知・スキャン・隔離・更新など）を出力する

    詳細設計書のシーケンス図（ClamAVService -> Logger : write）、通知機能詳細仕様書6章
    （Logger: 検知ログ記録）に対応。ログ画面のカテゴリ列・フィルタに使われる。
    """
    extra = {"category": category}
    if error_code:
        extra["error_code"] = error_code
    logging.getLogger(logger_name).log(level, message, extra=extra)


def log_app_error(logger: logging.Logger, error: AppError, level: int = logging.ERROR) -> None:
    """AppErrorをエラーコード付きでログ出力する共通ヘルパー

    参照: エラーコード一覧・例外設計書6章「下位ライブラリの例外を
    アプリ例外へ変換してから上位に伝播させる」方針に対応。
    """
    extra = {"error_code": error.code}
    detail = f" (detail: {error.detail})" if error.detail else ""
    logger.log(level, f"{error.message}{detail}", extra=extra)
