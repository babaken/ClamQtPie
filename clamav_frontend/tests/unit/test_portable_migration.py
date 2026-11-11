"""旧版（ポータブル）のデータの移行 clamqtpie-ctl migrate と、設定の分割のテスト（段階 12-3b）"""
from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
import sys
from contextlib import closing, contextmanager

import pytest

import clamqtpie_ctl
from core import portable_migration
from core.config_service import ConfigService
from core.exclude_service import ExcludeService
from core.quarantine_service import QuarantineService
from core.watch_target_service import WatchTargetService
from db.database import DatabaseService


@contextmanager
def _db(path):
    with closing(sqlite3.connect(path)) as conn:
        with conn:
            yield conn


@pytest.fixture
def legacy(tmp_path):
    """旧版のフォルダ（設定・DB・隔離ファイル・ログ）"""
    folder = tmp_path / "old"
    folder.mkdir()
    config = {
        "schema_version": 1,
        "general": {"autostart": False, "start_minimized": "yes"},
        "logging": {"level": "DEBUG", "retention_days": 30},
        "update": {"interval_hours": 99, "freshclam_path": "/tmp/freshclam"},
        "clamd": {"tcp_port": 3311, "auto_start": False},
        "smtp": {"host": "smtp.example.com", "user": ""},
    }
    (folder / "config.json").write_text(json.dumps(config), encoding="utf-8")
    db = DatabaseService(folder / "app.db")
    db.connect()
    victim = tmp_path / "victim.exe"
    victim.write_bytes(b"infected")
    row = QuarantineService(db, folder / "quarantine").quarantine(victim, "Eicar")
    ExcludeService(db).add("extension", ".iso")
    WatchTargetService(db).add(str(tmp_path / "Downloads"))
    with _db(folder / "app.db") as conn:
        conn.execute("INSERT INTO schedule (name, enabled, cron, target, scan_type) VALUES ('毎日', 1, '0 3 * * *', '', 'update')")
        conn.execute("UPDATE quarantine SET owner_info = 'O:WDG:WDD:(A;;FA;;;WD)'")
    db.close()
    (folder / "logs").mkdir()
    (folder / "logs" / "app.log.2026-10-01").write_text("old line\n", encoding="utf-8")
    (folder / "logs" / "other.txt").write_text("x", encoding="utf-8")
    return folder, row


@pytest.fixture
def ctl(tmp_path, monkeypatch):
    data = tmp_path / "svc"
    monkeypatch.setattr(clamqtpie_ctl, "service_running", lambda *a, **k: False)
    controls = []
    monkeypatch.setattr(clamqtpie_ctl, "_control_service", controls.append)
    # 実行した利用者の gui.json の場所
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(portable_migration, "legacy_smtp_password", lambda _config: None)

    def run(*args, answer="y"):
        out = io.StringIO()
        code = clamqtpie_ctl.main(["--data-dir", str(data), *args], out=out, ask=lambda _p: answer,
                                  ask_password=lambda _p: "")
        return code, out.getvalue()

    return data, run, controls


