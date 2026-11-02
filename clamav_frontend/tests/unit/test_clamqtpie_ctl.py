"""管理用コマンド clamqtpie-ctl と、サービスの秘密の保存先のテスト（段階 12-3b）"""
from __future__ import annotations

import io
import json
import sys

import pytest

import clamqtpie_ctl
from core.config_service import ConfigService
from core.secret_store import FileSecretStore


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "svc"
    data.mkdir()
    monkeypatch.setattr(clamqtpie_ctl, "service_running", lambda *a, **k: False)
    controls = []
    monkeypatch.setattr(clamqtpie_ctl, "_control_service", controls.append)

    def run(*args, answer="y"):
        out = io.StringIO()
        code = clamqtpie_ctl.main(["--data-dir", str(data), *args], out=out, ask=lambda _prompt: answer)
        return code, out.getvalue()

    return data, run, controls


def _saved(data):
    return json.loads((data / "config.json").read_text(encoding="utf-8"))


def test_needs_admin_without_data_dir(monkeypatch):
    monkeypatch.setattr(clamqtpie_ctl, "_is_admin", lambda: False)
    assert clamqtpie_ctl.main(["config", "show"], out=io.StringIO()) == clamqtpie_ctl.EXIT_NOT_ADMIN


def test_config_show(env):
    data, run, _ = env
    code, out = run("config", "show")
    assert code == 0 and json.loads(out)["api"]["port"] == 8765


def test_config_set_protected_item_with_confirmation(env):
    data, run, _ = env
    code, out = run("config", "set", "api.port", "18765")
    assert code == 0 and "8765 → 18765" in out
    assert _saved(data)["api"]["port"] == 18765
    # 確認で断れば変えない
    code, out = run("config", "set", "clamd.tcp_port", "3311", answer="n")
    assert code == 0 and _saved(data)["clamd"]["tcp_port"] == 3310


@pytest.mark.parametrize("key,value", [
    ("api.port", "80"),              # 特権ポート（範囲外）
    ("api.port", "abc"),             # 型が違う
    ("clamd.no_such", "1"),          # 無い項目
    ("schema_version", "3"),
])
def test_invalid_values_are_rejected(env, key, value):
    data, run, _ = env
    run("config", "show")  # 設定ファイルを作る
    code, _ = run("config", "set", key, value, "--yes")
    assert code == clamqtpie_ctl.EXIT_ERROR
    assert _saved(data)["api"]["port"] == 8765


def test_executable_location_is_checked(env, tmp_path):
    # 実行ファイルの場所は、名前と、一般の利用者が書き換えられない場所かを確かめる
    data, run, _ = env
    fake = tmp_path / ("freshclam.exe" if sys.platform == "win32" else "freshclam")
    fake.write_text("x")
    code, _ = run("config", "set", "update.freshclam_path", str(fake), "--yes")
    assert code == clamqtpie_ctl.EXIT_ERROR  # 一時フォルダは一般の利用者が書き換えられる
    code, _ = run("config", "set", "update.freshclam_path", "relative/freshclam", "--yes")
    assert code == clamqtpie_ctl.EXIT_ERROR


def test_running_service_is_restarted_only_when_asked(env, monkeypatch):
    # サービスが保存するときに上書きされないよう、動いている間は変えない（--restart で止めて変え、また開始する）
    data, run, controls = env
    monkeypatch.setattr(clamqtpie_ctl, "service_running", lambda *a, **k: True)
    code, _ = run("config", "set", "api.port", "18765", "--yes")
    assert code == clamqtpie_ctl.EXIT_ERROR and controls == []
    code, _ = run("config", "set", "api.port", "18765", "--yes", "--restart")
    assert code == 0 and controls == ["stop", "start"]
    assert _saved(data)["api"]["port"] == 18765


def test_change_is_recorded_in_the_service_log(env):
    data, run, _ = env
    run("config", "set", "api.port", "18765", "--yes")
    log = (data / "logs" / "app.log").read_text(encoding="utf-8")
    assert "clamqtpie-ctl: 設定を変更しました: api.port（8765 → 18765）" in log


