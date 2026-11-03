"""段階 12-3d-3 のテスト: 隔離の途中で中断したファイルの片づけ、サービスのクイックスキャンの対象（scan.quick_targets）、
サービスの定義の古さの確認と通知（サービス化・Web API設計書 6.1節・6.2節）"""
from __future__ import annotations

import io
import sys
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import clamqtpie_ctl
from api import actions
from api.service_api import ServiceApi
from core import quarantine_codec
from core.config_model import ConfigModel, validate_and_fix
from core.config_service import ConfigService
from core.exceptions import QuarantineRestoreError
from core.notification_service import NotificationService
from core.quarantine_service import INTERRUPTED_VIRUS_NAME, QuarantineService
from db.database import DatabaseService
from db.models import Quarantine
from tests.conftest import FAKE_INFECTED_CONTENT


# -- 隔離の途中で中断したファイル -------------------------------------------------------------
@pytest.fixture
def quarantine(tmp_path):
    db = DatabaseService(tmp_path / "app.db")
    db.connect()
    yield QuarantineService(db, tmp_path / "quarantine")
    db.close()


def _rows(service):
    with service.db_service.session() as session:
        return [(r.uuid, r.original_path, r.virus_name, r.size) for r in session.query(Quarantine)]


def test_interrupted_restore_leftover_is_removed(quarantine, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text(FAKE_INFECTED_CONTENT)
    row = quarantine.quarantine(src, "Eicar")
    leftover = quarantine.quarantine_dir / f"{row.uuid}.restoring"
    leftover.write_text(FAKE_INFECTED_CONTENT)  # 復元の途中で元に戻した中身
    assert quarantine.recover_interrupted() == [leftover.name]
    assert not leftover.exists() and (quarantine.quarantine_dir / row.uuid).exists()  # 隔離ファイルは残す


def test_interrupted_quarantine_is_encoded_and_recorded(quarantine, tmp_path):
    file_uuid = str(uuid.uuid4())
    moving = quarantine.quarantine_dir / f"{file_uuid}.moving"
    moving.write_bytes(FAKE_INFECTED_CONTENT.encode())  # 元の場所から移した中身（まだ記録が無い）
    assert quarantine.recover_interrupted() == [moving.name]
    assert not moving.exists()
    encoded = quarantine.quarantine_dir / file_uuid
    assert quarantine_codec.read_header(encoded) is not None  # 元の内容のまま置かない
    assert _rows(quarantine) == [(file_uuid, "", INTERRUPTED_VIRUS_NAME, len(FAKE_INFECTED_CONTENT))]

    # 元の場所が分からないため、復元先を指定したときだけ戻せる
    row_id = quarantine.list_all()[0].id
    with pytest.raises(QuarantineRestoreError):
        quarantine.restore(row_id)
    target = tmp_path / "restored.txt"
    quarantine.restore(row_id, dest_path=target)
    assert target.read_text() == FAKE_INFECTED_CONTENT


def test_interrupted_after_recording_only_removes_the_leftover(quarantine, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text(FAKE_INFECTED_CONTENT)
    row = quarantine.quarantine(src, "Eicar")
    moving = quarantine.quarantine_dir / f"{row.uuid}.moving"
    moving.write_text(FAKE_INFECTED_CONTENT)
    quarantine.recover_interrupted()
    assert not moving.exists() and len(_rows(quarantine)) == 1


def test_unrelated_files_are_left_alone(quarantine, tmp_path):
    odd = quarantine.quarantine_dir / "notes.moving"  # 名前の形が違う
    odd.write_text("x")
    folder = quarantine.quarantine_dir / f"{uuid.uuid4()}.moving"
    folder.mkdir()  # 通常のファイルではない
    assert quarantine.recover_interrupted() == []
    assert odd.exists() and folder.exists() and _rows(quarantine) == []


@pytest.mark.skipif(sys.platform == "win32", reason="シンボリックリンクの作成に権限が要る")
def test_link_leftover_is_not_followed(quarantine, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    (quarantine.quarantine_dir / f"{uuid.uuid4()}.restoring").symlink_to(outside)
    assert quarantine.recover_interrupted() == []
    assert outside.read_text() == "keep"


# -- scan.quick_targets -------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["/data", ["relative/path"], [""], [1], ["a\nb"], ["/x"] * 101])
def test_invalid_quick_targets_are_reset(value):
    model = ConfigModel.from_dict({"scan": {"quick_targets": value}})
    validate_and_fix(model, lambda _m: None)
    assert model.scan.quick_targets == []


def test_valid_quick_targets_are_kept(tmp_path):
    model = ConfigModel.from_dict({"scan": {"quick_targets": [str(tmp_path)]}})
    validate_and_fix(model, lambda _m: None)
    assert model.scan.quick_targets == [str(tmp_path)]


def _ctx(tmp_path, quick_targets, service_mode=True):
    config = ConfigModel()
    config.scan.quick_targets = quick_targets
    return SimpleNamespace(service_mode=service_mode, config_service=SimpleNamespace(current=config))


def test_service_quick_scan_uses_only_the_configured_places(tmp_path, monkeypatch):
    monkeypatch.setattr("core.scan_targets.quick_scan_targets", lambda: pytest.fail("サービスでは使わない"))
    missing = str(tmp_path / "missing")
    paths, _ = actions._scan_targets(_ctx(tmp_path, [str(tmp_path), missing]), "quick", None)
    assert paths == [str(tmp_path)]  # 無い場所は除く
    with pytest.raises(ValueError, match="scan.quick_targets"):
        actions._scan_targets(_ctx(tmp_path, []), "quick", None)


def test_desktop_app_quick_scan_keeps_the_user_folders(tmp_path, monkeypatch):
    monkeypatch.setattr("core.scan_targets.quick_scan_targets", lambda: ["/home/u/Downloads"])
    paths, _ = actions._scan_targets(_ctx(tmp_path, [], service_mode=False), "quick", None)
    assert paths == ["/home/u/Downloads"]


def test_quick_targets_are_checked_when_saved_through_the_api(tmp_path):
    assert actions._checked_value("scan.quick_targets", [], [str(tmp_path)]) == [str(tmp_path)]
    with pytest.raises(Exception):
        actions._checked_value("scan.quick_targets", [], str(tmp_path))
    with pytest.raises(Exception):
        actions._checked_value("scan.quick_targets", [], ["relative"])


def test_ctl_sets_quick_targets_from_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(clamqtpie_ctl, "service_running", lambda *a, **k: False)
    data = tmp_path / "svc"
    data.mkdir()
    target = str(tmp_path)

    def run(*args):
        out = io.StringIO()
        code = clamqtpie_ctl.main(["--data-dir", str(data), *args], out=out, ask=lambda _p: "y")
        return code, out.getvalue()

    code, _ = run("config", "set", "scan.quick_targets", f'["{target.replace(chr(92), chr(92) * 2)}"]', "--yes")
    assert code == 0
    assert ConfigService(data / "config.json").load().scan.quick_targets == [target]
    code, _ = run("config", "set", "scan.quick_targets", "/not/json")
    assert code != 0 and "JSON" in capsys.readouterr().err
    code, _ = run("config", "set", "scan.quick_targets", '["relative"]')
    assert code != 0


# -- 定義の古さ（サービス） ------------------------------------------------------------------
def _db_date(days_ago: int) -> str:
    # clamd が返す形（曜日・月は英語）。strftime の %a・%b は locale で変わる（先に動く Qt のテストで ja_JP になる）
    when = datetime.now() - timedelta(days=days_ago)
    day = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[when.weekday()]
    month = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")[when.month - 1]
    return f"{day} {month} {when:%d %H:%M:%S %Y}"


def _service_api(age_days, reachable=True, stale_days=3):
    notified = []
    state = {"date": _db_date(age_days), "reachable": reachable}
    config = ConfigModel()
    config.update.stale_days = stale_days
    runtime = SimpleNamespace(
        clamav_service=SimpleNamespace(ping=lambda retry=False: state["reachable"],
                                       get_version=lambda: ("ClamAV 1.5", "1", state["date"])),
        notification_service=SimpleNamespace(
            notify_definitions_stale=lambda age, mail=False: notified.append((age, mail))),
        config_service=SimpleNamespace(current=config),
        realtime_manager=SimpleNamespace(is_running=False),
    )
    api = ServiceApi.__new__(ServiceApi)
    api.runtime = runtime
    api._clamd_running = None
    api._definitions_age = None
    api._stale_notified = False
    published = []
    api._status = SimpleNamespace(on_status=published.append)
    return api, state, notified, published


def test_stale_definitions_are_reported_once():
    api, state, notified, published = _service_api(age_days=5)
    api.check_clamd()
    api.check_clamd()
    assert notified == [(5, True)]  # 古くなった時点で1回、メールでも
    api._publish_status()
    assert published[-1].definitions_age_days == 5 and published[-1].definitions_stale is True

    state["date"] = _db_date(0)  # 更新された
    api.check_clamd()
    state["date"] = _db_date(4)  # また古くなった
    api.check_clamd()
    assert len(notified) == 2


def test_unreachable_clamd_does_not_change_the_judgement():
    api, state, notified, published = _service_api(age_days=5)
    api.check_clamd()
    state["reachable"] = False
    api.check_clamd()
    state["reachable"] = True
    api.check_clamd()
    assert len(notified) == 1
    state["reachable"] = False
    api.check_clamd()
    api._publish_status()
    assert published[-1].definitions_age_days is None and published[-1].definitions_stale is False


def test_fresh_definitions_are_not_reported():
    api, _, notified, published = _service_api(age_days=1)
    api.check_clamd()
    api._publish_status()
    assert notified == [] and published[-1].definitions_stale is False


def test_stale_mail_is_sent_only_when_asked(tmp_path, monkeypatch):
    config_service = ConfigService(tmp_path / "config.json")
    model = config_service.load()
    model.notification.on_detect_mail = True
    model.smtp.host, model.smtp.from_address, model.smtp.to_address = "smtp.example.com", "a@example.com", "b@example.com"
    config_service.save(model)
    monkeypatch.setattr(config_service, "get_smtp_password", lambda: None)
    sent = []
    service = NotificationService(config_service, desktop_sink=lambda *a: None)
    monkeypatch.setattr(service, "_smtp_send", lambda smtp, password, subject, body: sent.append(subject))
    service.notify_definitions_stale(5)
    assert sent == []  # 従来のアプリはメールを送らない
    service.notify_definitions_stale(5, mail=True)
    assert len(sent) == 1 and "ウイルス定義が古くなっています" in sent[0]