def _count(data, table):
    with _db(data / "app.db") as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_migrates_settings_db_quarantine_and_logs(legacy, ctl, tmp_path):
    folder, row = legacy
    data, run, _ = ctl
    code, out = run("migrate", "--from", str(folder))
    assert code == 0, out
    # 確認のための一覧
    assert "[extension] .iso" in out and "毎日: update 0 3 * * *" in out and "victim.exe（Eicar）" in out

    saved = json.loads((data / "config.json").read_text(encoding="utf-8"))
    assert saved["schema_version"] == 2 and "general" not in saved
    assert saved["logging"]["level"] == "DEBUG" and saved["logging"]["retention_days"] == 30
    assert saved["update"]["interval_hours"] == 4  # 範囲外は移さない
    # 保護された項目は移さず、一覧で知らせる
    assert saved["clamd"]["tcp_port"] == 3310 and saved["update"]["freshclam_path"] == "freshclam"
    assert "clamd.tcp_port（旧版: 3311）" in out and "update.freshclam_path" in out
    assert saved["clamd"]["auto_start"] is False  # Web API で変えられる項目は移す

    for table in ("exclude", "watch_target", "schedule", "quarantine"):
        assert _count(data, table) == 1
    assert (data / "quarantine" / row.uuid).exists()
    assert not (folder / "quarantine" / row.uuid).exists(), out  # 旧版の隔離ファイルは移した後に消す
    with _db(data / "app.db") as conn:
        assert conn.execute("SELECT owner_info FROM quarantine").fetchone()[0] is None  # 元のアクセス権は移さない
    assert (data / "logs" / "app.log.2026-10-01").read_text(encoding="utf-8") == "old line\n"
    assert not (data / "logs" / "other.txt").exists()
    assert (folder / portable_migration.MARKER_NAME).exists()

    gui = json.loads((tmp_path / ("appdata/ClamQtPie" if sys.platform == "win32" else "xdg/clamqtpie")
                      / "gui.json").read_text(encoding="utf-8"))
    assert gui["general"]["autostart"] is False and gui["general"]["start_minimized"] is True

    # 移した隔離ファイルは、別の場所へ復元できる
    dest = tmp_path / "restored.exe"
    code, out = run("quarantine", "restore", "1", "--to", str(dest))
    assert code == 0 and dest.read_bytes() == b"infected"
    assert "所有者・アクセス権を変えてください" in out  # 元の所有者の記録は移さないため
    # 同じフォルダからもう一度は移さない
    code, _ = run("migrate", "--from", str(folder), "--yes")
    assert code == clamqtpie_ctl.EXIT_ERROR


def test_nothing_changes_when_not_confirmed(legacy, ctl):
    folder, row = legacy
    data, run, _ = ctl
    code, out = run("migrate", "--from", str(folder), answer="n")
    assert code == 0 and "移行しませんでした" in out
    assert _count(data, "quarantine") == 0
    assert (folder / "quarantine" / row.uuid).exists()
    assert not (folder / portable_migration.MARKER_NAME).exists()


def test_untrusted_rows_are_not_migrated(legacy, ctl):
    # ファイルの名前として使う uuid、種別、時刻の指定を確かめ、正しくない記録は移さない
    folder, _ = legacy
    data, run, _ = ctl
    with _db(folder / "app.db") as conn:
        conn.execute("INSERT INTO quarantine (uuid, original_path, encoding) VALUES ('../../config', 'C:/x', 'none')")
        conn.execute("INSERT INTO quarantine (uuid, original_path, encoding) VALUES "
                     "('00000000-0000-0000-0000-000000000000', 'C:/y', 'rot13')")
        conn.execute("INSERT INTO schedule (name, enabled, cron, target, scan_type) VALUES ('x', 1, '0 3 * * *', '', 'shell')")
        conn.execute("INSERT INTO schedule (name, enabled, cron, target, scan_type) VALUES ('y', 1, 'every day', '', 'scan')")
        conn.execute("INSERT INTO exclude (type, value, enabled) VALUES ('regex', '.*', 1)")
        conn.execute("CREATE TABLE extra (x)")
        conn.execute("CREATE TRIGGER t AFTER INSERT ON exclude BEGIN DELETE FROM watch_target; END")
    code, out = run("migrate", "--from", str(folder), "--yes")
    assert code == 0, out
    assert _count(data, "quarantine") == 1 and _count(data, "schedule") == 1 and _count(data, "exclude") == 1
    assert _count(data, "watch_target") == 1  # 旧版の DB のトリガーは持ち込まない
    with _db(data / "app.db") as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name IN ('extra', 't')").fetchone() is None
    assert "ファイルの名前が正しくありません" in out and "保存の形式が分かりません" in out


