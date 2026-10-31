"""
外部コマンドの場所を決める

ディレクトリの付いていない名前でコマンドを起動すると、WindowsのCreateProcessは起動したプロセスのexeのフォルダや
カレントディレクトリからも探す（shutil.whichもWindowsではカレントディレクトリを先に探す）。アプリは管理者権限で
動くため、そこに同じ名前のファイルを置かれると、それが管理者権限で実行されてしまう。そのため外部コマンドは
絶対パスで起動する。
- OSのコマンド（sc、powershell、systemctl等）: Windowsはシステムのフォルダ（GetSystemDirectoryW）、
  Linux等は決まったディレクトリから探す。環境変数（SystemRoot・PATH）は使わない（利用者が変えられるため）
- ClamAVのコマンド（freshclam等）: Windowsは ClamAVの導入先（clamdサービスの実行ファイルのフォルダ、
  Program Files\\ClamAV）を先に探し、次にPATHのうち絶対パスの項目だけを探す（カレントディレクトリは探さない）
"""
from __future__ import annotations

import ntpath
import os
import platform
import posixpath
import stat

from core.exceptions import UnsafeExecutablePathError

#: Windowsのシステムのフォルダからの相対パス
_WINDOWS_SYSTEM_TOOLS = {
    "sc": "sc.exe",
    "powershell": r"WindowsPowerShell\v1.0\powershell.exe",
}
#: Linux等でOSのコマンドを探すディレクトリ（root所有の場所）
_POSIX_SYSTEM_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/usr/local/bin", "/usr/local/sbin")
_FALLBACK_WINDOWS_SYSTEM_DIR = r"C:\Windows\System32"


def windows_system_dir() -> str:
    """Windowsのシステムのフォルダ（通常 C:\\Windows\\System32）。環境変数ではなくAPIから得る"""
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(260)
        length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
        if 0 < length < len(buffer):
            return buffer.value
    except (AttributeError, OSError):
        pass  # Windows以外（テストでsystem="Windows"を指定した場合）
    return _FALLBACK_WINDOWS_SYSTEM_DIR


def system_tool(name: str, system: str | None = None) -> str:
    """OSのコマンドの絶対パス（Windows: sc・powershell、Linux等: systemctl・pkexec等）"""
    system = system or platform.system()
    if system == "Windows":
        return ntpath.join(windows_system_dir(), _WINDOWS_SYSTEM_TOOLS.get(name, f"{name}.exe"))
    for directory in _POSIX_SYSTEM_DIRS:
        candidate = posixpath.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    # 見つからない場合も相対名にはしない（起動に失敗し、呼び出し側のエラー処理で知らせる）
    return posixpath.join(_POSIX_SYSTEM_DIRS[0], name)


def find_system_tool(name: str, system: str | None = None) -> str | None:
    """OSのコマンドを決まった場所（system_toolと同じ）だけから探す。見つからなければNone。
    昇格に使うpkexec等の有無の確認に使う（PATHは利用者が変えられるため使わない）"""
    path = system_tool(name, system)
    return path if os.path.isfile(path) else None


def child_env(extra: dict[str, str] | None = None, system: str | None = None) -> dict[str, str] | None:
    """外部コマンドに渡す環境変数（subprocessのenv）。Windows以外はNone（そのまま引き継ぐ）

    Windowsではアプリは管理者権限で動くが、その環境変数には、昇格していない利用者が変えられる値
    （利用者の環境変数）がそのまま入っている。外部コマンドが読み込むDLL・設定ファイルの場所を変えられないよう、
    動作に必要な決まった変数だけを、環境変数ではなくAPIから得た値で渡す（extraは呼び出し側が追加する値）。
    Linux等: アプリは一般ユーザーで動き、rootで動かすものはpkexecが環境変数を整理するため、引き継ぐ。
    """
    system = system or platform.system()
    if system != "Windows":
        return None
    system_dir = windows_system_dir()
    windows_dir = ntpath.dirname(system_dir)
    env = {
        "SystemRoot": windows_dir,
        "windir": windows_dir,
        "SystemDrive": ntpath.splitdrive(windows_dir)[0] or "C:",
        "ComSpec": ntpath.join(system_dir, "cmd.exe"),
        "PATH": ";".join([
            system_dir,
            windows_dir,
            ntpath.join(system_dir, "Wbem"),
            ntpath.join(system_dir, "WindowsPowerShell", "v1.0"),
        ]),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "TEMP": ntpath.join(windows_dir, "Temp"),
        "TMP": ntpath.join(windows_dir, "Temp"),
        "OS": "Windows_NT",
        "PROCESSOR_ARCHITECTURE": platform.machine() or "AMD64",
        "NUMBER_OF_PROCESSORS": str(os.cpu_count() or 1),
    }
    env.update(_windows_user_folders())
    env.update(extra or {})
    return env


