"""
外部コマンドの場所の決め方（core/exec_path.py）のテスト

相対名で起動すると、Windowsではexeのフォルダやカレントディレクトリからも探されるため、絶対パスで起動する。
"""
import ntpath
import os
import platform

import pytest

from core.exec_path import _first_token, find_program, system_tool, windows_system_dir


def test_windows_system_tools_are_absolute_paths_in_system_dir():
    sc = system_tool("sc", "Windows")
    powershell = system_tool("powershell", "Windows")
    assert ntpath.isabs(sc) and sc.lower().endswith("\\sc.exe")
    assert powershell.lower().endswith("\\windowspowershell\\v1.0\\powershell.exe")
    assert ntpath.dirname(sc) == windows_system_dir()


@pytest.mark.skipif(platform.system() != "Windows", reason="Windowsのみ")
def test_windows_system_dir_is_not_taken_from_environment(monkeypatch):
    real = windows_system_dir()
    monkeypatch.setenv("SystemRoot", r"C:\Users\Public\fake")
    monkeypatch.setenv("windir", r"C:\Users\Public\fake")
    assert windows_system_dir() == real
    assert os.path.isfile(system_tool("sc", "Windows"))


def test_posix_system_tool_is_absolute_even_if_missing():
    path = system_tool("no-such-command-xyz", "Linux")
    assert path.startswith("/")
    assert path.endswith("/no-such-command-xyz")


def test_find_program_ignores_current_directory_and_relative_path_entries(tmp_path, monkeypatch):
    system = platform.system()
    exe = "tool.exe" if system == "Windows" else "tool"
    planted = tmp_path / exe
    planted.write_bytes(b"")
    planted.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "relative/bin"]))

    assert find_program("tool", system=system) is None


def test_find_program_prefers_extra_dirs_over_path(tmp_path, monkeypatch):
    system = platform.system()
    exe = "tool.exe" if system == "Windows" else "tool"
    first, second = tmp_path / "clamav", tmp_path / "onpath"
    for d in (first, second):
        d.mkdir()
        (d / exe).write_bytes(b"")
        (d / exe).chmod(0o755)
    monkeypatch.setenv("PATH", str(second))

    assert find_program("tool", [str(first)], system=system) == os.path.join(str(first), exe)
    assert find_program("tool", system=system) == os.path.join(str(second), exe)


def test_find_program_returns_given_path_with_directory(tmp_path):
    target = tmp_path / "bin" / "freshclam"
    target.parent.mkdir()
    target.write_bytes(b"")
    assert find_program(str(target), system=platform.system()) == str(target)
    assert find_program(str(tmp_path / "missing" / "freshclam"), system=platform.system()) is None


@pytest.mark.parametrize(
    "image_path, expected",
    [
        (r'"C:\Program Files\ClamAV\clamd.exe" --service', r"C:\Program Files\ClamAV\clamd.exe"),
        (r"C:\ClamAV\clamd.exe --config-file=C:\ClamAV\clamd.conf", r"C:\ClamAV\clamd.exe"),
    ],
)
def test_first_token_extracts_executable(image_path, expected):
    assert _first_token(image_path) == expected


# -- 実行ファイル・設定ファイルの場所の検証 --------------------------------------------
from core.exceptions import UnsafeExecutablePathError  # noqa: E402
from core.exec_path import validate_executable, validate_trusted_file  # noqa: E402

_IS_WINDOWS = platform.system() == "Windows"


def _user_writable_file(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"")
    path.chmod(0o755)
    return str(path)


def test_executable_in_user_writable_folder_is_rejected(tmp_path):
    path = _user_writable_file(tmp_path, "freshclam.exe" if _IS_WINDOWS else "freshclam")
    with pytest.raises(UnsafeExecutablePathError) as excinfo:
        validate_executable(path, ("freshclam",))
    assert excinfo.value.code == "E6005"


