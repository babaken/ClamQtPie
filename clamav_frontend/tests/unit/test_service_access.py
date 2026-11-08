"""トレイGUI を使える利用者（core/service_access.py）・clamqtpie-ctl users・配布物の名前のテスト（段階 12-4d）"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import service_access
from core.service_access import AccessError

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows のアクセス権")
linux_only = pytest.mark.skipif(sys.platform == "win32", reason="Linux の所有者・権限")


class _Runner:
    def __init__(self, results=None):
        self.calls, self.results = [], list(results or [])

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        code, out = self.results.pop(0) if self.results else (0, "")
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="")


# -- トークンをグループに読ませる ----------------------------------------------------------------
@windows_only
def test_windows_token_is_readable_by_the_group_only(tmp_path, monkeypatch):
    import ntsecuritycon
    import win32security

    from core.private_files import write_private_file

    api = tmp_path / "api"
    api.mkdir()
    for name in ("user.token", "admin.token", "endpoint.json"):
        write_private_file(api / name, b"x")
    monkeypatch.setattr(service_access, "WINDOWS_GROUP", "Users")  # どの PC にもあるグループで代わりに確かめる
    assert service_access.share_with_users(tmp_path, api) is True
    users = win32security.LookupAccountName(None, "Users")[0]

    def readers(name):
        dacl = win32security.GetFileSecurity(str(api / name), win32security.DACL_SECURITY_INFORMATION) \
            .GetSecurityDescriptorDacl()
        return [dacl.GetAce(i) for i in range(dacl.GetAceCount()) if dacl.GetAce(i)[2] == users]

    [(_header, mask, _sid)] = readers("user.token")
    assert mask & ntsecuritycon.FILE_READ_DATA and not mask & ntsecuritycon.FILE_WRITE_DATA
    assert readers("endpoint.json") and readers("admin.token") == []  # admin.token は読ませない


@windows_only
def test_missing_group_is_not_an_error_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(service_access, "WINDOWS_GROUP", "ClamQtPie Users (テスト用・無いグループ)")
    assert service_access.share_with_users(tmp_path, tmp_path) is False


@linux_only
def test_linux_token_is_readable_by_the_group_only(tmp_path, monkeypatch):
    import grp

    api = tmp_path / "api"
    api.mkdir()
    for name in ("user.token", "admin.token", "endpoint.json"):
        (api / name).write_text("x")
    changed = {}
    monkeypatch.setattr(grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=990))
    monkeypatch.setattr(service_access.os, "chown", lambda path, uid, gid, **kw: changed.setdefault(Path(path).name, (uid, gid)))
    modes = {}
    monkeypatch.setattr(service_access.os, "chmod", lambda path, mode: modes.__setitem__(Path(path).name, mode))
    assert service_access.share_with_users(tmp_path, api) is True
    assert modes == {tmp_path.name: 0o710, "api": 0o750, "user.token": 0o640, "endpoint.json": 0o640}
    assert changed["user.token"] == (0, 990) and "admin.token" not in changed


@linux_only
def test_missing_group_is_not_an_error_on_linux(tmp_path, monkeypatch):
    import grp

    def missing(name):
        raise KeyError(name)

    monkeypatch.setattr(grp, "getgrnam", missing)
    assert service_access.share_with_users(tmp_path, tmp_path) is False


def test_share_failure_does_not_stop_the_service(monkeypatch, caplog):
    from api import service_api

    def fail(data_dir, api_dir):
        raise AccessError("フォルダがリンクになっています")

    monkeypatch.setattr(service_access, "share_with_users", fail)
    service_api._share_with_users(SimpleNamespace(data_dir=Path("."), api_dir=Path(".")))
    assert "グループに読ませられませんでした" in caplog.text


# -- clamqtpie-ctl users -----------------------------------------------------------------------
@pytest.mark.parametrize("name", ["alice", "bob.smith", "PC01\\taro", "user_1"])
def test_user_names_are_accepted(name):
    assert service_access._check_name(name) == name


@pytest.mark.parametrize("name", ["", "-rf", "a;b", "a b", "../x", "a" * 70, "x\ny"])
def test_unsafe_user_names_are_refused(name):
    with pytest.raises(AccessError):
        service_access._check_name(name)


def test_add_user_creates_the_group_first():
    runner = _Runner([(2, ""), (0, ""), (0, "")])  # グループが無い → 作る → メンバーを読む（空） → 入れる
    assert service_access.add_user("alice", runner) is True
    if sys.platform == "win32":
        assert runner.calls[1][-2:] == ["/add", "/comment:ClamQtPie のトレイGUI を使える利用者"]
        assert runner.calls[3][-3:] == ["ClamQtPie Users", "alice", "/add"]
    else:
        assert runner.calls[1][-2:] == ["--system", "clamqtpie"]
        assert runner.calls[3][-3:] == ["-aG", "clamqtpie", "alice"]


def test_add_user_who_is_already_a_member_does_nothing():
    # 入れ直し・更新のインストーラから呼ばれる（net localgroup は既に入っている利用者の追加をエラーにする）
    if sys.platform == "win32":
        members = ("エイリアス名     ClamQtPie Users\n\nメンバー\n\n" + "-" * 79 +
                   "\nPC01\\Alice\nコマンドは正常に終了しました。\n")
    else:
        members = "clamqtpie:x:990:alice\n"
    runner = _Runner([(0, ""), (0, members)])  # グループはある → メンバーを読む
    assert service_access.add_user("alice", runner) is False
    assert len(runner.calls) == 2  # 追加のコマンドは実行しない


def test_list_users_reads_the_group():
    if sys.platform == "win32":
        out = ("エイリアス名     ClamQtPie Users\nコメント\n\nメンバー\n\n"
               "-------------------------------------------------------------------------------\n"
               "alice\nbob\nコマンドは正常に終了しました。\n\n")
    else:
        out = "clamqtpie:x:990:alice,bob\n"
    assert service_access.list_users(_Runner([(0, out)])) == ["alice", "bob"]


def test_ctl_users_commands(tmp_path, monkeypatch):
    import clamqtpie_ctl
    from core.service_paths import ServicePaths

    out = io.StringIO()
    monkeypatch.setattr(clamqtpie_ctl, "_audit", lambda paths, message: out.write(f"[ログ] {message}\n"))
    runner = _Runner()
    assert clamqtpie_ctl.cmd_users(ServicePaths.under(tmp_path), "add", "alice", out, runner) == 0
    assert "サインアウトしてサインインし直す" in out.getvalue() and "[ログ] clamqtpie-ctl: トレイGUI を使える利用者に加えました: alice" in out.getvalue()
    with pytest.raises(clamqtpie_ctl.CtlError):
        clamqtpie_ctl.cmd_users(ServicePaths.under(tmp_path), "add", "a;b", out, runner)


# -- サービスの実行ファイルの置き場所 ------------------------------------------------------------
def test_program_location_must_not_be_writable_by_others(tmp_path, monkeypatch):
    import core.service_paths as service_paths

    exe = tmp_path / "ClamQtPieService.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(service_paths, "writable_by_others",
                        lambda path: "Users に書き込みの権限があります" if path == tmp_path else None)
    problem = service_access.program_location_problem(exe)
    assert problem == f"{tmp_path}: Users に書き込みの権限があります"
    monkeypatch.setattr(service_paths, "writable_by_others", lambda path: None)
    assert service_access.program_location_problem(exe) is None


# -- 配布物の名前 --------------------------------------------------------------------------------
def test_spec_names_match_the_code():
    """spec の実行ファイルの名前が、トレイGUI が起動する名前（昇格ヘルパー・通知のクリック）と合っていること"""
    from api_client.elevation import HELPER_EXE_NAME, HELPER_LINUX_NAME
    from core.activation import HELPER_EXE_NAME as ACTIVATE_EXE

    spec = (Path(__file__).resolve().parents[2] / "ClamQtPie.spec").read_text(encoding="utf-8")
    assert f'"{HELPER_EXE_NAME.removesuffix(".exe")}"' in spec and f'"{HELPER_LINUX_NAME}"' in spec
    assert f'"{ACTIVATE_EXE.removesuffix(".exe")}"' in spec
    policy = (Path(__file__).resolve().parents[2] / "packaging" / "linux" / "io.github.babaken.clamqtpie.policy") \
        .read_text(encoding="utf-8")
    assert f"/opt/clamqtpie/{HELPER_LINUX_NAME}</annotate>" in policy and "<allow_active>auth_admin<" in policy


def test_spec_shares_python_modules_between_executables():
    """4 つの実行ファイルが Python のモジュールを別々に持たない（_internal/ の .pyc を共有する）こと"""
    from pathlib import Path

    spec = (Path(__file__).resolve().parents[2] / "ClamQtPie.spec").read_text(encoding="utf-8")
    analysis = spec.split("def _analysis", 1)[1].split("def ", 1)[0]
    assert "noarchive=True" in analysis
    assert '"pygments"' in spec and '"setuptools"' in spec  # 実行時に使わないものを入れない


def test_spec_bundles_the_abi3t_compat_dll():
    """Windows の Python 3.15 で、cryptography（abi3t の拡張モジュール）が読み込む python3t.dll を配布物に入れること
    （PyInstaller は自動では集めない。無いと HTTPS の証明書を作れない）"""
    spec = (Path(__file__).resolve().parents[2] / "ClamQtPie.spec").read_text(encoding="utf-8")
    assert '"abi3t-compat" / "python3t.dll"' in spec and "binaries=_extra_binaries()" in spec


# -- 旧版の自動起動の登録し直し ------------------------------------------------------------------
def _linux_autostart(tmp_path, command):
    from core.autostart_service import AutostartService

    service = AutostartService(command)
    service._is_windows = False
    service._autostart_dir = lambda: tmp_path
    return service


def test_autostart_pointing_to_the_old_program_is_registered_again(tmp_path):
    old = _linux_autostart(tmp_path, "/home/u/ClamQtPie_v0.9.3/ClamQtPie")
    old.enable()
    new = _linux_autostart(tmp_path, "/opt/clamqtpie/clamqtpie")
    assert new.refresh_command() is True
    assert "Exec=/opt/clamqtpie/clamqtpie" in (tmp_path / "clamqtpie.desktop").read_text(encoding="utf-8")
    assert new.refresh_command() is False  # 2 回目は何もしない


def test_autostart_is_not_turned_on_by_the_take_over(tmp_path):
    new = _linux_autostart(tmp_path, "/opt/clamqtpie/clamqtpie")
    assert new.refresh_command() is False and not (tmp_path / "clamqtpie.desktop").exists()


def test_tray_takes_over_legacy_registrations(monkeypatch, caplog):
    import logging

    import tray_main

    class Autostart:
        exe_path = "/opt/clamqtpie/clamqtpie"

        def migrate_legacy(self):
            return False

        def refresh_command(self):
            return True

    monkeypatch.setattr("core.legacy_migration.remove_legacy_protocol", lambda: False)
    monkeypatch.setattr("core.legacy_migration.remove_legacy_state_dir", lambda: False)
    with caplog.at_level(logging.INFO, logger="clamav_frontend"):
        tray_main.take_over_legacy_registrations(Autostart())
    assert "自動起動を、このトレイGUI で登録し直しました" in caplog.text
    assert isinstance(tray_main._autostart_command(), str)  # AutostartService は文字列を受け取る


def test_ctl_users_add_tells_when_already_a_member(tmp_path, monkeypatch):
    import clamqtpie_ctl
    from core.service_paths import ServicePaths

    out = io.StringIO()
    monkeypatch.setattr(clamqtpie_ctl, "_audit", lambda paths, message: out.write(f"[ログ] {message}\n"))
    monkeypatch.setattr(service_access, "add_user", lambda name, runner: False)
    assert clamqtpie_ctl.cmd_users(ServicePaths.under(tmp_path), "add", "alice", out, _Runner()) == 0
    assert "すでに" in out.getvalue() and "[ログ]" not in out.getvalue()
# -- サインインし直しの案内（fix/install-signin） ------------------------------------------------
def _members(*names):
    if sys.platform == "win32":
        return 0, ("エイリアス名     ClamQtPie Users\n\nメンバー\n\n" + "-" * 79 + "\n"
                   + "".join(f"{n}\n" for n in names) + "コマンドは正常に終了しました。\n")
    return 0, f"clamqtpie:x:990:{','.join(names)}\n"


def test_member_whose_sign_in_predates_the_group_is_told_to_sign_in_again():
    """グループに入っているのに今のサインインに入っていなければ、サインインし直せば使える"""
    runner = _Runner([_members("PC01\\Alice")])
    assert service_access.signin_is_stale("alice", runner, token_has_group=lambda: False) is True


def test_non_member_is_not_told_to_sign_in_again():
    runner = _Runner([_members("bob")])
    assert service_access.signin_is_stale("alice", runner, token_has_group=lambda: False) is False


@pytest.mark.parametrize("has_group", [True, None])
def test_sign_in_is_not_stale_when_the_token_has_the_group_or_cannot_be_checked(has_group):
    runner = _Runner([_members("alice")])
    assert service_access.signin_is_stale("alice", runner, token_has_group=lambda: has_group) is False
    assert runner.calls == []  # メンバーの一覧は読まない


def test_token_group_check_does_not_fail():
    assert service_access._token_has_group() in (True, False, None)
