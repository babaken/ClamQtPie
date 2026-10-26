"""
config.json データモデル

参照: config.jsonスキーマ定義書 4章
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

SCHEMA_VERSION = 1

_ALLOWED_RETENTION_DAYS = (30, 60, 90, 180, 365)
_ALLOWED_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_ALLOWED_SMTP_ENCRYPTION = ("none", "ssl", "starttls")
_ALLOWED_CONNECTION_TYPES = ("auto", "unix", "tcp")
_ALLOWED_REALTIME_ACTIONS = ("quarantine", "notify_only")
_ALLOWED_LINUX_BACKENDS = ("clamonacc", "watchdog")


@dataclass
class GeneralConfig:
    autostart: bool = True
    start_minimized: bool = True
    minimize_to_tray: bool = True
    show_tray_hint_shown: bool = False
    language: str = "ja"


@dataclass
class ClamdConfig:
    connection_type: str = "auto"
    unix_socket_path: str = "/var/run/clamav/clamd.ctl"
    tcp_host: str = "127.0.0.1"
    tcp_port: int = 3310
    connect_timeout_sec: int = 5
    read_timeout_sec: int = 30
    instream_chunk_size: int = 8192
    max_stream_size_mb: int = 100
    max_concurrent_connections: int = 4
    auto_start: bool = True  # clamd停止を検知したらサービス起動を1回試みる（詳細設計書4章）
    service_name: str = ""  # 空ならOS既定（Windows: clamd / Linux: clamav-daemon）


@dataclass
class RealtimeConfig:
    default_enabled: bool = False
    default_action: str = "quarantine"
    windows_debounce_sec: float = 2.0
    scan_worker_threads: int = 2
    max_pending_timers: int = 1000
    lock_retry_wait_sec: float = 1.0
    # Linux専用（Windowsでは無視され常にwatchdog）
    linux_backend: str = "clamonacc"  # "clamonacc" | "watchdog"
    clamonacc_path: str = "clamonacc"  # 実行ファイル名またはフルパス
    clamonacc_config_file: str = ""  # 空ならclamonaccの既定（通常/etc/clamav/clamd.conf）
    clamonacc_fdpass: bool = True  # clamdがLocalSocket接続の場合に有効（TCP接続のclamdではFalseにする）


@dataclass
class SmtpConfig:
    host: str = ""
    port: int = 587
    user: str = ""
    keyring_account: str = "smtp_default"
    from_address: str = ""
    to_address: str = ""
    encryption: str = "starttls"


@dataclass
class NotificationConfig:
    on_detect_mail: bool = True
    on_detect_desktop: bool = True
    on_update_fail: bool = True
    on_job_fail: bool = True
    aggregation_window_sec: int = 60
    max_batch_size: int = 50


@dataclass
class UpdateConfig:
    auto_update: bool = True
    interval_hours: int = 4
    #: freshclamの実行ファイル名（PATHから探す）またはフルパス（コードレビューで追加。PATHに無い環境向け）
    freshclam_path: str = "freshclam"


@dataclass
class LoggingConfig:
    retention_days: int = 90
    level: str = "INFO"


@dataclass
class ConfigModel:
    schema_version: int = SCHEMA_VERSION
    general: GeneralConfig = field(default_factory=GeneralConfig)
    clamd: ClamdConfig = field(default_factory=ClamdConfig)
    realtime: RealtimeConfig = field(default_factory=RealtimeConfig)
    smtp: SmtpConfig = field(default_factory=SmtpConfig)
    notification: NotificationConfig = field(default_factory=NotificationConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ConfigModel":
        """欠損キーを既定値で補完しながら辞書からConfigModelを生成する

        参照: config.jsonスキーマ定義書2.3節「欠損キーがあれば既定値で補完」
        """
        general = GeneralConfig(**_filtered(data.get("general", {}), GeneralConfig))
        clamd = ClamdConfig(**_filtered(data.get("clamd", {}), ClamdConfig))
        realtime = RealtimeConfig(**_filtered(data.get("realtime", {}), RealtimeConfig))
        smtp = SmtpConfig(**_filtered(data.get("smtp", {}), SmtpConfig))
        notification = NotificationConfig(**_filtered(data.get("notification", {}), NotificationConfig))
        update = UpdateConfig(**_filtered(data.get("update", {}), UpdateConfig))
        logging_cfg = LoggingConfig(**_filtered(data.get("logging", {}), LoggingConfig))

        model = cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            general=general,
            clamd=clamd,
            realtime=realtime,
            smtp=smtp,
            notification=notification,
            update=update,
            logging=logging_cfg,
        )
        return model


def _filtered(raw: dict, dataclass_type) -> dict:
    """dataclassが持たない未知のキーを除去する（前方互換のための保護）"""
    valid_fields = {f for f in dataclass_type.__dataclass_fields__}
    return {k: v for k, v in raw.items() if k in valid_fields}


def validate_and_fix(model: ConfigModel, warn) -> ConfigModel:
    """バリデーションルール適用。不正値は既定値へ自動補正しWARNINGログを出す。

    参照: config.jsonスキーマ定義書5章
    warn: 呼び出し側から渡されるログ関数 (str) -> None
    """
    if model.logging.retention_days not in _ALLOWED_RETENTION_DAYS:
        warn(
            f"logging.retention_days が不正な値です ({model.logging.retention_days})。"
            f"90へ自動補正します。"
        )
        model.logging.retention_days = 90

    if model.logging.level not in _ALLOWED_LOG_LEVELS:
        warn(f"logging.level が不正な値です ({model.logging.level})。INFOへ自動補正します。")
        model.logging.level = "INFO"

    if model.smtp.encryption not in _ALLOWED_SMTP_ENCRYPTION:
        warn(f"smtp.encryption が不正な値です ({model.smtp.encryption})。starttlsへ自動補正します。")
        model.smtp.encryption = "starttls"

    if model.clamd.connection_type not in _ALLOWED_CONNECTION_TYPES:
        warn(
            f"clamd.connection_type が不正な値です ({model.clamd.connection_type})。"
            f"autoへ自動補正します。"
        )
        model.clamd.connection_type = "auto"

    if model.realtime.default_action not in _ALLOWED_REALTIME_ACTIONS:
        warn(
            f"realtime.default_action が不正な値です ({model.realtime.default_action})。"
            f"quarantineへ自動補正します。"
        )
        model.realtime.default_action = "quarantine"

    if model.realtime.linux_backend not in _ALLOWED_LINUX_BACKENDS:
        warn(
            f"realtime.linux_backend が不正な値です ({model.realtime.linux_backend})。"
            f"clamonaccへ自動補正します。"
        )
        model.realtime.linux_backend = "clamonacc"

    if not isinstance(model.update.freshclam_path, str) or not model.update.freshclam_path.strip():
        warn(f"update.freshclam_path が不正な値です ({model.update.freshclam_path!r})。freshclamへ自動補正します。")
        model.update.freshclam_path = "freshclam"

    if not (1 <= model.clamd.tcp_port <= 65535):
        warn(f"clamd.tcp_port が範囲外です ({model.clamd.tcp_port})。既定値3310へ補正します。")
        model.clamd.tcp_port = 3310

    if not (1 <= model.smtp.port <= 65535):
        warn(f"smtp.port が範囲外です ({model.smtp.port})。既定値587へ補正します。")
        model.smtp.port = 587

    if model.clamd.max_concurrent_connections < 1:
        warn("clamd.max_concurrent_connections は1以上である必要があります。既定値4へ補正します。")
        model.clamd.max_concurrent_connections = 4

    if not (0.1 <= model.realtime.windows_debounce_sec <= 10.0):
        warn(
            f"realtime.windows_debounce_sec が範囲外です "
            f"({model.realtime.windows_debounce_sec})。既定値2.0へ補正します。"
        )
        model.realtime.windows_debounce_sec = 2.0

    return model
