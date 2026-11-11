"""サービスの起動の回数の上限（設計書 9.4 の回復動作）の扱いのテスト

- 導入・更新（install.sh）は、管理者が意図して行う操作なので、開始の前に数え直す
- 上限で止まっているときは、clamqtpie-ctl status で区別して、原因の確かめ方と直し方を出す
- トレイGUI のつながらない表示から、clamqtpie-ctl status へ案内する
"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import clamqtpie_ctl
from core import service_status

ROOT = Path(__file__).resolve().parents[2]


def _fake_run(stdout: str):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout.encode("utf-8"), b"")

    return run, calls


def test_install_resets_the_start_count_before_starting():
    script = (ROOT / "packaging" / "linux" / "install.sh").read_text(encoding="utf-8")
    reset = script.index("systemctl reset-failed clamqtpie.service")
    assert reset < script.index("systemctl enable --now clamqtpie.service")


def test_stop_reason_reads_systemd(monkeypatch):
    monkeypatch.setattr(service_status, "find_system_tool", lambda name, system=None: "/usr/bin/systemctl")
    run, calls = _fake_run("ActiveState=failed\nResult=start-limit-hit\n")
    assert service_status.service_stop_reason("Linux", run=run) == service_status.START_LIMIT_HIT
    assert calls[0][:3] == ["/usr/bin/systemctl", "show", service_status.SYSTEMD_UNIT]
    run, _ = _fake_run("ActiveState=active\nResult=success\n")
    assert service_status.service_stop_reason("Linux", run=run) is None
    run, _ = _fake_run("ActiveState=failed\nResult=exit-code\n")
    assert service_status.service_stop_reason("Linux", run=run) == "exit-code"
    # Windows の回復動作は手での開始を断らないので、区別しない
    assert service_status.service_stop_reason("Windows", run=run) is None


def test_ctl_status_explains_the_start_limit(tmp_path, monkeypatch):
    from core.clamav_service import ClamAVService

    monkeypatch.setattr(clamqtpie_ctl, "service_running", lambda *a, **k: False)
    monkeypatch.setattr(ClamAVService, "ping", lambda self, retry=True: False)

    def status(reason):
        monkeypatch.setattr(clamqtpie_ctl, "service_stop_reason", lambda *a, **k: reason)
        out = io.StringIO()
        assert clamqtpie_ctl.main(["--data-dir", str(tmp_path / "svc"), "status"], out=out) == 0
        return out.getvalue()

    text = status(service_status.START_LIMIT_HIT)
    assert "起動の回数の上限で止めています" in text and "journalctl -u clamqtpie" in text
    assert "sudo systemctl reset-failed clamqtpie && sudo systemctl start clamqtpie" in text
    assert "起動の回数の上限" not in status("exit-code")
    assert "起動の回数の上限" not in status(None)


def test_tray_points_to_ctl_status_when_the_service_is_unreachable(qtbot):
    from gui.screens.home import HomeScreen

    screen = HomeScreen()
    qtbot.addWidget(screen)
    clamav = MagicMock()
    screen.bind_services(clamav, SimpleNamespace(is_running=False), MagicMock(), MagicMock(), MagicMock())
    screen.set_service_down("E8105")
    screen.refresh_status()
    text = screen.hero_detail_label.text()
    if sys.platform == "win32":
        assert "管理者のコマンドプロンプトで clamqtpie-ctl status" in text
    else:
        assert "sudo clamqtpie-ctl status" in text  # clamqtpie-ctl は root で動かす
