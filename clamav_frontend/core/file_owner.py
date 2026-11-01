"""
隔離したファイルの所有者・権限の記録と復帰（サービス化・Web API設計書 6.3節。段階 12-2d）

サービス（SYSTEM・root）が隔離・復元すると、復元したファイルが SYSTEM・root の所有になり、元の利用者が
使えなくなる（Linux では、元の権限のまま root の所有になると、setuid のファイルが root で動くようになる）。
そのため、隔離するときに元の所有者と権限を記録し、復元したときに戻す。

- Linux 等: uid・gid・mode。setuid・setgid のビットは戻さない。所有者を変えられない（root でない）ときは、
  権限だけを戻す
- Windows: セキュリティ記述子（所有者・DACL）を SDDL で。所有者を変える権限が無いときは DACL だけを戻す
- 戻せなくても復元は失敗にしない（ログに残す）
"""
from __future__ import annotations

import json
import logging
import os
import stat
import sys

logger = logging.getLogger("clamav_frontend")

_IS_WINDOWS = sys.platform == "win32"


def capture(path: str | os.PathLike) -> str | None:
    """path（リンクはたどらない）の所有者・権限を JSON の文字列で返す。取れなければ None"""
    try:
        if _IS_WINDOWS:
            return json.dumps({"sddl": _win_get_sddl(os.fspath(path))})
        st = os.lstat(path)
        return json.dumps({"uid": st.st_uid, "gid": st.st_gid, "mode": stat.S_IMODE(st.st_mode)})
    except Exception as exc:  # noqa: BLE001 - 記録できなくても隔離は続ける
        logger.warning(f"隔離するファイルの所有者・権限を記録できませんでした (detail: {exc})")
        return None


def apply(path: str | os.PathLike, owner_info: str | None) -> None:
    """capture() の記録を path に戻す（best-effort）"""
    if not owner_info:
        return
    try:
        info = json.loads(owner_info)
        if _IS_WINDOWS:
            if "sddl" in info:
                _win_set_sddl(os.fspath(path), info["sddl"])
            return
        if "mode" not in info:
            return
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            os.chown(path, int(info["uid"]), int(info["gid"]), follow_symlinks=False)
        # setuid・setgid は戻さない（復元したファイルが別の利用者の権限で動かないように）
        os.chmod(path, int(info["mode"]) & ~(stat.S_ISUID | stat.S_ISGID) & 0o7777)
    except Exception as exc:  # noqa: BLE001 - 戻せなくても復元は成功とする
        logger.warning(f"復元したファイルの所有者・権限を元に戻せませんでした (detail: {exc})")


def _win_get_sddl(path: str) -> str:
    import win32security

    info = win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION
    descriptor = win32security.GetFileSecurity(path, info)
    return win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
        descriptor, win32security.SDDL_REVISION_1, info)


def _win_set_sddl(path: str, sddl: str) -> None:
    import pywintypes
    import win32security

    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(sddl, win32security.SDDL_REVISION_1)
    owner = descriptor.GetSecurityDescriptorOwner()
    dacl = descriptor.GetSecurityDescriptorDacl()
    try:
        win32security.SetNamedSecurityInfo(
            path, win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
            owner, None, dacl, None)
    except pywintypes.error:
        # 所有者を変える権限が無い（管理者でない）ときは、アクセス権だけを戻す
        win32security.SetNamedSecurityInfo(
            path, win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION,
            None, None, dacl, None)
