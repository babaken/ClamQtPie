"""
clamd.confの読み取り（Linux: clamonaccの前提設定の確認用）

参照: Linuxリアルタイム監視詳細設計書 6章・11章
clamonaccはclamd.confのOnAccess*・TemporaryDirectory・LocalSocket等を読んで動作する。
設定の不足や組み合わせによっては、clamonaccが起動を拒否する・監視対象の指定を無視する・
監視を設定できないまま動き続ける（ClamAV 1.5.4のソースで確認）ため、起動前にアプリ側で確認する。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

#: clamonaccの--config-file未指定時に探す場所（ビルド時の既定値はディストリビューションにより異なる）
#: Debian/Ubuntu/Arch: /etc/clamav/clamd.conf、Fedora/RHEL: /etc/clamd.d/scan.conf
DEFAULT_CLAMD_CONF_CANDIDATES = ("/etc/clamav/clamd.conf", "/etc/clamd.d/scan.conf", "/etc/clamd.conf")

#: TemporaryDirectory未指定時にclamonaccが使う一時フォルダ（pkexecは環境変数TMPDIRを引き継がない）
DEFAULT_TEMPORARY_DIRECTORY = "/tmp"

_TRUE_VALUES = ("yes", "true", "1", "on")
_LOCAL_TCP_ADDRS = ("", "localhost", "127.0.0.1", "::1")


@dataclass
class ClamdConf:
    path: Path
    #: 小文字化したオプション名 → 値のリスト（同じオプションを複数回書ける項目があるため）
    options: dict[str, list[str]] = field(default_factory=dict)

    def get(self, name: str) -> str | None:
        """最後に書かれた値（未指定ならNone）"""
        values = self.options.get(name.lower())
        return values[-1] if values else None

    def get_all(self, name: str) -> list[str]:
        return list(self.options.get(name.lower(), []))

    def is_enabled(self, name: str) -> bool:
        value = self.get(name)
        return value is not None and value.lower() in _TRUE_VALUES

    @property
    def temporary_directory(self) -> str:
        return self.get("TemporaryDirectory") or DEFAULT_TEMPORARY_DIRECTORY

    @property
    def has_local_socket(self) -> bool:
        return self.get("LocalSocket") is not None

    @property
    def is_local_daemon(self) -> bool:
        """clamonaccから見てclamdが同じマシン上にあるか（clamonaccのonas_check_remote相当）"""
        if self.has_local_socket:
            return True
        tcp_addrs = self.get_all("TCPAddr") or [""]
        return any(addr.lower() in _LOCAL_TCP_ADDRS for addr in tcp_addrs)

    @property
    def has_on_access_exclude_user(self) -> bool:
        """clamd自身のアクセスを検査対象から外す設定があるか（ローカルのclamdではclamonaccの起動に必須）"""
        return (
            self.get("OnAccessExcludeUname") is not None
            or self.get("OnAccessExcludeUID") is not None
            or self.is_enabled("OnAccessExcludeRootUID")
        )


def parse_clamd_conf(text: str, path: Path) -> ClamdConf:
    conf = ClamdConf(path)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)  # 区切りは空白またはタブ
        name = parts[0]
        value = parts[1].strip() if len(parts) > 1 else ""
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        conf.options.setdefault(name.lower(), []).append(value)
    return conf


def find_clamd_conf(
    config_file: str = "", candidates: tuple[str, ...] = DEFAULT_CLAMD_CONF_CANDIDATES
) -> Path | None:
    """clamonaccが読むclamd.confの場所。config.realtime.clamonacc_config_fileの指定を優先する"""
    if config_file:
        return Path(config_file)
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return path
    return None


def load_clamd_conf(
    config_file: str = "", candidates: tuple[str, ...] = DEFAULT_CLAMD_CONF_CANDIDATES
) -> ClamdConf | None:
    """clamd.confを読む。見つからない・読めない場合はNone（確認を行わずclamonaccに任せる）"""
    path = find_clamd_conf(config_file, candidates)
    if path is None:
        return None
    try:
        return parse_clamd_conf(path.read_text(encoding="utf-8", errors="replace"), path)
    except OSError:
        return None