def test_quarantine_restore_to_another_place(env, tmp_path):
    from core.quarantine_service import QuarantineService
    from db.database import DatabaseService

    data, run, _ = env
    db = DatabaseService(data / "app.db")
    db.connect()
    original = tmp_path / "x.exe"
    original.write_bytes(b"content")
    row = QuarantineService(db, data / "quarantine").quarantine(original, "Eicar")
    db.close()
    dest = tmp_path / "restored" / "x.exe"
    code, out = run("quarantine", "restore", str(row.id), "--to", str(dest))
    assert code == 0 and dest.read_bytes() == b"content"
    assert not original.exists()
    code, _ = run("quarantine", "restore", str(row.id), "--to", str(dest))
    assert code == clamqtpie_ctl.EXIT_ERROR  # 復元済み
    code, _ = run("quarantine", "restore", "1", "--to", "relative/x.exe")
    assert code == clamqtpie_ctl.EXIT_ERROR


# -- 秘密の保存先 ----------------------------------------------------------------------------------
def test_service_keeps_the_smtp_password_in_a_private_file(tmp_path):
    # サービス（SYSTEM・root）では keyring を使えないため、データの置き場所のファイルに保存する
    store = FileSecretStore(tmp_path / "secrets")
    service = ConfigService(tmp_path / "config.json", secret_store=store)
    service.load()
    service.set_smtp_password("pässwörd")
    assert service.get_smtp_password() == "pässwörd"
    raw = (tmp_path / "secrets" / "smtp_password").read_bytes()
    if sys.platform == "win32":
        assert "pässwörd".encode() not in raw  # DPAPI で暗号化する
    else:
        import os
        import stat

        assert stat.S_IMODE(os.stat(tmp_path / "secrets" / "smtp_password").st_mode) == 0o600
        assert stat.S_IMODE(os.stat(tmp_path / "secrets").st_mode) == 0o700
    assert "pässwörd" not in (tmp_path / "config.json").read_text(encoding="utf-8")  # 設定ファイルには入れない
    service.delete_smtp_password()
    assert service.get_smtp_password() is None


def test_secret_names_are_restricted(tmp_path):
    with pytest.raises(ValueError):
        FileSecretStore(tmp_path).get("../outside")


@pytest.mark.parametrize("key,default", [("update.freshclam_path", "freshclam"), ("realtime.clamonacc_path", "clamonacc")])
def test_executable_location_can_be_reset_to_the_default(env, key, default, monkeypatch):
    # 空、または既定の名前で、PATH から探す既定に戻せる
    data, run, _ = env
    section, name = key.split(".")
    run("config", "show")
    saved = _saved(data)
    saved[section][name] = "/opt/clamav/bin/" + default
    (data / "config.json").write_text(json.dumps(saved), encoding="utf-8")
    code, _ = run("config", "set", key, "", "--yes")
    assert code == 0 and _saved(data)[section][name] == default
    saved[section][name] = "/opt/clamav/bin/" + default
    (data / "config.json").write_text(json.dumps(saved), encoding="utf-8")
    code, _ = run("config", "set", key, default, "--yes")
    assert code == 0 and _saved(data)[section][name] == default


def test_closed_input_means_no(env):
    data, run, _ = env
    run("config", "show")

    def closed(_prompt):
        raise EOFError

    out = io.StringIO()
    code = clamqtpie_ctl.main(["--data-dir", str(data), "config", "set", "api.port", "18765"], out=out, ask=closed)
    assert code == 0 and "変更しませんでした" in out.getvalue()
    assert _saved(data)["api"]["port"] == 8765


def test_change_is_not_echoed_to_the_terminal(env, capsys):
    data, run, _ = env
    run("config", "set", "api.port", "18765", "--yes")
    captured = capsys.readouterr()
    assert "clamqtpie-ctl: 設定を変更しました" not in captured.out + captured.err


def test_systemd_start_resets_the_start_limit(monkeypatch):
    # 手で行う開始も起動の回数の上限に数えられるため、数え直してから開始する。失敗したら、止まっていることを伝える
    calls = []

    class Result:
        returncode = 1

    monkeypatch.setattr(clamqtpie_ctl.platform, "system", lambda: "Linux")
    monkeypatch.setattr(clamqtpie_ctl, "find_system_tool", lambda name, system: f"/usr/bin/{name}")
    monkeypatch.setattr(clamqtpie_ctl.subprocess, "run", lambda command, **kw: calls.append(command) or Result())
    with pytest.raises(clamqtpie_ctl.CtlError, match="開始できませんでした.*止まっています.*journalctl"):
        clamqtpie_ctl._control_service("start")
    assert calls == [["/usr/bin/systemctl", "reset-failed", "clamqtpie.service"],
                     ["/usr/bin/systemctl", "start", "clamqtpie.service"]]