def _windows_user_folders() -> dict[str, str]:
    """利用者のフォルダ（PowerShell等が使う）。環境変数ではなくAPI（SHGetFolderPathW）から得る"""
    folders = {"USERPROFILE": 0x0028, "APPDATA": 0x001A, "LOCALAPPDATA": 0x001C}  # CSIDL_PROFILE等
    result: dict[str, str] = {}
    try:
        import ctypes

        for name, csidl in folders.items():
            buffer = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buffer) == 0 and buffer.value:
                result[name] = buffer.value
    except (AttributeError, OSError):
        pass  # Windows以外（テストでsystem="Windows"を指定した場合）
    return result


def windows_clamav_dirs() -> list[str]:
    """WindowsでClamAVのコマンドを探すフォルダ（clamdサービスの実行ファイルのフォルダ、Program Files\\ClamAV）"""
    dirs: list[str] = []
    try:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\clamd") as key:
                image_path, _ = winreg.QueryValueEx(key, "ImagePath")
            exe = _first_token(image_path)
            # 環境変数（%SystemRoot%等）は利用者が上書きできるため展開しない（含む場合は使わない）
            if exe and "%" not in exe and ntpath.isabs(exe):
                dirs.append(ntpath.dirname(exe))
        except OSError:
            pass
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion") as key:
                program_files, _ = winreg.QueryValueEx(key, "ProgramFilesDir")
            dirs.append(ntpath.join(program_files, "ClamAV"))
        except OSError:
            pass
    except ImportError:
        pass
    return list(dict.fromkeys(dirs))


def _first_token(command_line: str) -> str:
    """サービスのImagePath（"C:\\Program Files\\ClamAV\\clamd.exe" --service 等）から実行ファイルの部分を取り出す"""
    command_line = command_line.strip()
    if command_line.startswith('"'):
        end = command_line.find('"', 1)
        return command_line[1:end] if end > 0 else command_line[1:]
    lower = command_line.lower()
    index = lower.find(".exe")
    return command_line[: index + 4] if index >= 0 else command_line.split(" ")[0]


def find_program(name: str, extra_dirs: list[str] | tuple[str, ...] = (), system: str | None = None) -> str | None:
    """プログラムの絶対パスを探す。見つからなければNone

    ディレクトリ付きの名前はそのまま（存在すれば）返す。名前だけの場合は、extra_dirs → PATHのうち絶対パスの
    項目の順に探す。カレントディレクトリ・相対パスの項目は探さない。
    """
    system = system or platform.system()
    pathmod = ntpath if system == "Windows" else posixpath
    if pathmod.dirname(name):
        return name if os.path.isfile(name) else None
    if system == "Windows":
        names = [name] if pathmod.splitext(name)[1] else [f"{name}.exe", f"{name}.com"]
    else:
        names = [name]
    path_dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and pathmod.isabs(d)]
    for directory in [*extra_dirs, *path_dirs]:
        for candidate_name in names:
            candidate = pathmod.join(directory, candidate_name)
            if os.path.isfile(candidate) and (system == "Windows" or os.access(candidate, os.X_OK)):
                return candidate
    return None


# -- 実行ファイル・設定ファイルの場所の検証 ------------------------------------------
# freshclam・clamonaccは、管理者権限（Windowsのアプリ本体）やroot（pkexec）で実行される。一般ユーザーが書き換え
# られる場所のファイルを実行すると、その内容が管理者・rootの権限で動いてしまうため、実行の直前に場所を確かめる。

#: 書き込めてよいアカウント（SYSTEM、Administrators、TrustedInstaller）
_WINDOWS_TRUSTED_SIDS = frozenset({
    "S-1-5-18",
    "S-1-5-32-544",
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
})
#: 書き込み・置き換えにつながるアクセス権（データの書き込み・追記、削除、子の削除、DACL・所有者の変更、GENERIC_WRITE/ALL）
_WINDOWS_WRITE_MASK = 0x2 | 0x4 | 0x40 | 0x10000 | 0x40000 | 0x80000 | 0x10000000 | 0x40000000
_ACCESS_ALLOWED_ACE_TYPE = 0
_INHERIT_ONLY_ACE = 0x8