def test_executable_with_unexpected_name_is_rejected():
    trusted = system_tool("sc") if _IS_WINDOWS else "/bin/sh"
    with pytest.raises(UnsafeExecutablePathError):
        validate_executable(trusted, ("freshclam",))


def test_relative_or_missing_executable_is_rejected(tmp_path):
    with pytest.raises(UnsafeExecutablePathError):
        validate_executable("freshclam", ("freshclam",))
    with pytest.raises(UnsafeExecutablePathError):
        validate_executable(str(tmp_path / "missing" / "freshclam"), ("freshclam",))


def test_executable_in_system_location_is_accepted():
    if _IS_WINDOWS:
        assert validate_executable(system_tool("sc"), ("sc",)).lower().endswith("sc.exe")
    else:
        assert validate_executable("/bin/sh", ("sh",))


def test_trusted_file_in_user_writable_folder_is_rejected(tmp_path):
    path = tmp_path / "clamd.conf"
    path.write_text("LocalSocket /run/clamav/clamd.ctl\n", encoding="utf-8")
    with pytest.raises(UnsafeExecutablePathError):
        validate_trusted_file(str(path))


def test_find_system_tool_does_not_fall_back_to_path(tmp_path, monkeypatch):
    # 決まった場所に無いコマンドは、PATHにあっても見つからない扱いにする（昇格に使うpkexec等の確認に使う）
    from core.exec_path import find_system_tool

    monkeypatch.setenv("PATH", str(tmp_path))
    (tmp_path / "clamqtpie-no-such-tool").write_text("")
    assert find_system_tool("clamqtpie-no-such-tool", "Linux") is None


# -- 外部コマンドに渡す環境変数 ------------------------------------------------------------
def test_windows_child_env_passes_only_fixed_variables(monkeypatch):
    # 管理者権限で起動する外部コマンドには、利用者が変えられる環境変数を引き継がず、決まった変数だけを渡す
    from core.exec_path import child_env

    monkeypatch.setenv("CLAMQTPIE_TEST_USER_VARIABLE", "1")
    monkeypatch.setenv("SystemRoot", r"C:\Users\someone\fake")
    env = child_env({"EXTRA": "x"}, system="Windows")

    assert "CLAMQTPIE_TEST_USER_VARIABLE" not in env
    assert env["EXTRA"] == "x"
    assert env["SystemRoot"] != r"C:\Users\someone\fake"  # 環境変数ではなくAPIから得る
    assert set(env) <= {
        "SystemRoot", "windir", "SystemDrive", "ComSpec", "PATH", "PATHEXT", "TEMP", "TMP", "OS",
        "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "EXTRA",
    }


def test_posix_child_env_inherits_environment():
    from core.exec_path import child_env

    assert child_env(system="Linux") is None


@pytest.mark.skipif(_IS_WINDOWS, reason="Linux等の所有者・アクセス権の確認")
def test_posix_executable_under_user_writable_ancestor_is_rejected(tmp_path, monkeypatch):
    # 実行ファイルと親のフォルダがroot所有でも、祖先のフォルダにroot以外が書き込めるなら実行しない
    # （祖先のフォルダに書き込めると、その下のフォルダごと差し替えられるため）
    import stat as stat_module

    install = tmp_path / "opt" / "clamav" / "sbin"
    install.mkdir(parents=True)
    program = install / "clamonacc"
    program.write_text("")
    real_program = os.path.realpath(program)
    root_owned = {real_program, os.path.dirname(real_program)}
    real_stat = os.stat

    def fake_stat(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        values = list(info)
        if str(path) in root_owned:
            values[stat_module.ST_UID] = 0
            values[stat_module.ST_MODE] = (info.st_mode & ~0o022)
        return os.stat_result(values)

    monkeypatch.setattr("core.exec_path.os.stat", fake_stat)
    with pytest.raises(UnsafeExecutablePathError) as excinfo:
        validate_executable(str(program), ("clamonacc",))
    assert str(tmp_path) in excinfo.value.detail  # 祖先のフォルダの問題として知らせる