@pytest.mark.skipif(sys.platform == "win32", reason="シンボリックリンクの作成に権限が要る")
def test_linked_quarantine_files_are_not_copied(legacy, ctl, tmp_path):
    folder, row = legacy
    data, run, _ = ctl
    secret = tmp_path / "secret"
    secret.write_bytes(b"secret")
    (folder / "quarantine" / row.uuid).unlink()
    os.symlink(secret, folder / "quarantine" / row.uuid)
    code, out = run("migrate", "--from", str(folder), "--yes")
    assert code == 0 and "通常のファイルではありません" in out
    assert _count(data, "quarantine") == 0 and not (data / "quarantine" / row.uuid).exists()


def test_quarantine_copy_failure_rolls_back(legacy, ctl, monkeypatch, tmp_path):
    folder, row = legacy
    data, run, _ = ctl
    victim2 = tmp_path / "victim2.exe"
    victim2.write_bytes(b"infected2")
    db = DatabaseService(folder / "app.db")
    db.connect()
    second = QuarantineService(db, folder / "quarantine").quarantine(victim2, "Eicar")
    db.close()
    original = portable_migration._copy_regular

    def failing(src, dest):
        if src.name == second.uuid:
            raise OSError("disk full")
        original(src, dest)

    monkeypatch.setattr(portable_migration, "_copy_regular", failing)
    code, _ = run("migrate", "--from", str(folder), "--yes")
    assert code == clamqtpie_ctl.EXIT_ERROR
    assert not (data / "quarantine" / row.uuid).exists()  # 写したものを消す
    assert (folder / "quarantine" / row.uuid).exists() and (folder / "quarantine" / second.uuid).exists()
    assert _count(data, "quarantine") == 0
    assert not (folder / portable_migration.MARKER_NAME).exists()


def test_refuses_when_target_already_has_data(legacy, ctl):
    folder, _ = legacy
    data, run, _ = ctl
    db = DatabaseService(data / "app.db")
    db.connect()
    ExcludeService(db).add("extension", ".tmp")
    db.close()
    code, _ = run("migrate", "--from", str(folder), "--yes")
    assert code == clamqtpie_ctl.EXIT_ERROR
    assert any((folder / "quarantine").iterdir()) and not (folder / portable_migration.MARKER_NAME).exists()


def test_refuses_while_legacy_app_is_running(legacy, ctl):
    folder, _ = legacy
    data, run, _ = ctl
    (folder / "app.db-wal").write_bytes(b"pending")
    code, _ = run("migrate", "--from", str(folder), "--yes")
    assert code == clamqtpie_ctl.EXIT_ERROR


@pytest.mark.parametrize("make_source", [
    lambda folder, data: "relative/old",
    lambda folder, data: str(folder.parent / "missing"),
    lambda folder, data: str(data),  # 移行先そのもの
])
def test_rejects_wrong_source(legacy, ctl, make_source):
    folder, _ = legacy
    data, run, _ = ctl
    data.mkdir(exist_ok=True)
    (data / "config.json").write_text("{}", encoding="utf-8")
    code, _ = run("migrate", "--from", make_source(folder, data), "--yes")
    assert code == clamqtpie_ctl.EXIT_ERROR


def test_running_service_is_restarted_only_when_asked(legacy, ctl, monkeypatch):
    folder, _ = legacy
    data, run, controls = ctl
    monkeypatch.setattr(clamqtpie_ctl, "service_running", lambda *a, **k: True)
    code, _ = run("migrate", "--from", str(folder), "--yes")
    assert code == clamqtpie_ctl.EXIT_ERROR and controls == []
    code, _ = run("migrate", "--from", str(folder), "--yes", "--restart")
    assert code == 0 and controls == ["stop", "start"]


def test_smtp_password_is_moved_to_the_service_store(legacy, ctl, monkeypatch):
    folder, _ = legacy
    data, run, _ = ctl
    monkeypatch.setattr(portable_migration, "legacy_smtp_password", lambda _config: "pässwörd")
    code, out = run("migrate", "--from", str(folder), "--yes")
    assert code == 0 and "SMTP のパスワードを移しました" in out
    from core.secret_store import FileSecretStore

    service = ConfigService(data / "config.json", secret_store=FileSecretStore(data / "secrets"))
    assert service.get_smtp_password() == "pässwörd"


