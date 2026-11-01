"""UTF-8 として正しくない名前のファイルも、スキャン・記録できることのテスト"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.clamav_service import ClamAVService
from core.clamd_client import ClamdClient, ClamdPathEncodingError, ScanResult, _encode_command
from core.config_model import ClamdConfig
from core.scan_dispatcher import ScanWorker
from core.scan_history_service import ScanHistoryCollector, ScanHistoryService
from core.text_safety import visible_controls
from db.database import DatabaseService

#: Linux 等の UTF-8 として正しくないファイル名を、Python が表す形（surrogateescape）
UNDECODABLE = "report" + b"\xff".decode("utf-8", errors="surrogateescape") + ".txt"
#: 対になっていないサロゲート（Windows のファイル名に入り得る。どの方法でも UTF-8 にできない）
LONE_SURROGATE = "report\ud800.txt"

requires_posix_bytes_names = pytest.mark.skipif(
    os.name != "posix", reason="UTF-8 として正しくない名前のファイルを作れるのは POSIX だけ"
)


def test_names_are_written_in_a_visible_and_encodable_form():
    for name in (UNDECODABLE, LONE_SURROGATE):
        text = visible_controls(name)
        text.encode("utf-8")  # ログ・JSON・DB に書ける
        assert text.startswith("report\\u")


def test_undecodable_name_is_sent_as_the_original_bytes():
    # clamd が同じファイルを開けるよう、OS のファイル名の元のバイト列のまま送る
    assert _encode_command(f"SCAN /data/{UNDECODABLE}") == b"zSCAN /data/report\xff.txt\0"


def test_name_that_cannot_be_sent_is_reported_before_connecting(monkeypatch):
    client = ClamdClient(ClamdConfig(connection_type="tcp", tcp_host="127.0.0.1", tcp_port=1))
    monkeypatch.setattr(client, "_connect", lambda: pytest.fail("接続しない"))
    with pytest.raises(ClamdPathEncodingError):
        client.scan_path(LONE_SURROGATE)


def test_file_whose_name_cannot_be_sent_is_scanned_by_content(monkeypatch):
    # 名前を送れなくても検査を逃さないよう、内容を送る方法で検査する
    service = ClamAVService(ClamdConfig(connection_type="tcp", tcp_host="127.0.0.1", tcp_port=3310))
    service._use_fdpass = False
    monkeypatch.setattr(service._client, "scan_path",
                        lambda path, multi=False: (_ for _ in ()).throw(ClamdPathEncodingError()))
    streamed = []
    monkeypatch.setattr(service, "_scan_by_stream",
                        lambda path, original: streamed.append(path) or ScanResult(str(path), "FOUND", "Eicar"))
    assert service._scan_file(LONE_SURROGATE)[0].status == "FOUND"
    assert streamed == [LONE_SURROGATE]


@requires_posix_bytes_names
def test_undecodable_file_name_does_not_stop_the_scan(tmp_path, fake_clamd_server):
    # TCP 接続（SCAN でパスを送る）でも、スキャンが最後まで進む
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "normal.txt").write_text("x")
    bad = Path(os.fsdecode(bytes(folder) + b"/report\xff.txt"))
    bad.write_text("x")
    service = ClamAVService(ClamdConfig(connection_type="tcp", tcp_host=fake_clamd_server.host,
                                        tcp_port=fake_clamd_server.port))
    worker = ScanWorker(service, [str(folder)])
    finished = []
    worker.finished.connect(finished.append)
    worker.run()
    assert finished[0]["scanned"] == 2


def test_unexpected_error_for_one_file_does_not_stop_the_scan(tmp_path, qapp):
    # 1ファイルの想定外の失敗でスキャン全体が止まると、完了の合図も出ず、画面が「スキャン中」のまま戻らない
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("x")

    def scan_file(path, **kwargs):
        if Path(path).name == "a.txt":
            raise RuntimeError("想定外")
        return [ScanResult(str(path), "OK", "")]

    worker = ScanWorker(SimpleNamespace(scan_file=scan_file), [str(tmp_path)])
    results, finished = [], []
    worker.file_scanned.connect(lambda path, status, detail, at: results.append((Path(path).name, status)))
    worker.finished.connect(finished.append)
    worker.run()
    assert finished[0]["scanned"] == 2 and finished[0]["errors"] == 1
    assert ("a.txt", "ERROR") in results and ("b.txt", "OK") in results


def test_history_keeps_unusual_names(tmp_path):
    db = DatabaseService(tmp_path / "app.db")
    db.connect()
    try:
        history = ScanHistoryService(db)
        collector = ScanHistoryCollector(source="gui", scan_type="custom", label="カスタムスキャン", targets=["/data"])
        collector.add(f"/data/{UNDECODABLE}", "FOUND", "Eicar")
        collector.add(f"/data/{LONE_SURROGATE}", "ERROR", "Permission denied")
        history_id = history.record(collector, 2, "completed")
        detail = history.get(history_id)
        assert detail["detections"][0]["path"] == visible_controls(f"/data/{UNDECODABLE}")
        assert len(detail["errors"]) == 1
    finally:
        db.close()


# -- Qt のシグナルを通しても名前が変わらないこと ----------------------------------------------
def test_paths_pass_through_signals_unchanged(qapp):
    # Qt の文字列（str のシグナル）では UTF-8 にできない名前の文字が黙って消え、別のファイルの名前になる。
    # パスを渡すシグナルは、名前をそのまま届ける
    from core.realtime_manager import RealtimeManager
    from core.scan_dispatcher import ScanDispatcher

    dispatcher = ScanDispatcher(SimpleNamespace())
    got = []
    dispatcher.file_scanned.connect(lambda path, *_: got.append(path))
    dispatcher.progress.connect(lambda _s, _t, path: got.append(path))
    dispatcher.activity.connect(lambda _state, path, _sec: got.append(path))
    for name in (UNDECODABLE, LONE_SURROGATE):
        dispatcher.file_scanned.emit(name, "FOUND", "Eicar", "")
        dispatcher.progress.emit(1, 2, name)
        dispatcher.activity.emit("stalled", name, 10)
    assert got == [UNDECODABLE] * 3 + [LONE_SURROGATE] * 3

    from core.config_model import RealtimeConfig

    manager = RealtimeManager(SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
                              RealtimeConfig())
    got.clear()
    manager.detected.connect(lambda path, _virus: got.append(path))
    manager.detection_handled.connect(lambda path, _virus, _q: got.append(path))
    manager.watch_target_lost.connect(got.append)
    manager.detected.emit(LONE_SURROGATE, "Eicar")
    manager.detection_handled.emit(LONE_SURROGATE, "Eicar", False)
    manager.watch_target_lost.emit(UNDECODABLE)
    assert got == [LONE_SURROGATE, LONE_SURROGATE, UNDECODABLE]


def test_scan_screen_quarantines_the_scanned_file_itself(qtbot, monkeypatch):
    # 一覧の表示は見える形にするが、隔離・無視には検査したファイルのパスそのものを使う
    # （表示の文字で隔離すると、同じに見える別のファイルを隔離してしまう）
    from unittest.mock import MagicMock

    import gui.screens.scan as scan_module
    from gui.screens.scan import ScanScreen

    screen = ScanScreen()
    qtbot.addWidget(screen)
    quarantine_service = MagicMock()
    screen.bind_services(MagicMock(), quarantine_service)
    path = f"/data/eicar{UNDECODABLE}"
    screen._on_file_scanned(path, "FOUND", "Eicar", "")
    assert screen.result_table.item(0, 0).text() == visible_controls(path)
    screen.result_table.selectRow(0)
    monkeypatch.setattr(scan_module, "show_confirm_dialog", lambda *a, **k: True)
    screen.quarantine_button.click()
    quarantine_service.quarantine.assert_called_once_with(path, "Eicar")
