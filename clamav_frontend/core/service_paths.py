"""
サービスのデータの置き場所と、その権限の確認（サービス化・Web API設計書 9章・9.3節。段階 12-3a）

| 用途 | Windows | Linux |
|---|---|---|
| 設定 | %ProgramData%\\ClamQtPie\\config.json | /etc/clamqtpie/config.json |
| DB・隔離・API のトークン・clamonacc のリスト | %ProgramData%\\ClamQtPie\\ | /var/lib/clamqtpie/ |
| ログ | %ProgramData%\\ClamQtPie\\logs\\ | /var/log/clamqtpie/ |

サービスは SYSTEM・root で動く。データの置き場所を一般の利用者が書き換えられると、設定（実行するプログラム等）や
隔離ファイルを差し替えて、SYSTEM・root の権限で使わせることができてしまう。そのため、起動のたびに
置き場所の所有者と権限を確かめ、一般の利用者が書き込めるようになっていたら起動しない（12章 32・33）。
`C:\\ProgramData` の直下は一般の利用者もフォルダを作れるため、導入より前に作られていた場合も同じく起動しない。

開発中の確認（service_main.py --console --data-dir DIR）では、すべてを DIR の下に置き、権限の確認は警告だけにする。
"""
from __future__ import annotations

import logging
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("clamav_frontend")

_IS_WINDOWS = sys.platform == "win32"

# Windows のよく知られた SID
_SID_SYSTEM = "S-1-5-18"
_SID_ADMINISTRATORS = "S-1-5-32-544"
_SID_TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
#: 書き込みを許してよい SID（それ以外に書き込みの権限があれば、一般の利用者が書けるとみなす）
_TRUSTED_SIDS = (_SID_SYSTEM, _SID_ADMINISTRATORS, _SID_TRUSTED_INSTALLER, "S-1-3-0")  # S-1-3-0: CREATOR OWNER


class UnsafeDataDirectoryError(RuntimeError):
    """データの置き場所を一般の利用者が書き換えられる"""


@dataclass(frozen=True)
class ServicePaths:
    config_file: Path
    data_dir: Path
    log_dir: Path

    @property
    def db_file(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def quarantine_dir(self) -> Path:
        return self.data_dir / "quarantine"

    @property
    def api_dir(self) -> Path:
        return self.data_dir / "api"

    @property
    def config_dir(self) -> Path:
        return self.config_file.parent

    def directories(self) -> list[Path]:
        """作成・確認するフォルダ（親から順に）"""
        dirs = [self.config_dir, self.data_dir, self.log_dir, self.quarantine_dir, self.api_dir]
        return list(dict.fromkeys(dirs))

    @classmethod
    def default(cls) -> "ServicePaths":
        if _IS_WINDOWS:
            base = Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "ClamQtPie"
            return cls(config_file=base / "config.json", data_dir=base, log_dir=base / "logs")
        return cls(config_file=Path("/etc/clamqtpie/config.json"), data_dir=Path("/var/lib/clamqtpie"),
                   log_dir=Path("/var/log/clamqtpie"))

    @classmethod
    def under(cls, base: str | os.PathLike) -> "ServicePaths":
        """開発中の確認用: すべてを base の下に置く"""
        base = Path(base)
        return cls(config_file=base / "config.json", data_dir=base, log_dir=base / "logs")


def prepare_directories(paths: ServicePaths, strict: bool) -> None:
    """フォルダが無ければ作り（本人・SYSTEM・Administrators／root だけが使える権限で）、権限を確かめる。
    strict（サービスとして動くとき）なら、一般の利用者が書き込めるフォルダがあれば UnsafeDataDirectoryError"""
    for directory in paths.directories():
        if directory.is_symlink():
            _problem(f"データのフォルダがリンクになっています: {directory}", strict)
            continue
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            _restrict(directory)
        problem = writable_by_others(directory)
        if problem:
            _problem(f"データのフォルダを一般の利用者が書き換えられます: {directory}（{problem}）", strict)


def _problem(message: str, strict: bool) -> None:
    if strict:
        raise UnsafeDataDirectoryError(message)
    logger.warning(message)


def _restrict(directory: Path) -> None:
    if _IS_WINDOWS:
        from api.auth import _restrict_windows_acl

        _restrict_windows_acl(directory)
    else:
        os.chmod(directory, 0o700)


def writable_by_others(directory: Path) -> str | None:
    """一般の利用者が書き込めるなら、その理由。問題なければ None"""
    if _IS_WINDOWS:
        return _win_writable_by_others(directory)
    st = os.lstat(directory)
    if hasattr(os, "geteuid") and os.geteuid() == 0 and st.st_uid != 0:
        return f"所有者が root ではありません（uid {st.st_uid}）"
    if st.st_uid != os.getuid():
        return f"所有者が実行している利用者ではありません（uid {st.st_uid}）"
    if stat.S_IMODE(st.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
        return f"グループ・その他に書き込みの権限があります（{oct(stat.S_IMODE(st.st_mode))}）"
    return None


def _win_writable_by_others(directory: Path) -> str | None:
    import ntsecuritycon
    import win32api
    import win32security

    info = win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION
    descriptor = win32security.GetFileSecurity(str(directory), info)
    owner = win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    me = win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, win32security.TokenUser)[0])
    trusted = set(_TRUSTED_SIDS) | {me}
    if owner not in trusted:
        return f"所有者が SYSTEM・Administrators ではありません（{owner}）"
    dacl = descriptor.GetSecurityDescriptorDacl()
    if dacl is None:
        return "アクセス権の一覧（DACL）がなく、誰でも書き込めます"
    write_mask = (ntsecuritycon.FILE_WRITE_DATA | ntsecuritycon.FILE_APPEND_DATA | ntsecuritycon.GENERIC_WRITE
                  | ntsecuritycon.GENERIC_ALL | ntsecuritycon.WRITE_DAC | ntsecuritycon.WRITE_OWNER
                  | ntsecuritycon.DELETE | ntsecuritycon.FILE_ADD_FILE | ntsecuritycon.FILE_ADD_SUBDIRECTORY)
    for index in range(dacl.GetAceCount()):
        (ace_type, _flags), mask, sid = dacl.GetAce(index)[0], dacl.GetAce(index)[1], dacl.GetAce(index)[2]
        if ace_type != win32security.ACCESS_ALLOWED_ACE_TYPE or not mask & write_mask:
            continue
        sid_text = win32security.ConvertSidToStringSid(sid)
        if sid_text not in trusted:
            return f"{sid_text} に書き込みの権限があります"
    return None
