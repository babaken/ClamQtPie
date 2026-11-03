"""
トレイGUI を使える利用者（サービス化・Web API設計書 3.2節・12章 5。段階 12-4d）

トレイGUI は user.token と endpoint.json を読んでサービスにつなぐ。読めるのは ClamQtPie Users（Windows）・
clamqtpie（Linux）のグループに入っている利用者だけにする（インストーラが作り、導入した管理者を入れる）。
- share_with_users: サービスが起動のたびに作り直すトークン・接続先のファイルを、グループに読ませる
  （admin.token は読ませない）。グループが無ければ何もしない（開発中の確認用の導入）
- add_user・remove_user・list_users: clamqtpie-ctl users（net localgroup／groupadd・usermod・gpasswd）
- program_location_problem: サービスの実行ファイルとそのフォルダを、一般の利用者が書き換えられないか（12章 33）

Windows で Administrators に直接読ませないのは、UAC で昇格していない管理者では Administrators の SID が
「拒否専用」になり、許可が効かないため（グループのメンバーシップは昇格しなくても効く）。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from core.exec_path import child_env, system_tool

logger = logging.getLogger("clamav_frontend")

_IS_WINDOWS = sys.platform == "win32"
WINDOWS_GROUP = "ClamQtPie Users"
LINUX_GROUP = "clamqtpie"
#: グループに読ませるファイル（api/ の中）
SHARED_FILES = ("user.token", "endpoint.json")
#: 利用者名として受け付ける形（コマンドの引数に渡すため、決まった文字だけにする）
_USER_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}(\\[A-Za-z0-9_][A-Za-z0-9_.\- ]{0,63})?$")


class AccessError(RuntimeError):
    pass


def is_windows_error(exc: BaseException) -> bool:
    """pywin32 の Windows のエラー（pywintypes.error）か。配布版（PyInstaller）では pywintypes が二重に読み込まれ、
    except pywintypes.error で捕まえられないことがあるため（12-4d のビルドで発見）、winerror の有無で見分ける"""
    return isinstance(getattr(exc, "winerror", None), int) and not isinstance(exc, OSError)


def group_name() -> str:
    return WINDOWS_GROUP if _IS_WINDOWS else LINUX_GROUP


# -- トークンをグループに読ませる（サービス） ----------------------------------------------------
def share_with_users(data_dir: Path, api_dir: Path) -> bool:
    """user.token・endpoint.json をグループに読ませる。グループが無ければ False（何もしない）"""
    if _IS_WINDOWS:
        return _share_windows(api_dir)
    return _share_linux(data_dir, api_dir)


def _share_linux(data_dir: Path, api_dir: Path) -> bool:
    import grp

    try:
        gid = grp.getgrnam(LINUX_GROUP).gr_gid
    except KeyError:
        logger.info(f"グループ {LINUX_GROUP} が無いため、トレイGUI からはつなげません（install.sh で作ります）")
        return False
    # フォルダは通り抜けるだけ（data_dir）・一覧を読めるだけ（api）。書き込みは root だけのまま
    for directory, mode in ((data_dir, 0o710), (api_dir, 0o750)):
        if directory.is_symlink():
            raise AccessError(f"フォルダがリンクになっています: {directory}")
        os.chown(directory, 0, gid)
        os.chmod(directory, mode)
    for name in SHARED_FILES:
        path = api_dir / name
        if path.is_symlink() or not path.exists():
            continue
        os.chown(path, 0, gid, follow_symlinks=False)
        os.chmod(path, 0o640)
    return True


def _share_windows(api_dir: Path) -> bool:
    import ntsecuritycon
    import win32security

    try:
        group_sid = win32security.LookupAccountName(None, WINDOWS_GROUP)[0]
    except Exception as exc:  # noqa: BLE001 - pywintypes.error（配布版では別の型として読み込まれることがある）
        if not is_windows_error(exc):
            raise
        logger.info(f"グループ {WINDOWS_GROUP} が無いため、トレイGUI からはつなげません（インストーラで作ります）")
        return False
    read = ntsecuritycon.FILE_GENERIC_READ
    for name in SHARED_FILES:
        path = api_dir / name
        if not path.exists():
            continue
        descriptor = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
        dacl = descriptor.GetSecurityDescriptorDacl()
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, read, group_sid)
        win32security.SetNamedSecurityInfo(
            str(path), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, dacl, None)
    return True


# -- clamqtpie-ctl users ---------------------------------------------------------------------
def _check_name(name: str) -> str:
    if not _USER_NAME.match(name):
        raise AccessError(f"利用者名が正しくありません: {name!r}")
    return name


def _run(cmd: list[str], runner=subprocess.run) -> subprocess.CompletedProcess:
    kwargs = {"capture_output": True, "timeout": 30}
    if _IS_WINDOWS:
        from core.clamd_process import console_text_kwargs

        kwargs.update(console_text_kwargs("Windows"), env=child_env(None, "Windows"))
    else:
        kwargs.update(text=True, errors="replace")
    try:
        return runner(cmd, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AccessError(f"{cmd[0]} を実行できません ({exc})") from exc


def ensure_group(runner=subprocess.run) -> None:
    """グループが無ければ作る"""
    if _IS_WINDOWS:
        net = system_tool("net", "Windows")
        if _run([net, "localgroup", WINDOWS_GROUP], runner).returncode != 0:
            result = _run([net, "localgroup", WINDOWS_GROUP, "/add",
                           "/comment:ClamQtPie のトレイGUI を使える利用者"], runner)
            if result.returncode != 0:
                raise AccessError(f"グループ {WINDOWS_GROUP} を作れません: {(result.stderr or result.stdout).strip()}")
        return
    if _run([system_tool("getent"), "group", LINUX_GROUP], runner).returncode != 0:
        result = _run([system_tool("groupadd"), "--system", LINUX_GROUP], runner)
        if result.returncode != 0:
            raise AccessError(f"グループ {LINUX_GROUP} を作れません: {(result.stderr or '').strip()}")


def add_user(name: str, runner=subprocess.run) -> None:
    _check_name(name)
    ensure_group(runner)
    if _IS_WINDOWS:
        cmd = [system_tool("net", "Windows"), "localgroup", WINDOWS_GROUP, name, "/add"]
    else:
        cmd = [system_tool("usermod"), "-aG", LINUX_GROUP, name]
    result = _run(cmd, runner)
    if result.returncode != 0:
        raise AccessError(f"{name} をグループに入れられません: {(result.stderr or result.stdout or '').strip()}")


def remove_user(name: str, runner=subprocess.run) -> None:
    _check_name(name)
    if _IS_WINDOWS:
        cmd = [system_tool("net", "Windows"), "localgroup", WINDOWS_GROUP, name, "/delete"]
    else:
        cmd = [system_tool("gpasswd"), "-d", name, LINUX_GROUP]
    result = _run(cmd, runner)
    if result.returncode != 0:
        raise AccessError(f"{name} をグループから外せません: {(result.stderr or result.stdout or '').strip()}")


def list_users(runner=subprocess.run) -> list[str]:
    if _IS_WINDOWS:
        result = _run([system_tool("net", "Windows"), "localgroup", WINDOWS_GROUP], runner)
        if result.returncode != 0:
            return []
        lines = [line.strip() for line in (result.stdout or "").splitlines()]
        # 区切りの行（-----）の後から、最後の完了の行の前まで
        try:
            start = next(i for i, line in enumerate(lines) if line.startswith("---")) + 1
        except StopIteration:
            return []
        members = [line for line in lines[start:] if line]
        return members[:-1]  # 最後の行は「コマンドは正常に終了しました。」
    result = _run([system_tool("getent"), "group", LINUX_GROUP], runner)
    if result.returncode != 0:
        return []
    members = (result.stdout or "").strip().split(":")[-1]
    return [m for m in members.split(",") if m]


# -- サービスの実行ファイルの置き場所（12章 33） -------------------------------------------------
def program_location_problem(executable: Path) -> str | None:
    """サービスの実行ファイルとそのフォルダを、一般の利用者が書き換えられるなら、その理由（12章 33）。
    差し替えられると、次の起動から SYSTEM・root の権限で別のプログラムが動くため"""
    from core.service_paths import writable_by_others

    for path in (executable, executable.parent):
        problem = writable_by_others(path)
        if problem:
            return f"{path}: {problem}"
    return None
