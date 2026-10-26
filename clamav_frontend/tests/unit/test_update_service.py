"""
UpdateServiceのユニットテスト（freshclamはモック化）
"""
import sys
import textwrap
from pathlib import Path

import pytest

from core.clamav_service import ClamAVService
from core.config_model import ClamdConfig
from core.update_service import UpdateService
from db.database import DatabaseService


@pytest.fixture
def update_service(tmp_path, fake_clamd_server):
    db_service = DatabaseService(tmp_path / "app.db")
    db_service.connect()

    clamd_config = ClamdConfig(
        connection_type="tcp", tcp_host=fake_clamd_server.host, tcp_port=fake_clamd_server.port
    )
    clamav_service = ClamAVService(clamd_config)

    # OS固有の分岐（Linuxのpkexec等）は下のLinux用テストで確認する。ここではfreshclamを直接実行する経路に固定
    service = UpdateService(db_service, clamav_service, system="Windows")
    yield service, db_service
    db_service.close()


def _make_fake_freshclam(tmp_path: Path, exit_code: int = 0, lines=None) -> str:
    lines = lines or ["ClamAV update process started", "main.cvd is up to date"]
    script = tmp_path / "fake_freshclam.py"
    # 注意: textwrap.dedent()にf-stringで複数行のbodyを埋め込むと、bodyの2行目以降が
    # インデント0のまま挿入され、dedentの共通接頭辞計算が壊れてIndentationErrorになる
    # 不具合があった（生成スクリプトの一部だけインデントが残ってしまう）。
    # 各行を明示的に結合してインデント崩れを避ける。
    body_lines = "\n".join(f"print({line!r})" for line in lines)
    script_text = "import sys\n" + body_lines + f"\nsys.exit({exit_code})\n"
    script.write_text(script_text)
    return f"{sys.executable} {script}"


def test_update_success_records_history_and_streams_output(update_service, tmp_path, fake_clamd_server):
    service, db_service = update_service
    fake_clamd_server.response_map["VERSION"] = "ClamAV 1.3.1/27200/Mon Jan  6 08:00:00 2026"
    fake_clamd_server.response_map["RELOAD"] = "RELOADING"

    fake_cmd = _make_fake_freshclam(tmp_path, exit_code=0)
    service.freshclam_path = fake_cmd.split(" ")[0]
    # subprocess.Popenはリスト引数のため、複数トークンのコマンドはPopen呼び出し側で分割する必要がある。
    # ここではpythonインタプリタ+スクリプトパスの2引数構成に対応させる。
    import subprocess as sp

    original_popen = sp.Popen

    def patched_popen(args, **kwargs):
        return original_popen(fake_cmd.split(" "), **kwargs)

    sp.Popen = patched_popen
    try:
        output_lines = []
        record = service.update_now(on_output=output_lines.append)
    finally:
        sp.Popen = original_popen

    assert record.result in ("success", "no_change")
    assert any("ClamAV update process started" in line for line in output_lines)

    history = service.list_history()
    assert len(history) == 1


def test_update_failure_exit_code_records_failure(update_service, tmp_path, fake_clamd_server):
    service, db_service = update_service
    fake_clamd_server.response_map["VERSION"] = "ClamAV 1.3.1/27200/Mon Jan  6 08:00:00 2026"

    fake_cmd = _make_fake_freshclam(tmp_path, exit_code=1, lines=["ERROR: connection failed"])

    import subprocess as sp

    original_popen = sp.Popen

    def patched_popen(args, **kwargs):
        return original_popen(fake_cmd.split(" "), **kwargs)

    sp.Popen = patched_popen
    try:
        record = service.update_now()
    finally:
        sp.Popen = original_popen

    assert record.result == "failure"
    # freshclamが出したエラー行を詳細に含める（原因が分かるように。Linux実機で発見）
    assert "freshclamの終了コード 1" in record.detail
    assert "ERROR: connection failed" in record.detail


