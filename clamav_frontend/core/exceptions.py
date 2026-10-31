"""
アプリケーション共通例外階層

参照: エラーコード一覧・例外設計書 2章・3章
- 全アプリ例外は AppError を継承する
- code / message / detail の3属性を持つ
- モジュールごとにエラーコード帯を割り当てる（3章）
"""
from __future__ import annotations


class AppError(Exception):
    """全アプリ例外の基底クラス"""

    #: サブクラスで既定コードを上書きする（例: "E1001"）
    default_code: str = "E9001"
    #: サブクラスで既定のGUI表示用メッセージを上書きする
    default_message: str = "予期しないエラーが発生しました"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.code = code or self.default_code
        self.message = message or self.default_message
        self.detail = detail
        super().__init__(f"[{self.code}] {self.message}")

    def __repr__(self) -> str:  # pragma: no cover - デバッグ補助
        return f"{self.__class__.__name__}(code={self.code!r}, message={self.message!r})"


# ---------------------------------------------------------------------------
# E1000番台: clamd通信（clamdソケット通信プロトコル設計書6章）
# ---------------------------------------------------------------------------
class ClamdError(AppError):
    default_code = "E1000"
    default_message = "ClamAVサービスとの通信でエラーが発生しました"


class ClamdConnectionError(ClamdError):
    default_code = "E1001"
    default_message = "ClamAVサービス（clamd）に接続できません"


class ClamdTimeoutError(ClamdError):
    default_code = "E1002"
    default_message = "ClamAVサービスからの応答がありません"


class ClamdProtocolError(ClamdError):
    default_code = "E1003"
    default_message = "ClamAVサービスの応答を解釈できませんでした"


class ClamdStreamSizeError(ClamdError):
    default_code = "E1004"
    default_message = "ファイルサイズが上限を超えているためスキャンできません"


# ---------------------------------------------------------------------------
# E2000番台: リアルタイム監視（Windowsリアルタイム監視詳細設計書9章）
# ---------------------------------------------------------------------------
class RealtimeError(AppError):
    default_code = "E2000"
    default_message = "リアルタイム監視でエラーが発生しました"


class WatchTargetLostError(RealtimeError):
    default_code = "E2001"
    default_message = "監視対象フォルダが見つかりません"


class WatchBufferOverflowError(RealtimeError):
    default_code = "E2002"
    default_message = "リアルタイム監視のイベント処理が追いつきませんでした"


class ClamonaccError(RealtimeError):
    """Linuxのclamonacc（オンアクセススキャン）の起動失敗・異常終了"""

    default_code = "E2005"
    default_message = "clamonacc（オンアクセススキャン）を実行できませんでした"


class ClamonaccTargetError(RealtimeError):
    """Linux: clamonaccの制限で監視できないフォルダ（clamdの一時フォルダ・/）を監視対象から外した"""

    default_code = "E2006"
    default_message = "clamonaccで監視できないフォルダがあります"


# ---------------------------------------------------------------------------
# E3000番台: 隔離（エラーコード一覧・例外設計書4.3節）
# ---------------------------------------------------------------------------
class QuarantineError(AppError):
    default_code = "E3000"
    default_message = "隔離処理でエラーが発生しました"


class QuarantineWriteError(QuarantineError):
    default_code = "E3001"
    default_message = "ファイルの隔離に失敗しました"


class QuarantineRestoreError(QuarantineError):
    default_code = "E3002"
    default_message = "ファイルの復元に失敗しました"


class QuarantineNotFoundError(QuarantineError):
    default_code = "E3003"
    default_message = "対象のデータが見つかりません"


# ---------------------------------------------------------------------------
# E4000番台: スケジュール（エラーコード一覧・例外設計書4.4節）
# ---------------------------------------------------------------------------
class SchedulerError(AppError):
    default_code = "E4000"
    default_message = "スケジュール処理でエラーが発生しました"


class JobValidationError(SchedulerError):
    default_code = "E4001"
    default_message = "ジョブの設定内容が正しくありません"


class JobExecutionError(SchedulerError):
    default_code = "E4003"
    default_message = "スケジュールジョブの実行に失敗しました"


class JobAlreadyRunningError(SchedulerError):
    default_code = "E4004"
    default_message = "このジョブは実行中です"


# ---------------------------------------------------------------------------
# E5000番台: データベース（DBマイグレーション設計書5.3節）
# ---------------------------------------------------------------------------
class DatabaseError(AppError):
    default_code = "E5000"
    default_message = "データベース処理でエラーが発生しました"


class DatabaseConnectionError(DatabaseError):
    default_code = "E5001"
    default_message = "データベースに接続できません。アプリを再起動してください"


class DatabaseMigrationError(DatabaseError):
    default_code = "E5002"
    default_message = "データベースの更新に失敗しました"


class DatabaseIntegrityError(DatabaseError):
    default_code = "E5003"
    default_message = "データの保存に失敗しました"


# ---------------------------------------------------------------------------
# E6000番台: 設定（config.jsonスキーマ定義書2.2節・3.2節・5章）
# ---------------------------------------------------------------------------
class ConfigError(AppError):
    default_code = "E6000"
    default_message = "設定の読み込みに失敗しました"


class ConfigWriteError(ConfigError):
    default_code = "E6001"
    default_message = "設定を保存できませんでした。実行ファイルの配置場所の書き込み権限を確認してください"


class ConfigValidationError(ConfigError):
    default_code = "E6002"
    default_message = "入力値が正しくありません"


class KeyringUnavailableError(ConfigError):
    default_code = "E6003"
    default_message = "この環境ではパスワードの安全な保存に対応していません。SMTP通知機能は利用できません"


# E6004は設定ファイルの破損（ログのエラーコードとしてのみ使用。config_service.py）
class UnsafeExecutablePathError(ConfigError):
    default_code = "E6005"
    default_message = (
        "実行ファイルの場所が安全でないため実行しませんでした。"
        "管理者だけが書き込める場所にある実行ファイルを指定してください"
    )


# ---------------------------------------------------------------------------
# E7000番台: 通知（通知機能詳細仕様書5.3節）
# ---------------------------------------------------------------------------
class NotificationError(AppError):
    default_code = "E7000"
    default_message = "通知処理でエラーが発生しました"


class MailSendError(NotificationError):
    default_code = "E7001"
    default_message = "メール通知の送信に失敗しました"


class DesktopNotifyError(NotificationError):
    default_code = "E7002"
    default_message = "デスクトップ通知の表示に失敗しました"


# ---------------------------------------------------------------------------
# E8000番台: プロセス・権限・起動（タスクトレイ常駐アプリプロセス設計書）
# ---------------------------------------------------------------------------
class ProcessError(AppError):
    default_code = "E8000"
    default_message = "アプリケーション処理でエラーが発生しました"


class PermissionDeniedError(ProcessError):
    default_code = "E8001"
    default_message = "この操作には管理者権限が必要です"


class SingleInstanceError(ProcessError):
    default_code = "E8002"
    default_message = "アプリは既に起動しています"


class ClamdProcessError(ProcessError):
    default_code = "E8003"
    default_message = "ClamAVサービスの起動/停止に失敗しました"


class DefinitionUpdateError(ProcessError):
    """freshclamの実行失敗（コマンド未検出・異常終了）。Phase 11でE8003（clamdの起動/停止失敗）から分離"""

    default_code = "E8004"
    default_message = "定義データベースの更新に失敗しました"
