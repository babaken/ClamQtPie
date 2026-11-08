"""
config.json データモデル

参照: config.jsonスキーマ定義書 4章
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict

#: 2: サービスの設定（段階 12-3b）。利用者ごとの GUI の設定（general）を gui.json（core/gui_config.py）に分けた
SCHEMA_VERSION = 2
#: サービスの設定に含めない部分（利用者ごとの GUI の設定）
GUI_SECTIONS = ("general",)

_ALLOWED_RETENTION_DAYS = (30, 60, 90, 180, 365)
_ALLOWED_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_ALLOWED_SMTP_ENCRYPTION = ("none", "ssl", "starttls")
_ALLOWED_CONNECTION_TYPES = ("auto", "unix", "tcp")
_ALLOWED_REALTIME_ACTIONS = ("quarantine", "notify_only")
_ALLOWED_LINUX_BACKENDS = ("clamonacc", "watchdog")


#: Web API で変えられる項目。設定画面で変えられるもの（と、定義の古さの日数）に限る。それ以外は読み取りだけ。
#: 次のものは特に、Web API から変えられると権限を越えた操作に使えるため含めない（12章 25）:
#: - 実行ファイル・設定ファイルの場所（update.freshclam_path、realtime.clamonacc_path・clamonacc_config_file）:
#:   任意のプログラムを動かせる
#: - clamd の接続先（clamd.connection_type・tcp_host・tcp_port・unix_socket_path）: 偽の clamd に検査するファイルの中身を
#:   送らせ、常に「検知なし」と返させられる（ポートだけでも、このPC の一般の利用者が待ち受けたポートに向けられる）
#: - clamd.service_name: 任意のサービスを起動・停止させられる
#: - general（自動起動・最小化など）: 利用者ごとの GUI の設定（gui.json）
#: 変えるときは clamqtpie-ctl config set を使う。旧版のデータの移行（clamqtpie-ctl migrate）でも、移すのはこの項目だけ
WRITABLE_FIELDS = (
    "clamd.auto_start",
    "logging.level",
    "logging.retention_days",
    "notification.on_detect_desktop",
    "notification.on_detect_mail",
    "notification.on_job_fail",
    "notification.on_update_fail",
    "realtime.default_action",
    "realtime.default_enabled",
    "smtp.encryption",
    "smtp.from_address",
    "smtp.host",
    "smtp.port",
    "smtp.to_address",
    "smtp.user",
    "scan.quick_targets",
    "update.auto_update",
    "update.interval_hours",
    "update.stale_days",
)
#: 読み込み時の検証（validate_and_fix）に無い範囲（設定画面の入力欄と同じ）
WRITABLE_RANGES = {"update.interval_hours": (1, 24)}


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
    #: 定義データベースがこの日数より古くなったら警告する（ホーム画面・トレイ・通知。1〜30）
    stale_days: int = 3


@dataclass
class LoggingConfig:
    retention_days: int = 90
    level: str = "INFO"


#: scan.quick_targets の数・長さの上限
MAX_QUICK_TARGETS = 100
_MAX_PATH = 4096


@dataclass
class ScanConfig:
    """サービスのクイックスキャンの対象（サービス化・Web API設計書 6.2節。段階 12-3d）

    サービスは SYSTEM・root で動くため、利用者のフォルダ（ダウンロード等）が分からない。Web API で対象を
    指定しないクイックスキャン（ブラウザ・clamqtpie-ctl）は、ここに利用者が指定した場所だけを使う。
    空の間は受け付けない。トレイGUI は利用者の権限で対象を求めて送るため、この設定を使わない
    """

    quick_targets: list[str] = field(default_factory=list)


def quick_targets_problem(value) -> str | None:
    """scan.quick_targets として正しくなければ、その理由"""
    if not isinstance(value, list) or len(value) > MAX_QUICK_TARGETS:
        return f"{MAX_QUICK_TARGETS} 件までのパスの一覧で指定してください"
    for target in value:
        if not isinstance(target, str) or not target or len(target) > _MAX_PATH:
            return "パスは空でない文字列で指定してください"
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in target):
            return "パスに制御文字は使えません"
        if not os.path.isabs(target):
            return f"パスは絶対パスで指定してください: {target}"
    return None


@dataclass
class ScheduleConfig:
    """スケジュールの取りこぼし対策（サービス化・Web API設計書 9章）"""

    #: 予定の時刻を過ぎても実行する猶予（秒。60〜86400）。重なった回は1回にまとめる
    misfire_grace_sec: int = 3600


#: api.web_tls に使える値
WEB_TLS_MODES = ("off", "local_ca")


@dataclass
class ApiConfig:
    """Web API（サービス化・Web API設計書3.1節の内部API）。段階12-3までは従来のアプリの中で動かすため既定はOFF"""

    enabled: bool = False
    port: int = 8765  # 待ち受けるのは127.0.0.1だけ（変更不可）
    # WebUI（3.1節・3.3節。段階 12-5）。api はすべて保護された項目（Web API の PUT /config では変えられない）。
    # ON/OFF・ログインの設定は PUT /web・PUT /web/credentials（admin の Bearer だけ）か clamqtpie-ctl web で変える
    web_enabled: bool = False
    web_port: int = 8766
    web_username: str = "admin"
    #: scrypt のハッシュ（core/web_password.py）。空なら未設定（未設定の間は WebUI を ON にできない）
    web_password_hash: str = ""
    #: 設定したパスワードの長さ（LAN 許可のときの 12 文字以上の確認用。ハッシュからは分からないため）
    web_password_length: int = 0
    #: WebUI を自動で OFF にする日時（UTC の ISO 8601）。空なら期限なし
    web_off_at: str = ""
    #: 二段階認証（TOTP。段階 12-5d）を設定済みか。秘密鍵は設定ファイルに入れず、秘密の保存先に置く
    web_totp_enabled: bool = False
    #: 最後に受け付けたコードのステップ番号（これ以前のコードは受け付けない。使い回しの防止）
    web_totp_last_step: int = 0
    #: 予備のコードの scrypt のハッシュ（使ったものは消す）
    web_recovery_code_hashes: list[str] = field(default_factory=list)
    #: 「パスワードは合っていて確認コードだけが違う」ログインの回数。core.totp.STOP_FAILURES になったら、パスワードと
    #: 確認コードでのログインを止める。0 に戻すのは管理者の操作（ログインの停止を解く・パスワードの変更）だけ
    web_totp_failures: int = 0
    #: WebUI の HTTPS（段階 12-5e1）。"off"（HTTP）か "local_ca"（ClamQtPie のローカル CA で作った証明書。<データ>/tls/）。
    #: 利用者が用意した証明書（"custom"）は v0.10.0 では受け付けない
    web_tls: str = "off"
    #: WebUI のセッションの有効期限（最後の操作からの分）
    session_timeout_min: int = 30


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
    scan: ScanConfig = field(default_factory=ScanConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    api: ApiConfig = field(default_factory=ApiConfig)

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
        scan = ScanConfig(**_filtered(data.get("scan", {}), ScanConfig))
        schedule = ScheduleConfig(**_filtered(data.get("schedule", {}), ScheduleConfig))
        api = ApiConfig(**_filtered(data.get("api", {}), ApiConfig))

        model = cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            general=general,
            clamd=clamd,
            realtime=realtime,
            smtp=smtp,
            notification=notification,
            update=update,
            logging=logging_cfg,
            scan=scan,
            schedule=schedule,
            api=api,
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

    if not isinstance(model.update.stale_days, int) or not (1 <= model.update.stale_days <= 30):
        warn(f"update.stale_days が範囲外です ({model.update.stale_days!r})。既定値3へ補正します。")
        model.update.stale_days = 3

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

    problem = quick_targets_problem(model.scan.quick_targets)
    if problem is not None:
        warn(f"scan.quick_targets が不正な値です（{problem}）。空へ補正します。")
        model.scan.quick_targets = []

    grace = model.schedule.misfire_grace_sec
    if isinstance(grace, bool) or not isinstance(grace, int) or not (60 <= grace <= 86400):
        warn(f"schedule.misfire_grace_sec が範囲外です ({grace!r})。既定値3600へ補正します。")
        model.schedule.misfire_grace_sec = 3600

    if not isinstance(model.api.enabled, bool):
        warn(f"api.enabled が不正な値です ({model.api.enabled!r})。falseへ自動補正します。")
        model.api.enabled = False

    # 1024未満は特権ポート。bool（intの派生）は値として扱わない
    if isinstance(model.api.port, bool) or not isinstance(model.api.port, int) or not (1024 <= model.api.port <= 65535):
        warn(f"api.port が範囲外です ({model.api.port!r})。既定値8765へ補正します。")
        model.api.port = 8765

    _validate_web(model.api, warn)
    return model


_WEB_USERNAME = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")


def _validate_web(api: ApiConfig, warn) -> None:
    """WebUI の設定（段階 12-5a）。パスワードが未設定・壊れていれば WebUI を OFF にする"""
    from core.duration_text import parse_off_at
    from core.web_password import is_valid_hash

    if not isinstance(api.web_enabled, bool):
        warn(f"api.web_enabled が不正な値です ({api.web_enabled!r})。falseへ自動補正します。")
        api.web_enabled = False
    port = api.web_port
    if isinstance(port, bool) or not isinstance(port, int) or not (1024 <= port <= 65535) or port == api.port:
        fallback = 8766 if api.port != 8766 else 8767
        warn(f"api.web_port が範囲外か、内部 API のポートと同じです ({port!r})。{fallback}へ補正します。")
        api.web_port = fallback
    if not isinstance(api.web_username, str) or not _WEB_USERNAME.match(api.web_username):
        warn(f"api.web_username が不正な値です ({api.web_username!r})。adminへ補正します。")
        api.web_username = "admin"
    if not isinstance(api.web_password_hash, str) or (api.web_password_hash and not is_valid_hash(api.web_password_hash)):
        warn("api.web_password_hash が正しくありません。パスワードを未設定にします。")
        api.web_password_hash = ""
    length = api.web_password_length
    if isinstance(length, bool) or not isinstance(length, int) or not (0 <= length <= 1024):
        api.web_password_length = 0
    if not api.web_password_hash:
        api.web_password_length = 0
        if api.web_enabled:
            warn("WebUI のパスワードが未設定のため、WebUI を OFF にします。")
            api.web_enabled = False
    if not isinstance(api.web_totp_enabled, bool):
        # 壊れていても二段階認証を外さない（外すのは clamqtpie-ctl web totp reset だけ）
        warn(f"api.web_totp_enabled が不正な値です ({api.web_totp_enabled!r})。設定済みとして扱います。")
        api.web_totp_enabled = True
    step = api.web_totp_last_step
    if isinstance(step, bool) or not isinstance(step, int) or step < 0:
        api.web_totp_last_step = 0
    hashes = api.web_recovery_code_hashes
    if not isinstance(hashes, list) or not all(isinstance(h, str) and is_valid_hash(h) for h in hashes):
        warn("api.web_recovery_code_hashes が正しくありません。正しくない予備のコードを除きます。")
        api.web_recovery_code_hashes = [h for h in (hashes if isinstance(hashes, list) else [])
                                        if isinstance(h, str) and is_valid_hash(h)][:20]
    failures = api.web_totp_failures
    if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
        from core.totp import STOP_FAILURES

        # 壊れていたら止めた側に倒す（管理者が「ログインの停止を解く」で戻せる）
        warn(f"api.web_totp_failures が不正な値です ({failures!r})。WebUI のログインを止めた状態として扱います。")
        api.web_totp_failures = STOP_FAILURES
    if not api.web_totp_enabled:
        api.web_totp_last_step = 0
        api.web_recovery_code_hashes = []
        api.web_totp_failures = 0
    if api.web_tls not in WEB_TLS_MODES:
        warn(f"api.web_tls が不正な値です ({api.web_tls!r})。off へ補正します。")
        api.web_tls = "off"
    if not isinstance(api.web_off_at, str) or (api.web_off_at and parse_off_at(api.web_off_at) is None):
        warn(f"api.web_off_at が不正な値です ({api.web_off_at!r})。期限なしにします。")
        api.web_off_at = ""
    timeout = api.session_timeout_min
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not (1 <= timeout <= 1440):
        warn(f"api.session_timeout_min が範囲外です ({timeout!r})。既定値30へ補正します。")
        api.session_timeout_min = 30
