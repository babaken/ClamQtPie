"""
SMTP パスワードなどの秘密の保存先（サービス化・Web API設計書 6.8節。段階 12-3b）

従来のアプリ（利用者の権限）は keyring（Windows の資格情報マネージャー・Linux の Secret Service）に保存する。
サービス（SYSTEM・root）では keyring を使えない（SYSTEM の資格情報ストアは利用者のものと別、Linux の root には
Secret Service がない）ため、データの置き場所の secrets/ にファイルで保存する。
- Windows: DPAPI（CRYPTPROTECT_LOCAL_MACHINE）で暗号化する。PC 単位の鍵なので、ファイルを読めれば
  この PC のどの利用者でも復号できる。実際に守っているのはファイルの ACL（SYSTEM・Administrators だけ）
- Linux: 0600 のファイル（root だけ）。同じ水準
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from core.private_files import make_private_dir, write_private_file

_IS_WINDOWS = sys.platform == "win32"
#: CryptProtectData の CRYPTPROTECT_LOCAL_MACHINE（PC 単位の鍵で暗号化する）
_CRYPTPROTECT_LOCAL_MACHINE = 0x4


class FileSecretStore:
    """名前ごとに 1 つのファイルで保存する（secrets/<名前>）"""

    def __init__(self, directory: str | os.PathLike) -> None:
        self.directory = Path(directory)

    def _path(self, name: str) -> Path:
        if not name.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"秘密の名前に使えない文字が含まれています: {name!r}")
        return self.directory / name

    def get(self, name: str) -> str | None:
        path = self._path(name)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        if _IS_WINDOWS:
            import win32crypt

            data = win32crypt.CryptUnprotectData(data, None, None, None, 0)[1]
        return data.decode("utf-8")

    def set(self, name: str, value: str) -> None:
        data = value.encode("utf-8")
        if _IS_WINDOWS:
            import win32crypt

            data = win32crypt.CryptProtectData(data, "ClamQtPie", None, None, None, _CRYPTPROTECT_LOCAL_MACHINE)
        make_private_dir(self.directory)
        write_private_file(self._path(name), data)

    def delete(self, name: str) -> None:
        try:
            self._path(name).unlink()
        except FileNotFoundError:
            pass
