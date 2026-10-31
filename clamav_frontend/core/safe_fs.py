"""
リンクをたどらないファイル操作（隔離・復元用）

隔離・復元はアプリの権限（Windowsでは管理者）で、利用者が書き込める場所のファイルを移動する。
確認してから移動するまでの間に、ファイルや途中のフォルダをジャンクション・シンボリックリンクに
置き換えられると、別の場所のファイルを移動・上書きしてしまうため、次のようにする。
- Windows: ファイル・フォルダをFILE_FLAG_OPEN_REPARSE_POINTで開き、リパースポイントでないこと、
  GetFinalPathNameByHandleで得た実際のパスが指定したパスと同じこと（途中にリンクが無いこと）を確かめる。
  ファイルは開いたハンドルに対して名前を変える（FileRenameInfo）。復元先のフォルダは、共有モードに
  FILE_SHARE_DELETEを含めずに開いておく（開いている間は、そのフォルダと上位のフォルダの名前を変えられない）。
- Linux等: ルートから1段ずつO_NOFOLLOWでフォルダを開き、開いたフォルダのfdを基準に操作する
  （openat/renameat/unlinkat相当）。移動した後に、開いたファイルと同じもの（st_dev・st_ino）かを確かめる。
条件を満たさない場合はUnsafePathError（OSErrorの派生）を送出する。
"""
from __future__ import annotations

import errno
import hashlib
import os
import platform
import stat

_CHUNK = 1024 * 1024
_IS_WINDOWS = platform.system() == "Windows"


class UnsafePathError(OSError):
    """リンクを含む・通常のファイルでない等の理由で、安全に操作できないパス"""


def quarantine_move(src: str | os.PathLike, dest: str | os.PathLike) -> tuple[str, int]:
    """srcをリンクをたどらずに開き、そのファイル自体をdest（隔離フォルダ内の新しい名前）へ移す

    移したファイルのSHA-256（16進）と大きさを返す。destのフォルダはアプリ自身の隔離フォルダとする。
    """
    if _IS_WINDOWS:
        return _win_quarantine_move(os.path.abspath(src), os.path.abspath(dest))
    return _posix_quarantine_move(os.path.abspath(src), os.path.abspath(dest))


def restore_move(src: str | os.PathLike, target: str | os.PathLike, overwrite: bool) -> None:
    """隔離フォルダのsrcを、リンクをたどらずに開いた復元先のフォルダの中のtargetとして置く

    復元先のフォルダが無ければ作る。targetに既にあるものが通常のファイルでない（リンク・フォルダ等）場合は、
    overwriteでも置き換えない（UnsafePathError）。overwrite=Falseで既にある場合はFileExistsError。
    """
    if _IS_WINDOWS:
        _win_restore_move(os.path.abspath(src), os.path.abspath(target), overwrite)
    else:
        _posix_restore_move(os.path.abspath(src), os.path.abspath(target), overwrite)


# -- Windows ---------------------------------------------------------------
_DELETE = 0x00010000
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_ATTRIBUTE_DIRECTORY = 0x0010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_ERROR_NOT_SAME_DEVICE = 17


def _win_error(exc, path: str) -> OSError:
    """pywintypes.errorを、winerrorから対応する派生クラス（FileNotFoundError等）になるOSErrorに変える"""
    return OSError(None, exc.strerror, path, exc.winerror)


def _final_path(handle) -> str:
    import win32file

    path = win32file.GetFinalPathNameByHandle(handle, 0)  # FILE_NAME_NORMALIZED | VOLUME_NAME_DOS
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _win_check_handle(handle, expected: str, want_dir: bool) -> None:
    import win32file

    attrs = win32file.GetFileInformationByHandle(handle)[0]
    if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise UnsafePathError(f"リンク（ジャンクション・シンボリックリンク等）のため操作しません: {expected}")
    if bool(attrs & _FILE_ATTRIBUTE_DIRECTORY) != want_dir:
        kind = "フォルダ" if want_dir else "通常のファイル"
        raise UnsafePathError(f"{kind}ではないため操作しません: {expected}")
    if not _same_path(_final_path(handle), expected):
        raise UnsafePathError(f"経路にリンク（ジャンクション・シンボリックリンク等）が含まれるため操作しません: {expected}")