def validate_executable(path: str, expected_names: tuple[str, ...], system: str | None = None) -> str:
    """実行ファイルの場所を確かめる。問題があればUnsafeExecutablePathError（E6005）

    - 絶対パスで、通常のファイルであること
    - ファイル名が想定どおり（expected_names。Windowsは大文字・小文字を区別せず、.exeの有無を問わない）
    - ファイルとその親のフォルダに、管理者（Linux等はroot）以外が書き込めないこと
    リンクは解決した先を確かめる。確かめたパス（リンクを解決したもの）を返す。
    """
    system = system or platform.system()
    pathmod = ntpath if system == "Windows" else posixpath
    name = pathmod.basename(path)
    if system == "Windows":
        stem = name.lower().removesuffix(".exe")
        name_ok = stem in {n.lower() for n in expected_names}
    else:
        name_ok = name in expected_names
    if not name_ok:
        raise UnsafeExecutablePathError(detail=f"想定していない名前の実行ファイルです（{'・'.join(expected_names)} を指定してください）: {path}")
    return validate_trusted_file(path, system)


def validate_trusted_file(path: str, system: str | None = None) -> str:
    """root・管理者の権限で読み込む・実行するファイルの場所を確かめる（名前は確かめない）。
    問題があればUnsafeExecutablePathError（E6005）。確かめたパス（リンクを解決したもの）を返す"""
    system = system or platform.system()
    pathmod = ntpath if system == "Windows" else posixpath
    if not pathmod.isabs(path):
        raise UnsafeExecutablePathError(detail=f"絶対パスではありません: {path}")
    real = os.path.realpath(path)
    if not os.path.isfile(real):
        raise UnsafeExecutablePathError(detail=f"通常のファイルが見つかりません: {path}")
    problems = []
    for target, is_dir in ((real, False), (os.path.dirname(real), True)):
        problems += _untrusted_writers(target, is_dir, system)
    if system != "Windows":
        # Linux等では、祖先のフォルダに書き込めれば、その下のフォルダの名前を変えて丸ごと差し替えられるため、
        # /までのすべての祖先も確かめる（Windowsは、祖先のフォルダの権限だけでは子のフォルダの名前を変えられない）
        problems += _posix_untrusted_ancestors(os.path.dirname(os.path.dirname(real)))
    if problems:
        raise UnsafeExecutablePathError(detail=f"{path}: " + "、".join(problems))
    return real


def _untrusted_writers(path: str, is_dir: bool, system: str) -> list[str]:
    if system == "Windows":
        return _windows_untrusted_writers(path, is_dir)
    return _posix_untrusted_writers(path, is_dir)


def _posix_untrusted_writers(path: str, is_dir: bool) -> list[str]:
    info = os.stat(path)
    kind = "フォルダ" if is_dir else "ファイル"
    problems = []
    if info.st_uid != 0:
        problems.append(f"{kind}の所有者がrootではありません（{path}）")
    group_writable = info.st_mode & stat.S_IWGRP and info.st_gid != 0
    if info.st_mode & stat.S_IWOTH or group_writable:
        problems.append(f"{kind}にroot以外が書き込めます（{path}）")
    return problems


def _posix_untrusted_ancestors(directory: str) -> list[str]:
    """directoryから/までの各フォルダの問題（所有者がrootでない・root以外が書き込める）"""
    problems = []
    while True:
        problems += _posix_untrusted_writers(directory, True)
        parent = os.path.dirname(directory)
        if parent == directory:
            return problems
        directory = parent


def _windows_untrusted_writers(path: str, is_dir: bool) -> list[str]:
    try:
        import win32security
    except ImportError:
        return ["アクセス権を確かめられません（pywin32がありません）"]
    kind = "フォルダ" if is_dir else "ファイル"
    try:
        descriptor = win32security.GetNamedSecurityInfo(
            path,
            win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
        )
    except Exception as exc:  # noqa: BLE001 - pywintypes.error等
        return [f"{kind}のアクセス権を読み取れません（{path}: {exc}）"]
    problems = []
    owner = win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
    if owner not in _WINDOWS_TRUSTED_SIDS:
        problems.append(f"{kind}の所有者が管理者ではありません（{path}）")
    dacl = descriptor.GetSecurityDescriptorDacl()
    if dacl is None:
        return problems + [f"{kind}に誰でも書き込めます（{path}）"]
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        (ace_type, ace_flags), mask = ace[0], ace[1]
        if ace_type != _ACCESS_ALLOWED_ACE_TYPE or ace_flags & _INHERIT_ONLY_ACE:
            continue
        sid = win32security.ConvertSidToStringSid(ace[2])
        if mask & _WINDOWS_WRITE_MASK and sid not in _WINDOWS_TRUSTED_SIDS:
            problems.append(f"{kind}に管理者以外（{_account_name(ace[2], sid)}）が書き込めます（{path}）")
    return problems


def _account_name(sid, sid_text: str) -> str:
    try:
        import win32security

        name, domain, _ = win32security.LookupAccountSid(None, sid)
        return f"{domain}\\{name}" if domain else name
    except Exception:  # noqa: BLE001
        return sid_text
