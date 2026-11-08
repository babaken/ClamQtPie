"""Windows のインストーラから呼ぶ導入・削除（core/windows_setup.py・clamqtpie-ctl setup）のテスト（段階 12-4d）。
サービスの登録・グループは実際には変えない（コマンドを差し替える）"""
from __future__ import annotations

import io
import ntpath
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from core import windows_setup


class _Runner:
    """コマンドの名前と引数から結果を決める"""

    def __init__(self, installed=False, running=False):
        self.calls, self.installed, self.running = [], installed, running

    def __call__(self, cmd, **kwargs):
        name = ntpath.basename(cmd[0])  # Linux で動かしても Windows のパスとして読む
        self.calls.append([name, *cmd[1:]])
        args = cmd[1:]
        out, code = "", 0
        if name.lower().startswith("sc"):
            if args[0] == "query":
                code = 0 if self.installed else 1060
                out = "STATE : 4  RUNNING" if self.running else "STATE : 1  STOPPED"
            elif args[0] == "stop":
                self.running = False
            elif args[0] == "start":
                self.running = True
            elif args[0] == "delete":
                self.installed = False
        elif "install" in args or "update" in args:
            self.installed = True
        elif args[:1] == ["localgroup"] and len(args) == 2:
            code = 2  # グループがまだ無い
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="")


def test_untrusted_data_dir_is_set_aside(tmp_path):
    data = tmp_path / "ClamQtPie"
    data.mkdir()
    (data / "config.json").write_text("{}")
    moved = windows_setup.set_aside_untrusted(data, datetime(2026, 11, 3, 10, 0, 0),
                                              check=lambda path: "S-1-5-11 に書き込みの権限があります")
    assert moved == tmp_path / "ClamQtPie.untrusted-20261103-100000"
    assert not data.exists() and (moved / "config.json").exists()


def test_trusted_or_missing_data_dir_is_left_alone(tmp_path):
    data = tmp_path / "ClamQtPie"
    assert windows_setup.set_aside_untrusted(data, check=lambda path: "x") is None
    data.mkdir()
    assert windows_setup.set_aside_untrusted(data, check=lambda path: None) is None and data.exists()


def test_setup_install_registers_and_starts_the_service(tmp_path, monkeypatch):
    monkeypatch.setattr("core.service_access._IS_WINDOWS", True)
    runner = _Runner()
    out = io.StringIO()
    # 新しくグループに入れたら True（インストーラがサインインし直しを案内する）
    assert windows_setup.setup_install(tmp_path / "data", tmp_path, "alice", out, runner, check=lambda p: None) is True
    names = [" ".join(c[1:]) for c in runner.calls]
    assert any("localgroup ClamQtPie Users /add" in n for n in names)
    assert any(n.endswith("alice /add") for n in names)
    assert any(n.endswith("--startup delayed install") for n in names)  # 遅延自動起動
    assert "failure ClamQtPie reset= 86400 actions= restart/60000/restart/60000" in names
    assert "failureflag ClamQtPie 1" in names and "start ClamQtPie" in names
    assert "サービスを登録して開始しました" in out.getvalue()


def test_setup_install_updates_an_existing_service(tmp_path, monkeypatch):
    monkeypatch.setattr("core.service_access._IS_WINDOWS", True)
    runner = _Runner(installed=True, running=True)
    assert windows_setup.setup_install(tmp_path / "data", tmp_path, "", io.StringIO(), runner,
                                       check=lambda p: None) is False
    names = [" ".join(c[1:]) for c in runner.calls]
    assert names.index("stop ClamQtPie") < next(i for i, n in enumerate(names) if n.endswith("delayed update"))


def test_setup_uninstall_keeps_data_unless_purged(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    runner = _Runner(installed=True, running=True)
    windows_setup.setup_uninstall(data, purge=False, out=io.StringIO(), runner=runner)
    assert not runner.installed and data.exists()
    windows_setup.setup_uninstall(data, purge=True, out=io.StringIO(), runner=_Runner())
    assert not data.exists()


def test_service_that_does_not_stop_is_an_error():
    class Stuck(_Runner):
        def __call__(self, cmd, **kwargs):
            result = super().__call__(cmd, **kwargs)
            self.running = True
            return result

    with pytest.raises(windows_setup.SetupError):
        windows_setup.stop_service(Stuck(installed=True, running=True), sleep=lambda s: None, wait_sec=3)


def test_iss_calls_the_setup_commands():
    iss = (Path(__file__).resolve().parents[2] / "packaging" / "windows" / "ClamQtPie.iss").read_text(encoding="utf-8")
    assert "setup install --user" in iss and "setup uninstall" in iss and "--purge" in iss
    assert "PrivilegesRequired=admin" in iss and "runasoriginaluser" in iss  # 起動し直しは元の利用者の権限で
    assert r'"""{app}\ClamQtPie.exe"""' in iss  # 自動起動のパスを引用符で囲む


def test_service_process_is_waited_for_and_killed_if_it_lingers():
    # STOPPED になった後もプロセスが残っていると、削除・差し替えでファイルが使用中になる
    class Lingering:
        def __init__(self):
            self.calls = []

        def __call__(self, cmd, **kwargs):
            self.calls.append([ntpath.basename(cmd[0]).lower(), *cmd[1:]])
            out = "ClamQtPieService.exe   1234 Services   0   50,000 K" if "tasklist" in self.calls[-1][0] else ""
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    runner = Lingering()
    windows_setup.wait_process_exit(runner, sleep=lambda s: None, wait_sec=1)
    assert runner.calls[-1] == ["taskkill.exe", "/F", "/IM", "ClamQtPieService.exe"]
@pytest.mark.parametrize("added, expected", [(True, 10), (False, 0)])
def test_ctl_setup_install_reports_that_sign_in_is_required(tmp_path, monkeypatch, added, expected):
    """利用者をグループに新しく入れたときだけ、インストーラに「サインインし直しが要る」（終了コード 10）を返す"""
    import clamqtpie_ctl

    monkeypatch.setattr(clamqtpie_ctl.sys, "platform", "win32")
    monkeypatch.setattr(clamqtpie_ctl, "_audit", lambda paths, message: None)
    monkeypatch.setattr(windows_setup, "setup_install", lambda *a, **k: added)
    paths = clamqtpie_ctl.ServicePaths.under(tmp_path / "svc")
    code = clamqtpie_ctl.cmd_setup(paths, "install", "alice", False, io.StringIO())
    assert code == expected == (clamqtpie_ctl.EXIT_SIGNIN_REQUIRED if added else clamqtpie_ctl.EXIT_OK)


def test_iss_asks_to_sign_in_again_and_does_not_start_the_tray():
    """グループに新しく入れたときは、完了の画面で再起動の確認を出し、トレイGUI を起動しない"""
    import clamqtpie_ctl

    iss = (Path(__file__).resolve().parents[2] / "packaging" / "windows" / "ClamQtPie.iss").read_text(encoding="utf-8")
    assert f"ExitSignInRequired = {clamqtpie_ctl.EXIT_SIGNIN_REQUIRED};" in iss
    assert "function NeedRestart(): Boolean;" in iss and "FinishedRestartLabel=" in iss
    run_lines = [line for line in iss.splitlines() if "runasoriginaluser" in line]
    assert run_lines and all("not SignInRequired" in line for line in run_lines)