@pytest.mark.parametrize("service_name", ["ClamQtPie", "ClamAVFrontend"])
def test_legacy_smtp_password_is_read_from_the_old_names(monkeypatch, service_name):
    # 製品名を変える前の旧版は、旧名のサービス名で keyring に保存していた（従来のアプリが起動時に移していた）
    import keyring

    monkeypatch.setattr(portable_migration.sys, "platform", "win32")
    monkeypatch.setattr(keyring, "get_password",
                        lambda name, account: "pässwörd" if (name, account) == (service_name, "smtp_default") else None)
    assert portable_migration.legacy_smtp_password({"smtp": {"keyring_account": "smtp_default"}}) == "pässwörd"


# -- 設定の分割 ------------------------------------------------------------------------------------
def test_service_config_drops_gui_settings(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"schema_version": 1, "general": {"autostart": False}, "clamd": {"tcp_port": 3311}}),
                    encoding="utf-8")
    ConfigService(path).load()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 2 and "general" not in saved and saved["clamd"]["tcp_port"] == 3311


def test_ctl_does_not_handle_gui_settings(ctl):
    data, run, _ = ctl
    code, out = run("config", "show")
    assert code == 0 and "general" not in json.loads(out)
    code, _ = run("config", "set", "general.autostart", "false", "--yes")
    assert code == clamqtpie_ctl.EXIT_ERROR


def test_stopped_service_is_started_again_when_migration_fails(legacy, ctl, monkeypatch):
    folder, _ = legacy
    data, run, controls = ctl
    monkeypatch.setattr(clamqtpie_ctl, "service_running", lambda *a, **k: True)

    def failing(_db, _data):
        raise RuntimeError("db locked")

    monkeypatch.setattr(portable_migration, "insert_rows", failing)
    code, _ = run("migrate", "--from", str(folder), "--yes", "--restart")
    assert code == clamqtpie_ctl.EXIT_ERROR and controls == ["stop", "start"]
    assert not any((data / "quarantine").iterdir())


def test_legacy_logs_do_not_block_the_daily_rollover(legacy, ctl):
    # 旧版のログを今日以降の日付の名前で写すと、サービスが今日の分を夜中に切り替えられなくなるため、昨日の日付にまとめる
    from datetime import date, timedelta

    from core.logger import _OwnerOnlyTimedRotatingFileHandler

    folder, _ = legacy
    data, run, _ = ctl
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    (folder / "logs" / f"app.log.{yesterday}").write_text("rotated\n", encoding="utf-8")
    (folder / "logs" / "app.log.2099-01-01").write_text("future\n", encoding="utf-8")
    (folder / "logs" / "app.log").write_text("today\n", encoding="utf-8")  # 旧版を終了したばかり
    code, out = run("migrate", "--from", str(folder), "--yes")
    assert code == 0, out
    logs = data / "logs"
    assert not (logs / f"app.log.{date.today().isoformat()}").exists()
    assert (logs / f"app.log.{yesterday}").read_text(encoding="utf-8") == "rotated\nfuture\ntoday\n"

    for old in list(logging.getLogger("clamav_frontend").handlers):  # 管理用コマンドが開いたログを閉じる
        logging.getLogger("clamav_frontend").removeHandler(old)
        old.close()
    handler = _OwnerOnlyTimedRotatingFileHandler(logs / "app.log", when="midnight", backupCount=90, encoding="utf-8")
    try:
        handler.emit(logging.makeLogRecord({"msg": "service"}))
        handler.doRollover()
        assert (logs / f"app.log.{date.today().isoformat()}").exists()  # 今日の分を切り替えられる
    finally:
        handler.close()