def test_freshclam_not_found_returns_failure_instead_of_raising(update_service):
    # 以前は例外を送出し、定義更新画面のワーカーが完了を通知できず実行中表示のまま止まっていた
    service, db_service = update_service
    service.freshclam_path = "/nonexistent/freshclam_binary_xyz"

    record = service.update_now()

    assert record.result == "failure"
    assert "freshclamが見つかりません" in record.detail
    history = service.list_history()
    assert len(history) == 1
    assert history[0].result == "failure"


# -- Linuxの一般ユーザー（/var/lib/clamav等へ書き込めない。Linux実機で発見） ------------------
class _FakeProcess:
    def __init__(self, lines, returncode):
        self.stdout = iter(f"{line}\n" for line in lines)
        self._returncode = returncode

    def wait(self):
        return self._returncode


@pytest.fixture
def linux_service(update_service, monkeypatch):
    service, db_service = update_service
    monkeypatch.setattr("core.update_service.os.geteuid", lambda: 1000, raising=False)
    calls = {"popen": [], "service_active": False}

    def fake_popen(command, **kwargs):
        calls["popen"].append(command)
        return calls["process"]

    def fake_runner(command, **kwargs):
        import subprocess

        return subprocess.CompletedProcess(command, 0 if calls["service_active"] else 3)

    monkeypatch.setattr("core.update_service.subprocess.Popen", fake_popen)
    service._system = "Linux"
    service._which = lambda name: {"pkexec": "/usr/bin/pkexec", "freshclam": "/usr/bin/freshclam"}.get(name)
    service._runner = fake_runner
    return service, calls


def test_linux_interactive_update_uses_pkexec(linux_service):
    service, calls = linux_service
    calls["process"] = _FakeProcess(["Database updated"], 0)

    record = service.update_now(interactive=True)

    assert calls["popen"] == [["/usr/bin/pkexec", "/usr/bin/freshclam"]]
    assert record.result in ("success", "no_change")


def test_linux_interactive_update_avoids_log_lock_when_freshclam_service_runs(linux_service):
    """サービスのfreshclam -dがログファイルをロックしているため、手動更新ではログを別にする"""
    service, calls = linux_service
    calls["service_active"] = True
    calls["process"] = _FakeProcess(["Database updated"], 0)

    record = service.update_now(interactive=True)

    assert calls["popen"] == [["/usr/bin/pkexec", "/usr/bin/freshclam", "--log=/dev/null"]]
    assert record.result in ("success", "no_change")


def test_linux_root_update_avoids_log_lock_when_freshclam_service_runs(linux_service, monkeypatch):
    service, calls = linux_service
    monkeypatch.setattr("core.update_service.os.geteuid", lambda: 0, raising=False)
    calls["service_active"] = True
    calls["process"] = _FakeProcess(["Database updated"], 0)

    service.update_now(interactive=False)

    assert calls["popen"] == [["freshclam", "--log=/dev/null"]]


def test_linux_pkexec_auth_cancel(linux_service):
    service, calls = linux_service
    calls["process"] = _FakeProcess([], 126)

    record = service.update_now(interactive=True)

    assert record.result == "failure"
    assert "管理者認証" in record.detail


def test_linux_scheduled_update_is_skipped_when_freshclam_service_runs(linux_service):
    service, calls = linux_service
    calls["service_active"] = True

    record = service.update_now(interactive=False)

    assert record.result == "skipped"
    assert calls["popen"] == []  # 無人実行で認証を求めない
    assert service.list_history() == []  # スキップは履歴に記録しない


def test_linux_scheduled_update_without_service_fails_with_hint(linux_service):
    service, calls = linux_service

    record = service.update_now(interactive=False)

    assert record.result == "failure"
    assert calls["popen"] == []
    assert "systemctl enable --now clamav-freshclam" in record.detail


def test_linux_without_pkexec_runs_freshclam_and_explains_failure(linux_service):
    service, calls = linux_service
    service._which = lambda name: None
    calls["process"] = _FakeProcess(
        ["ERROR: Failed to open log file /var/log/clamav/freshclam.log: Permission denied"], 2
    )

    record = service.update_now(interactive=True)

    assert calls["popen"] == [["freshclam"]]
    assert "Permission denied" in record.detail
    assert "systemctl enable --now clamav-freshclam" in record.detail