def _win_quarantine_move(src: str, dest: str) -> tuple[str, int]:
    import pywintypes
    import win32con
    import win32file

    try:
        # 共有モードは読み取りだけ（隔離が終わるまで、他のプロセスが書き換え・名前の変更・削除をできない）
        handle = win32file.CreateFile(
            src,
            win32con.GENERIC_READ | _DELETE,
            win32con.FILE_SHARE_READ,
            None,
            win32con.OPEN_EXISTING,
            win32file.FILE_FLAG_OPEN_REPARSE_POINT | win32file.FILE_FLAG_SEQUENTIAL_SCAN,
            None,
        )
    except pywintypes.error as exc:
        raise _win_error(exc, src) from exc
    try:
        _win_check_handle(handle, src, want_dir=False)
        hasher = hashlib.sha256()
        size = 0
        while True:
            _, data = win32file.ReadFile(handle, _CHUNK)
            if not data:
                break
            hasher.update(data)
            size += len(data)
        try:
            win32file.SetFileInformationByHandle(
                handle,
                win32file.FileRenameInfo,
                {"ReplaceIfExists": False, "RootDirectory": None, "FileName": dest},
            )
        except pywintypes.error as exc:
            if exc.winerror != _ERROR_NOT_SAME_DEVICE:
                raise _win_error(exc, src) from exc
            # 別のドライブの隔離フォルダへは名前の変更で移せないため、開いたハンドルから写して元を消す
            win32file.SetFilePointer(handle, 0, win32file.FILE_BEGIN)
            with open(dest, "xb") as out:
                while True:
                    _, data = win32file.ReadFile(handle, _CHUNK)
                    if not data:
                        break
                    out.write(data)
            try:
                win32file.SetFileInformationByHandle(handle, win32file.FileDispositionInfo, True)
            except pywintypes.error as exc2:
                os.remove(dest)
                raise _win_error(exc2, src) from exc2
        return hasher.hexdigest(), size
    finally:
        handle.Close()


