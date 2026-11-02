"""
本人（Windows は加えて SYSTEM・Administrators）だけが使えるフォルダ・ファイルの作成

API のトークンファイル（api/auth.py）、サービスのデータの置き場所（core/service_paths.py）、
秘密の保存先（core/secret_store.py）で使う。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Windows のよく知られた SID（SYSTEM・Administrators）
SID_SYSTEM = "S-1-5-18"
SID_ADMINISTRATORS = "S-1-5-32-544"


def make_private_dir(path: Path) -> None:
    # リンクに置き換えられていたら、リンク先の権限を変えたり、そこへ書き出したりしない
    if path.is_symlink():
        raise OSError(f"フォルダがリンクになっています: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        restrict_windows_acl(path)
    else:
        os.chmod(path, 0o700)


def write_private_file(path: Path, data: bytes) -> None:
    """本人だけが読めるファイルとして書き出す（中身を書く前に権限を絞り、os.replace で置き換える）"""
    tmp = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        if sys.platform == "win32":
            restrict_windows_acl(tmp)
        else:
            os.fchmod(fd, 0o600)
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def restrict_windows_acl(path: Path) -> None:
    """アクセス権を「本人・SYSTEM・Administrators のフルコントロール」だけにし、親からの継承を切る"""
    import ntsecuritycon
    import win32api
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    owner_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    dacl = win32security.ACL()
    inherit = 0
    if path.is_dir():
        inherit = win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE
    for sid in (
        owner_sid,
        win32security.ConvertStringSidToSid(SID_SYSTEM),
        win32security.ConvertStringSidToSid(SID_ADMINISTRATORS),
    ):
        dacl.AddAccessAllowedAceEx(win32security.ACL_REVISION, inherit, ntsecuritycon.FILE_ALL_ACCESS, sid)
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )
