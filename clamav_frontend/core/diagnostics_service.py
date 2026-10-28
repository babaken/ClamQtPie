"""
DiagnosticsService — システム診断（ClamAVの実行ファイルの場所・clamdの接続先の確認と診断レポートの作成）

画面レイアウト刷新で追加した「システム診断」画面から使う。
- 実行ファイル: アプリが実際に起動するのと同じ探し方（PATH。freshclamはconfig.update.freshclam_path、
  clamonaccはconfig.realtime.clamonacc_pathの指定）で探す。見つからない場合だけ、案内のために
  Windowsは既定のインストール先（Program Files\\ClamAV、Chocolateyのbin）、Linuxはsbin（/usr/sbin等）も探す
  （コードレビューで修正。以前は既定のインストール先で見つかればOKとしていたが、実際の定義更新は
  PATHのfreshclamしか起動しないため、診断はOKなのに更新は失敗していた）
- clamdはOSのサービスとして起動しソケットで接続するため、実行ファイルの場所は表示のみ（判定はPING・サービスの状態）
- 設定ファイル・定義DBフォルダ: clamd.conf / freshclam.conf の場所と、clamd.confのDatabaseDirectory
- clamd: 接続方式・接続先・OSのサービスの状態・PING・バージョン。clamd.confの待ち受け先
  （TCPSocket / TCPAddr / LocalSocket）とアプリの接続先（config.clamd）が違えば注意を出す
- レポートはテキストで作り、問い合わせ等に貼り付けやすいよう、伏せ字にした版（mask_report）も作れる。
  伏せ字にするのはホームフォルダ・ユーザー名・コンピューター名と、DiagnosticsReport.sensitive
  （ソケット・clamd.conf / freshclam.confのパス・clamonaccのパス・アプリのフォルダ・clamdのホスト名とグローバルIP）

clamdへの接続やsc/systemctlの実行で数秒かかることがあるため、GUIからはBackgroundTask（core/background_task.py）経由で呼ぶ。
"""
from __future__ import annotations

import ipaddress
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from core.clamd_conf import DEFAULT_CLAMD_CONF_CANDIDATES, find_clamd_conf, load_clamd_conf
from core.clamd_process import ClamdServiceController, console_text_kwargs, is_valid_service_name, no_window_kwargs
from core.config_model import ConfigModel
from core.exec_path import system_tool
from core.version import APP_NAME, APP_VERSION

REPORT_TITLE = f"{APP_NAME} 診断レポート"

LEVEL_OK = "OK"
LEVEL_WARN = "注意"
LEVEL_NG = "NG"

#: 確認する実行ファイル（clamonaccはLinuxのみ）
COMPONENTS = ("clamscan", "freshclam", "clamd", "clamdscan", "clamonacc")
#: 本アプリが自分で起動するもの（見つからなければ注意）。clamdはOSのサービスとして起動するため含めない。
#: clamscan / clamdscan は本アプリでは使わない（表示のみ）
_LAUNCHED_COMPONENTS = ("freshclam", "clamonacc")

#: 一般ユーザーのPATHに無いことがあるsbin（Debian等。clamd・clamonaccはここに入る）
_LINUX_SBIN_DIRS = ("/usr/local/sbin", "/usr/sbin", "/sbin")
_LINUX_FRESHCLAM_CONF_CANDIDATES = ("/etc/clamav/freshclam.conf", "/etc/freshclam.conf")
_LINUX_DEFAULT_DATABASE_DIR = "/var/lib/clamav"
_DEFINITION_SUFFIXES = (".cvd", ".cld", ".cud")
_NOT_FOUND = "見つかりません"
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
#: clamd.confのTCPAddrで、すべてのアドレスで待ち受けることを表す値
_ANY_ADDRS = ("0.0.0.0", "::")

_PLACEHOLDER_SOCKET = "<ソケット>"
_PLACEHOLDER_CLAMD_HOST = "<clamdのホスト>"


@dataclass
class CheckResult:
    level: str  # LEVEL_OK / LEVEL_WARN / LEVEL_NG
    message: str