def _win_open_dir(path: str):
    """フォルダをリンクをたどらずに開き、確かめたうえでハンドルを返す。
    共有モードにFILE_SHARE_DELETEを含めないため、開いている間はそのフォルダと上位のフォルダの名前を変えられない"""
    import pywintypes
    import win32con
    import win32file

    try:
        handle = win32file.CreateFile(
            path,
            _FILE_READ_ATTRIBUTES,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
            None,
            win32con.OPEN_EXISTING,
            win32file.FILE_FLAG_BACKUP_SEMANTICS | win32file.FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
    except pywintypes.error as exc:
        raise _win_error(exc, path) from exc
    try:
        _win_check_handle(handle, path, want_dir=True)
    except Exception:
        handle.Close()
        raise
    return handle


def _win_open_dir_creating(path: str):
    """復元先のフォルダを開く。無いフォルダは、確かめた上位のフォルダを開いたまま1段ずつ作る"""
    missing: list[str] = []
    current = path
    while not os.path.lexists(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        missing.append(os.path.basename(current))
        current = parent
    handle = _win_open_dir(current)
    try:
        for name in reversed(missing):
            child = os.path.join(current, name)
            try:
                os.mkdir(child)
            except FileExistsError:
                pass  # 作る直前に作られた場合も、開いて確かめる
            child_handle = _win_open_dir(child)
            handle.Close()
            handle, current = child_handle, child
    except Exception:
        handle.Close()
        raise
    return handle


def _win_restore_move(src: str, target: str, overwrite: bool) -> None:
    import pywintypes
    import win32file

    parent_handle = _win_open_dir_creating(os.path.dirname(target))
    try:
        if os.path.lexists(target):
            info = os.lstat(target)
            if info.st_file_attributes & (_FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_DIRECTORY):
                raise UnsafePathError(f"復元先が通常のファイルではないため置き換えません: {target}")
            if not overwrite:
                raise FileExistsError(errno.EEXIST, "復元先に同じ名前のファイルがあります", target)
        flags = win32file.MOVEFILE_COPY_ALLOWED
        if overwrite:
            flags |= win32file.MOVEFILE_REPLACE_EXISTING
        try:
            win32file.MoveFileEx(src, target, flags)
        except pywintypes.error as exc:
            raise _win_error(exc, target) from exc
    finally:
        parent_handle.Close()


# -- Linux等（POSIX） --------------------------------------------------------
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_DIR_FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC


def _posix_open_dir(path: str, create: bool = False) -> int:
    """ルートから1段ずつリンクをたどらずにフォルダを開き、最後のフォルダのfdを返す。
    create=Trueなら無いフォルダを作る"""
    parts = [p for p in os.path.normpath(path).split("/") if p]
    fd = os.open("/", os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    try:
        for part in parts:
            if part == "..":
                raise UnsafePathError(f"パスに..が含まれるため操作しません: {path}")
            try:
                next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass  # 作る直前に作られた場合も、リンクをたどらずに開く
                next_fd = _posix_open_child_dir(part, fd, path)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise UnsafePathError(f"経路にシンボリックリンク等が含まれるため操作しません: {path}") from exc
                raise
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _posix_open_child_dir(name: str, dir_fd: int, path: str) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafePathError(f"経路にシンボリックリンク等が含まれるため操作しません: {path}") from exc
        raise


def _posix_copy_fd(src_fd: int, dst_fd: int) -> None:
    while True:
        data = os.read(src_fd, _CHUNK)
        if not data:
            return
        view = memoryview(data)
        while view:
            written = os.write(dst_fd, view)
            view = view[written:]


def _posix_quarantine_move(src: str, dest: str) -> tuple[str, int]:
    name = os.path.basename(src)
    dest_name = os.path.basename(dest)
    parent_fd = _posix_open_dir(os.path.dirname(src))
    try:
        try:
            fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW | os.O_NONBLOCK | _O_CLOEXEC, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise UnsafePathError(f"シンボリックリンクのため隔離しません: {src}") from exc
            raise
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise UnsafePathError(f"通常のファイルではないため隔離しません: {src}")
            hasher = hashlib.sha256()
            size = 0
            while True:
                data = os.read(fd, _CHUNK)
                if not data:
                    break
                hasher.update(data)
                size += len(data)
            identity = (info.st_dev, info.st_ino)

            quarantine_fd = os.open(os.path.dirname(dest), os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
            try:
                try:
                    os.rename(name, dest_name, src_dir_fd=parent_fd, dst_dir_fd=quarantine_fd)
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        raise
                    _posix_copy_across_devices(fd, identity, name, parent_fd, dest_name, quarantine_fd, src)
                else:
                    moved = os.stat(dest_name, dir_fd=quarantine_fd, follow_symlinks=False)
                    if (moved.st_dev, moved.st_ino) != identity:
                        # 開いてから名前を変えるまでの間に置き換えられた。移したものを元に戻して中止する
                        os.rename(dest_name, name, src_dir_fd=quarantine_fd, dst_dir_fd=parent_fd)
                        raise UnsafePathError(f"隔離の途中でファイルが置き換えられたため中止しました: {src}")
            finally:
                os.close(quarantine_fd)
            return hasher.hexdigest(), size
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _posix_copy_across_devices(fd, identity, name, parent_fd, dest_name, quarantine_fd, src) -> None:
    """別のファイルシステムの隔離フォルダへ、開いたfdから写し、同じファイルであれば元を消す"""
    os.lseek(fd, 0, os.SEEK_SET)
    out_fd = os.open(dest_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_CLOEXEC, 0o600, dir_fd=quarantine_fd)
    try:
        _posix_copy_fd(fd, out_fd)
    finally:
        os.close(out_fd)
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != identity:
        os.unlink(dest_name, dir_fd=quarantine_fd)
        raise UnsafePathError(f"隔離の途中でファイルが置き換えられたため中止しました: {src}")
    os.unlink(name, dir_fd=parent_fd)


def _posix_restore_move(src: str, target: str, overwrite: bool) -> None:
    name = os.path.basename(target)
    src_name = os.path.basename(src)
    parent_fd = _posix_open_dir(os.path.dirname(target), create=True)
    try:
        try:
            existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISREG(existing.st_mode):
                raise UnsafePathError(f"復元先が通常のファイルではないため置き換えません: {target}")
            if not overwrite:
                raise FileExistsError(errno.EEXIST, "復元先に同じ名前のファイルがあります", target)

        src_dir_fd = os.open(os.path.dirname(src), os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
        try:
            try:
                # renameは最後の要素がリンクでもたどらない（リンク自体を置き換える）
                os.rename(src_name, name, src_dir_fd=src_dir_fd, dst_dir_fd=parent_fd)
                return
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
            # 別のファイルシステムへは、写してから隔離フォルダのものを消す
            if existing is not None:
                os.unlink(name, dir_fd=parent_fd)
            in_fd = os.open(src_name, os.O_RDONLY | _O_CLOEXEC, dir_fd=src_dir_fd)
            try:
                out_fd = os.open(
                    name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC, 0o600, dir_fd=parent_fd
                )
                try:
                    _posix_copy_fd(in_fd, out_fd)
                finally:
                    os.close(out_fd)
            finally:
                os.close(in_fd)
            os.unlink(src_name, dir_fd=src_dir_fd)
        finally:
            os.close(src_dir_fd)
    finally:
        os.close(parent_fd)