@dataclass
class DiagnosticsReport:
    created_at: datetime
    #: (見出し, [(項目名, 値), ...]) の並び
    sections: list[tuple[str, list[tuple[str, str]]]] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    #: 伏せ字版で置き換える値 → 置き換え後の文字列（mask_reportのextra）
    sensitive: dict[str, str] = field(default_factory=dict)

    @property
    def has_problem(self) -> bool:
        return any(c.level == LEVEL_NG for c in self.checks)

    def add_sensitive(self, value: str | None, placeholder: str) -> None:
        if value:
            self.sensitive.setdefault(str(value), placeholder)

    def to_text(self) -> str:
        lines = [
            f"=== {REPORT_TITLE} ===",
            f"作成日時 (UTC): {self.created_at.strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        for title, items in self.sections:
            lines.append("")
            lines.append(f"--- {title} ---")
            width = max((len(name) for name, _ in items), default=0)
            for name, value in items:
                lines.append(f"{(name + ':').ljust(width + 1)} {value}")
        lines.append("")
        lines.append("--- ヘルスチェック結果 ---")
        for check in self.checks:
            lines.append(f"[{check.level}] {check.message}")
        return "\n".join(lines) + "\n"


def mask_report(
    text: str,
    home: str | None = None,
    user: str | None = None,
    host: str | None = None,
    extra: dict[str, str] | None = None,
) -> str:
    """問い合わせ等へ貼り付ける用に、ホームフォルダ・ユーザー名・コンピューター名を伏せ字にする

    引数を省略した場合は実行中の環境の値を使う。短すぎる名前（2文字以下）は他の語を巻き込むため置き換えない。
    extra: 追加で伏せる値 → 置き換え後の文字列（DiagnosticsReport.sensitive。ソケット・設定ファイルのパス等）
    """
    home = home if home is not None else str(Path.home())
    user = user if user is not None else _current_user()
    host = host if host is not None else platform.node()
    flags = re.IGNORECASE if os.name == "nt" else 0
    # 追加の値はホームフォルダより先に置き換える（ホーム配下のアプリのフォルダ等を「~\...」でなく丸ごと伏せるため）。
    # 長い値から置き換え、短い値が長い値の一部だけを先に崩さないようにする
    for value, placeholder in sorted((extra or {}).items(), key=lambda item: -len(item[0])):
        text = _replace_word(text, value, placeholder, flags)
    if home and home not in ("/", "\\"):
        # 区切り文字の違い（C:\Users\x と C:/Users/x）も同じものとして置き換える
        parts = [re.escape(part) for part in re.split(r"[\\/]", home.rstrip("\\/"))]
        text = re.sub(r"[\\/]".join(parts), "~", text, flags=flags)
    for value, placeholder in ((user, "<ユーザー名>"), (host, "<コンピューター名>")):
        text = _replace_word(text, value, placeholder, flags)
    return text


def _replace_word(text: str, value: str | None, placeholder: str, flags: int) -> str:
    if not value or len(value) <= 2:
        return text
    return re.sub(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])", placeholder, text, flags=flags)


def _current_user() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # noqa: BLE001 - 環境変数が無い等。伏せ字にできないだけで動作は続ける
        return ""


def is_sensitive_host(host: str) -> bool:
    """伏せ字版で伏せるclamdのホストか。ループバック・プライベートIPは環境を特定できないため伏せない。
    ホスト名（社内サーバー名等）は、グローバルかどうかを名前から判断できないため伏せる"""
    if not host or host.lower() in _LOOPBACK_HOSTS:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return address.is_global


class DiagnosticsService:
    def __init__(
        self,
        config_service,
        clamav_service,
        realtime_manager=None,
        app_dir: Path | None = None,
        system: str | None = None,
        which: Callable[[str], str | None] = shutil.which,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        environ: dict[str, str] | None = None,
    ) -> None:
        """
        config_service: ConfigService（診断のたびにcurrentを読む。設定画面で保存した内容を反映するため）
        system/which/runner/environ: テスト用の差し替え口
        """
        self._config_service = config_service
        self._clamav_service = clamav_service
        self._realtime_manager = realtime_manager
        self._app_dir = app_dir
        self._system = system or platform.system()
        self._which = which
        self._run = runner
        self._environ = environ if environ is not None else os.environ

    @property
    def config(self) -> ConfigModel:
        return self._config_service.current

    # -- 実行ファイル -----------------------------------------------------
    def find_executable(self, name: str) -> str | None:
        """アプリが実際に起動するのと同じ探し方で探す（freshclam・clamonaccは設定の指定、それ以外はPATH）"""
        return self._which(self._configured_name(name))

    def _configured_name(self, name: str) -> str:
        if name == "freshclam":
            return self.config.update.freshclam_path or name
        if name == "clamonacc":
            return self.config.realtime.clamonacc_path or name
        return name

    def find_install_location(self, name: str) -> str | None:
        """PATHに無い場合の案内用: 既定のインストール先（Windows）・sbin（Linux）にあればその場所"""
        if self._system == "Windows":
            dirs, file_name = self._windows_install_dirs(), f"{name}.exe"
        else:
            dirs, file_name = [Path(d) for d in _LINUX_SBIN_DIRS], name
        for directory in dirs:
            candidate = directory / file_name
            if candidate.is_file():
                return str(candidate)
        return None

    def _windows_install_dirs(self) -> list[Path]:
        dirs = []
        for var in ("ProgramFiles", "ProgramFiles(x86)"):
            if self._environ.get(var):
                dirs.append(Path(self._environ[var]) / "ClamAV")
        if self._environ.get("ProgramData"):
            dirs.append(Path(self._environ["ProgramData"]) / "chocolatey" / "bin")
        return dirs

    def _check_component(
        self, checks: list[CheckResult], name: str, path: str | None, install_path: str | None
    ) -> None:
        if name not in _LAUNCHED_COMPONENTS:
            return  # clamdはPING・サービスの状態で判定する。clamscan / clamdscan は本アプリでは使わない
        if name == "clamonacc" and self.config.realtime.linux_backend == "watchdog":
            return  # 設定でwatchdog方式を選んでいるため使わない
        if path:
            checks.append(CheckResult(LEVEL_OK, f"{name} を検出しました: {path}"))
            return
        configured = self._configured_name(name)
        if configured != name:
            sentences = [f"{name} が設定で指定した場所に見つかりません（{configured}）"]
        else:
            sentences = [f"{name} が PATH に見つかりません"]
        if name == "freshclam":
            sentences.append("定義の更新ができません")
            setting = "設定画面の「定義更新」タブ"
        else:
            sentences.append("リアルタイム監視はwatchdog方式で動作します")
            setting = "設定画面の「ClamAV エンジン」タブ"
        if install_path:
            sentences.append(f"{install_path} にあります。PATH に追加するか、{setting}で場所を指定してください")
        else:
            sentences.append("ClamAVがインストールされているか確認してください")
        checks.append(CheckResult(LEVEL_WARN, "。".join(sentences)))

    # -- 設定ファイル・定義DB ---------------------------------------------
    def _windows_service_command(self) -> tuple[str | None, str | None]:
        """sc qcのBINARY_PATH_NAMEから、OSのサービスが実際に起動するclamd.exeと--config-fileを取り出す

        Chocolatey等では、PATH上のclamd.exeは本体を呼び出すだけの中継ファイル（shim）で、その隣に
        clamd.confや定義DBは無い。サービスの登録内容から本体の場所と設定ファイルを知る
        （Windows実機の検証で、正常に動作しているのに「定義ファイルが見つかりません」とNGになったため）。
        """
        name = ClamdServiceController(self.config.clamd, system=self._system).service_name
        if not is_valid_service_name(name):
            return None, None
        try:
            completed = self._run(
                [system_tool("sc", self._system), "qc", name],
                capture_output=True,
                timeout=15,
                **console_text_kwargs(self._system),
                **no_window_kwargs(self._system),
            )
        except (OSError, subprocess.TimeoutExpired):
            return None, None
        m = re.search(r"BINARY_PATH_NAME\s*:\s*(.+)", completed.stdout or "")
        if not m:
            return None, None
        command_line = m.group(1).strip()
        exe = re.match(r'"([^"]+)"|(\S+)', command_line)
        conf = re.search(r'(?:--config-file|-c)(?:=|\s+)(?:"([^"]+)"|(\S+))', command_line)
        return (
            (exe.group(1) or exe.group(2)) if exe else None,
            (conf.group(1) or conf.group(2)) if conf else None,
        )

    def _conf_dirs(self, clamd_path: str | None, service_exe: str | None, service_conf: str | None) -> list[Path]:
        """Windowsで設定ファイル・定義DBを探すフォルダ（サービスの登録内容を優先する）"""
        dirs = [Path(p).parent for p in (service_conf, service_exe, clamd_path) if p]
        return dirs + self._windows_install_dirs()

    def _conf_candidates(self, conf_name: str, conf_dirs: list[Path]) -> tuple[str, ...]:
        if self._system == "Windows":
            return tuple(str(d / conf_name) for d in conf_dirs)
        if conf_name == "clamd.conf":
            return DEFAULT_CLAMD_CONF_CANDIDATES
        return _LINUX_FRESHCLAM_CONF_CANDIDATES

    def _database_dir(self, clamd_conf, conf_dirs: list[Path]) -> Path | None:
        if clamd_conf is not None and clamd_conf.get("DatabaseDirectory"):
            return Path(clamd_conf.get("DatabaseDirectory"))
        if self._system == "Windows":
            for directory in conf_dirs:
                if (directory / "database").is_dir():
                    return directory / "database"
            return conf_dirs[0] / "database" if conf_dirs else None
        return Path(_LINUX_DEFAULT_DATABASE_DIR)

    def _check_listen_address(self, checks: list[CheckResult], clamd_conf, report: DiagnosticsReport) -> None:
        """clamd.confの待ち受け先とアプリの接続先（config.clamd）が食い違っていないか"""
        if clamd_conf is None:
            return
        clamd = self.config.clamd
        connection_type = clamd.connection_type
        if connection_type == "auto":
            connection_type = "tcp" if self._system == "Windows" else "unix"
        hint = "clamd.conf と設定画面の「ClamAV エンジン」タブの接続の設定を合わせてください"
        if connection_type == "unix":
            local_socket = clamd_conf.get("LocalSocket")
            report.add_sensitive(local_socket, _PLACEHOLDER_SOCKET)
            if local_socket is None:
                checks.append(CheckResult(
                    LEVEL_WARN,
                    f"clamd.conf に LocalSocket がありません。アプリは UNIX ソケット（{clamd.unix_socket_path}）で接続します。{hint}",
                ))
            elif not _same_path(local_socket, clamd.unix_socket_path):
                checks.append(CheckResult(
                    LEVEL_WARN,
                    f"clamd.conf の LocalSocket（{local_socket}）とアプリの接続先（{clamd.unix_socket_path}）が違います。{hint}",
                ))
            return
        port = clamd_conf.get("TCPSocket")
        if port is None:
            checks.append(CheckResult(
                LEVEL_WARN,
                f"clamd.conf に TCPSocket がありません。アプリは TCP（{clamd.tcp_host}:{clamd.tcp_port}）で接続します。{hint}",
            ))
            return
        if port.strip() != str(clamd.tcp_port):
            checks.append(CheckResult(
                LEVEL_WARN,
                f"clamd.conf の TCPSocket（{port}）とアプリの接続先のポート（{clamd.tcp_port}）が違います。{hint}",
            ))
        addrs = clamd_conf.get_all("TCPAddr")
        for addr in addrs:
            if is_sensitive_host(addr):
                report.add_sensitive(addr, _PLACEHOLDER_CLAMD_HOST)
        if addrs and not any(_same_host(addr, clamd.tcp_host) for addr in addrs):
            checks.append(CheckResult(
                LEVEL_WARN,
                f"clamd.conf の TCPAddr（{', '.join(addrs)}）とアプリの接続先のホスト（{clamd.tcp_host}）が違います。{hint}",
            ))

    # -- OSのサービス -----------------------------------------------------
    def _service_state(self) -> str:
        # サービス名の決め方（OSごとの既定値）はエンジン サービス画面の停止・起動と同じものを使う
        name = ClamdServiceController(self.config.clamd, system=self._system).service_name
        if not is_valid_service_name(name):
            return f"{name!r}: サービス名に使えない文字が含まれているため確認しません"
        if self._system == "Windows":
            command = [system_tool("sc", self._system), "query", name]
        else:
            command = [system_tool("systemctl", self._system), "is-active", name]
        try:
            completed = self._run(
                command,
                capture_output=True,
                timeout=15,
                **console_text_kwargs(self._system),
                **no_window_kwargs(self._system),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"{name}: 確認できません ({exc})"
        output = f"{completed.stdout or ''}{completed.stderr or ''}".strip()
        if self._system == "Windows":
            m = re.search(r"STATE\s*:\s*\d+\s+(\w+)", output)
            if m:
                return f"{name}: {m.group(1)}"
            if "1060" in output:  # ERROR_SERVICE_DOES_NOT_EXIST
                return f"{name}: 登録されていません"
            return f"{name}: 確認できません (exit {completed.returncode})"
        return f"{name}: {output.splitlines()[0] if output else '不明'}"

    # -- レポート作成 -----------------------------------------------------
    def run(self) -> DiagnosticsReport:
        report = DiagnosticsReport(created_at=datetime.now(timezone.utc))
        checks = report.checks
        self._add_config_sensitive(report)

        report.sections.append(("アプリ", self._app_items()))

        # 1. ClamAVの実行ファイル
        paths: dict[str, str | None] = {}
        install_paths: dict[str, str | None] = {}
        component_items = []
        for name in COMPONENTS:
            if name == "clamonacc" and self._system == "Windows":
                component_items.append((name, "（Windowsでは使用しません）"))
                continue
            paths[name] = self.find_executable(name)
            install_paths[name] = None if paths[name] else self.find_install_location(name)
            if paths[name]:
                location = paths[name]
            elif install_paths[name]:
                location = f"PATH にありません（{install_paths[name]} にあります）"
            else:
                location = _NOT_FOUND
            component_items.append((name, location))
            self._check_component(checks, name, paths[name], install_paths[name])
        report.add_sensitive(paths.get("clamonacc") or install_paths.get("clamonacc"), "<clamonaccのパス>")

        # 2. 設定ファイル・定義DBフォルダ（WindowsはPATHに無くても、既定のインストール先のclamdの隣を探す）
        clamd_path = paths.get("clamd") or install_paths.get("clamd")
        service_exe, service_conf = (None, None)
        if self._system == "Windows":
            service_exe, service_conf = self._windows_service_command()
            if service_exe:
                component_items.append(("clamd（サービス）", service_exe))
        conf_dirs = self._conf_dirs(clamd_path, service_exe, service_conf) if self._system == "Windows" else []
        if self._system == "Windows":
            # サービスが--config-fileで読む設定ファイルを最優先する（無ければ既定の場所を探す）
            config_file = service_conf if service_conf and Path(service_conf).is_file() else ""
        else:
            config_file = self.config.realtime.clamonacc_config_file
        if config_file and not Path(config_file).is_file():
            # 指定先が無い場合はその旨を記録し、既定の場所を探す（指定パスを「見つかった」と表示しない）
            checks.append(
                CheckResult(LEVEL_WARN, f"設定で指定された clamd.conf が見つかりません: {config_file}")
            )
            config_file = ""
        clamd_conf_path = find_clamd_conf(config_file, self._conf_candidates("clamd.conf", conf_dirs))
        # 見つけたパスをそのまま読む（候補をもう一度探すと、表示するパスと読む内容が食い違いうる）
        clamd_conf = load_clamd_conf(str(clamd_conf_path)) if clamd_conf_path else None
        freshclam_conf_path = find_clamd_conf("", self._conf_candidates("freshclam.conf", conf_dirs))
        report.add_sensitive(clamd_conf_path, "<clamd.conf>")
        report.add_sensitive(freshclam_conf_path, "<freshclam.conf>")
        database_dir = self._database_dir(clamd_conf, conf_dirs)
        definition_files = _definition_files(database_dir)
        component_items += [
            ("clamd.conf", str(clamd_conf_path) if clamd_conf_path else _NOT_FOUND),
            ("freshclam.conf", str(freshclam_conf_path) if freshclam_conf_path else _NOT_FOUND),
            ("定義DBフォルダ", str(database_dir) if database_dir else "不明"),
            ("定義ファイル", ", ".join(definition_files) if definition_files else "なし"),
        ]
        report.sections.append(("ClamAV コンポーネント", component_items))
        if clamd_conf is not None:
            checks.append(CheckResult(LEVEL_OK, f"clamd.conf を読み込みました: {clamd_conf_path}"))
            if not clamd_conf.is_enabled("AlertExceedsMax"):
                # 無効だと、上限を超えたファイルは検査されないまま「OK」と応答され、検査できなかったことが分からない
                checks.append(CheckResult(
                    LEVEL_WARN,
                    "AlertExceedsMax が clamd.conf で有効になっていません。サイズ等の上限（MaxFileSize・MaxScanSize 等）を"
                    "超えたファイルが、検査されないまま「問題なし」になります。AlertExceedsMax yes にすると、"
                    "スキャン結果に「未検査（サイズの上限）」として表示されます",
                ))
        else:
            checks.append(CheckResult(LEVEL_WARN, "clamd.conf が見つからないか、読み込めません"))
        # 3. clamd（エンジン サービス）。定義ファイルの判定に応答の有無を使うため先に確認する
        daemon_checks: list[CheckResult] = []
        daemon_items = self._daemon_items(daemon_checks)
        clamd_responding = any(c.level == LEVEL_OK for c in daemon_checks)
        if definition_files:
            checks.append(CheckResult(LEVEL_OK, f"定義ファイルがあります: {database_dir}"))
        elif clamd_responding:
            # clamdが定義を読み込んで応答している以上、定義が無いわけではない（探した場所が違うだけ）
            checks.append(
                CheckResult(LEVEL_WARN, "定義ファイルの場所を特定できません（clamd は定義を読み込んで応答しています）")
            )
        else:
            checks.append(
                CheckResult(LEVEL_NG, "定義ファイルが見つかりません。「ウイルス定義の更新」から定義を取得してください")
            )
        checks.extend(daemon_checks)
        report.sections.append(("ClamAV エンジン サービス (clamd)", daemon_items))
        self._check_listen_address(checks, clamd_conf, report)

        # 4. リアルタイム監視
        if self._realtime_manager is not None:
            report.sections.append((
                "リアルタイム監視",
                [
                    ("方式", getattr(self._realtime_manager, "mode_name", "-")),
                    ("状態", "監視中" if self._realtime_manager.is_running else "停止中"),
                ],
            ))
        return report

    def _add_config_sensitive(self, report: DiagnosticsReport) -> None:
        """設定から分かる伏せ字の対象（ソケット・clamdのホスト・指定したパス・アプリのフォルダ）"""
        config = self.config
        report.add_sensitive(config.clamd.unix_socket_path, _PLACEHOLDER_SOCKET)
        if is_sensitive_host(config.clamd.tcp_host):
            report.add_sensitive(config.clamd.tcp_host, _PLACEHOLDER_CLAMD_HOST)
        report.add_sensitive(config.realtime.clamonacc_config_file, "<clamd.conf>")
        if config.realtime.clamonacc_path != "clamonacc":
            report.add_sensitive(config.realtime.clamonacc_path, "<clamonaccのパス>")
        if self._app_dir is not None:
            report.add_sensitive(str(self._app_dir), "<アプリのフォルダ>")

    def _app_items(self) -> list[tuple[str, str]]:
        try:
            from PySide6 import __version__ as pyside_version
        except ImportError:  # pragma: no cover - PySide6は必須依存
            pyside_version = "-"
        items = [
            ("バージョン", f"{APP_NAME} {APP_VERSION}"),
            ("OS / プラットフォーム", f"{platform.platform()} ({platform.machine()})"),
            ("Python / PySide6", f"{platform.python_version()} / {pyside_version}"),
            ("実行形式", "実行ファイル（ビルド版）" if getattr(sys, "frozen", False) else "Pythonスクリプト"),
        ]
        if self._app_dir is not None:
            items.append(("アプリのフォルダ", str(self._app_dir)))
        return items

    def _daemon_items(self, checks: list[CheckResult]) -> list[tuple[str, str]]:
        endpoint = self._clamav_service.endpoint
        items = [("接続先", endpoint), ("OSのサービス", self._service_state())]
        try:
            responding = self._clamav_service.ping(retry=False)
        except Exception as exc:  # noqa: BLE001 - 診断のため、どの失敗も結果として記録する
            responding, detail = False, str(exc)
        else:
            detail = "" if responding else "PINGに応答しません"
        if not responding:
            items += [("応答", f"なし（{detail}）"), ("エンジン / 定義DB", "-")]
            checks.append(
                CheckResult(
                    LEVEL_NG,
                    f"clamd（{endpoint}）に接続できません。「ClamAV エンジン サービス」から起動してください",
                )
            )
            return items
        try:
            engine, db_version, db_date = self._clamav_service.get_version()
            version_text = f"{engine} / {db_version} ({db_date})"
        except Exception as exc:  # noqa: BLE001
            version_text = f"取得できません ({exc})"
        items += [("応答", "あり（PONG）"), ("エンジン / 定義DB", version_text)]
        checks.append(CheckResult(LEVEL_OK, f"clamd（{endpoint}）が応答しました"))
        return items


def _same_path(a: str, b: str) -> bool:
    """同じファイルを指すか（Linuxの/var/runは/runへのシンボリックリンクのことが多い）"""
    if a == b:
        return True
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except (OSError, ValueError):
        return False


def _same_host(listen_addr: str, host: str) -> bool:
    """clamd.confのTCPAddrで待ち受けているアドレスに、アプリの接続先ホストで届くか"""
    listen_addr, host = listen_addr.lower(), host.lower()
    if listen_addr == host or listen_addr in _ANY_ADDRS:
        return True
    return listen_addr in _LOOPBACK_HOSTS and host in _LOOPBACK_HOSTS


def _definition_files(directory: Path | None) -> list[str]:
    if directory is None:
        return []
    try:
        return sorted(p.name for p in directory.iterdir() if p.suffix.lower() in _DEFINITION_SUFFIXES)
    except OSError:
        return []
