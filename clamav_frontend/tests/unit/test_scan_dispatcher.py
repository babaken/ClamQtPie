"""
ScanDispatcher / ScanWorker のユニットテスト

ScanWorker.run()は同期的に直接呼び出して検証する（QThread経由の結合的な
挙動はtest_scan_dispatcher_threaded.pyで別途qtbotのwaitSignalを使い確認する）。
"""
from core.clamav_service import ClamAVService
from core.config_model import ClamdConfig
from core.exclude_service import ExcludeService
from core.scan_dispatcher import ScanWorker
from db.database import DatabaseService


def _service_for(server, **overrides) -> ClamAVService:
    config = ClamdConfig(connection_type="tcp", tcp_host=server.host, tcp_port=server.port, **overrides)
    return ClamAVService(config)


def test_scan_single_file_emits_expected_signals(fake_clamd_server, tmp_path, qtbot):
    file_path = tmp_path / "a.txt"
    file_path.write_text("hello")
    fake_clamd_server.response_map["SCAN"] = f"{file_path}: OK"

    service = _service_for(fake_clamd_server)
    worker = ScanWorker(service, [str(file_path)])

    progress_calls = []
    file_scanned_calls = []
    finished_calls = []

    worker.progress.connect(lambda a, b, c: progress_calls.append((a, b, c)))
    worker.file_scanned.connect(lambda a, b, c, d: file_scanned_calls.append((a, b, c, d)))
    worker.finished.connect(lambda summary: finished_calls.append(summary))

    worker.run()

    # 先頭は対象列挙中の不確定表示（total=0）、続いてファイルごとの進捗
    assert progress_calls == [(0, 0, "スキャン対象のファイルを確認しています..."), (0, 1, str(file_path))]
    assert len(file_scanned_calls) == 1
    assert file_scanned_calls[0][1] == "OK"
    assert finished_calls[0] == {"scanned": 1, "detected": 0, "errors": 0, "cancelled": False}


def test_scan_directory_enumerates_recursively(fake_clamd_server, tmp_path, qtbot):
    (tmp_path / "sub").mkdir()
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "sub" / "b.txt"
    f1.write_text("1")
    f2.write_text("2")

    # サーバは常にOKを返す想定（応答内のパスの厳密一致はクライアント側では
    # 要求しない実装のため、固定応答でも成立する）
    fake_clamd_server.response_map["SCAN"] = "dummy: OK"

    service = _service_for(fake_clamd_server)
    worker = ScanWorker(service, [str(tmp_path)], include_subfolders=True)

    file_scanned_calls = []
    worker.file_scanned.connect(lambda *args: file_scanned_calls.append(args))

    worker.run()

    assert len(file_scanned_calls) == 2


def test_scan_excludes_files_via_exclude_service(fake_clamd_server, tmp_path):
    scan_dir = tmp_path / "scan_target"
    scan_dir.mkdir()
    f1 = scan_dir / "a.txt"
    f2 = scan_dir / "b.log"
    f1.write_text("1")
    f2.write_text("2")

    fake_clamd_server.response_map["SCAN"] = "dummy: OK"

    db_service = DatabaseService(tmp_path / "dbdir" / "app.db")
    db_service.connect()
    exclude_service = ExcludeService(db_service)
    exclude_service.add("extension", ".log")

    service = _service_for(fake_clamd_server)
    worker = ScanWorker(
        service,
        [str(scan_dir)],
        apply_exclude=True,
        exclude_service=exclude_service,
    )

    file_scanned_calls = []
    worker.file_scanned.connect(lambda *args: file_scanned_calls.append(args))
    worker.run()

    assert len(file_scanned_calls) == 1  # b.logは除外される
    db_service.close()


def test_scan_stop_flag_halts_before_completion(fake_clamd_server, tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text(str(i))

    fake_clamd_server.response_map["SCAN"] = "dummy: OK"

    service = _service_for(fake_clamd_server)
    worker = ScanWorker(service, [str(tmp_path)])

    scanned_paths = []

    def on_progress(scanned, total, path):
        scanned_paths.append(path)
        if len(scanned_paths) >= 2:
            worker.stop()

    worker.progress.connect(on_progress)

    finished_summary = {}
    worker.finished.connect(lambda s: finished_summary.update(s))

    worker.run()

    assert finished_summary["cancelled"] is True
    assert finished_summary["scanned"] < 5


def test_skip_dirs_are_not_enumerated(fake_clamd_server, tmp_path):
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc" / "status").write_text("x")
    (tmp_path / "keep.txt").write_text("x")
    fake_clamd_server.response_map["SCAN"] = "dummy: OK"

    worker = ScanWorker(_service_for(fake_clamd_server), [str(tmp_path)], skip_dirs=(str(tmp_path / "proc"),))
    scanned = []
    worker.progress.connect(lambda a, b, path: scanned.append(path))
    worker.run()

    assert str(tmp_path / "keep.txt") in scanned
    assert str(tmp_path / "proc" / "status") not in scanned


def test_clamd_unreachable_aborts_scan_with_single_error(tmp_path, caplog):
    from core.clamav_service import ClamAVService
    from core.config_model import ClamdConfig

    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text(str(i))
    service = ClamAVService(ClamdConfig(connection_type="tcp", tcp_host="127.0.0.1", tcp_port=1))
    worker = ScanWorker(service, [str(tmp_path)])
    errors, summary = [], {}
    worker.error.connect(errors.append)
    worker.finished.connect(summary.update)

    worker.run()

    assert len(errors) == 1  # ファイル数分のエラーダイアログを出さない
    assert summary["cancelled"] is True
    # 中断の理由をログに残す（サービスではジョブ・履歴に要約しか残らない）
    assert "を中断します: ClamAVサービス（clamd）に接続できません" in caplog.text


# -- 応答待ち・待機中の表示と停止操作による打ち切り（プロトコル設計書4章・5章） ------------------
import threading as _threading  # noqa: E402
import time as _time  # noqa: E402

from PySide6.QtCore import Qt as _Qt  # noqa: E402


def _run_in_thread(worker):
    events, summary = [], {}
    direct = _Qt.ConnectionType.DirectConnection
    worker.activity.connect(lambda s, p, e: events.append((s, p, e)), direct)
    worker.finished.connect(summary.update, direct)
    errors = []
    worker.error.connect(errors.append, direct)
    thread = _threading.Thread(target=worker.run, daemon=True)
    thread.start()
    return thread, events, summary, errors


def test_stalled_scan_is_reported_and_stop_aborts_immediately(fake_clamd_server, tmp_path, monkeypatch):
    target = tmp_path / "big.iso"
    target.write_text("x")
    fake_clamd_server.response_map["SCAN"] = f"{target}: OK"
    fake_clamd_server.delay_sec = 30  # clamdが応答しない状態
    monkeypatch.setattr(ScanWorker, "STALL_NOTICE_SEC", 0.3)
    monkeypatch.setattr(ScanWorker, "_MONITOR_INTERVAL_SEC", 0.1)
    worker = ScanWorker(_service_for(fake_clamd_server), [str(target)])

    thread, events, summary, errors = _run_in_thread(worker)
    deadline = _time.monotonic() + 5
    while not any(e[0] == "stalled" for e in events) and _time.monotonic() < deadline:
        _time.sleep(0.05)
    stalled = [e for e in events if e[0] == "stalled"]
    assert stalled and stalled[0][1] == str(target)

    started = _time.monotonic()
    worker.stop()  # 応答待ちの通信を打ち切ってすぐに止まる（30秒待たない）
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert _time.monotonic() - started < 3
    assert summary["cancelled"] is True
    assert errors == []  # 停止操作による打ち切りはエラーとして表示しない


def test_queued_while_connection_slots_are_busy(fake_clamd_server, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x")
    fake_clamd_server.response_map["SCAN"] = f"{target}: OK"
    service = _service_for(fake_clamd_server, max_concurrent_connections=2)  # オンデマンドは1接続まで
    service._ondemand_semaphore.acquire()  # 他のオンデマンドスキャンが使用中
    worker = ScanWorker(service, [str(target)])

    thread, events, summary, _ = _run_in_thread(worker)
    deadline = _time.monotonic() + 5
    while not events and _time.monotonic() < deadline:
        _time.sleep(0.05)
    assert events[0][0] == "queued"

    service._ondemand_semaphore.release()
    thread.join(timeout=5)
    assert summary["scanned"] == 1
    assert events[-1][0] == "scanning"  # 待機が終わったら通常表示に戻す


def test_stop_while_queued(fake_clamd_server, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x")
    service = _service_for(fake_clamd_server, max_concurrent_connections=2)
    service._ondemand_semaphore.acquire()
    worker = ScanWorker(service, [str(target)])

    thread, events, summary, _ = _run_in_thread(worker)
    deadline = _time.monotonic() + 5
    while not events and _time.monotonic() < deadline:
        _time.sleep(0.05)
    worker.stop()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert summary["cancelled"] is True
    service._ondemand_semaphore.release()


def test_scan_does_not_keep_the_whole_file_list(fake_clamd_server, tmp_path, monkeypatch):
    # 対象のファイルの一覧をメモリに作らない（フルスキャンで数百万件になってもメモリが増えない）。
    # 数えるときと検査するときに 1 件ずつたどり、検査は数え終わってから始まる
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("x")
    fake_clamd_server.response_map["SCAN"] = "dummy: OK"
    worker = ScanWorker(_service_for(fake_clamd_server), [str(tmp_path)])
    passes = []
    original = ScanWorker._iter_files

    def tracking(self, is_excluded):
        passes.append(type(original(self, is_excluded)).__name__)
        return original(self, is_excluded)

    monkeypatch.setattr(ScanWorker, "_iter_files", tracking)
    progress = []
    worker.progress.connect(lambda a, b, c: progress.append((a, b)))
    worker.run()

    assert passes == ["generator", "generator"]
    assert progress[1:] == [(i, 5) for i in range(5)]


def test_file_created_after_counting_is_scanned_and_total_follows(fake_clamd_server, tmp_path):
    # 数えた後に増えたファイルも検査し、進み具合の分母が検査した数を下回らない
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "sub").mkdir()  # 上のフォルダの後にたどるフォルダ
    fake_clamd_server.response_map["SCAN"] = "dummy: OK"
    worker = ScanWorker(_service_for(fake_clamd_server), [str(tmp_path)])
    progress = []

    def on_progress(scanned, total, _path):
        progress.append((scanned, total))
        if total == 1 and scanned == 0:
            (tmp_path / "sub" / "b.txt").write_text("x")  # 数え終わった後に増える

    worker.progress.connect(on_progress)
    summaries = []
    worker.finished.connect(summaries.append)
    worker.run()

    assert summaries[0]["scanned"] == 2
    assert all(scanned < total for scanned, total in progress[1:])


def test_stop_while_counting_files_ends_without_scanning(fake_clamd_server, tmp_path):
    # 数えている途中で止めても、検査を始めずに終わる
    for i in range(3):
        (tmp_path / f"f{i}.txt").write_text("x")
    fake_clamd_server.response_map["SCAN"] = "dummy: OK"
    worker = ScanWorker(_service_for(fake_clamd_server), [str(tmp_path)])
    worker.progress.connect(lambda scanned, total, _path: worker.stop() if total == 0 else None)
    summaries = []
    worker.finished.connect(summaries.append)
    worker.run()

    assert summaries[0]["scanned"] == 0 and summaries[0]["cancelled"] is True
    assert "SCAN" not in fake_clamd_server.commands


# --- マウントした先のたどり方（README_DEV §96） ---------------------------------------------

import logging as _logging  # noqa: E402
import os as _os  # noqa: E402

import core.scan_dispatcher as _sd  # noqa: E402


def test_parse_mountinfo_reads_mount_points_and_types():
    text = (
        "22 1 8:2 / / rw,relatime shared:1 - ext4 /dev/sda2 rw\n"
        "30 22 0:40 / /mnt/my\\040nas rw shared:5 - nfs4 server:/share rw\n"
        "31 22 8:3 / /home rw - btrfs /dev/sda3 rw\n"
        "32 22 0:41 / /home rw - tmpfs tmpfs rw\n"
        "broken line\n"
    )
    assert _sd.parse_mountinfo(text) == {"/": "ext4", "/mnt/my nas": "nfs4", "/home": "tmpfs"}


def _scan_paths(server, targets):
    server.response_map["SCAN"] = "dummy: OK"
    worker = ScanWorker(_service_for(server), [str(t) for t in targets])
    scanned = []
    worker.file_scanned.connect(lambda path, *_rest: scanned.append(str(path)))
    worker.run()
    return scanned


def test_network_mounts_are_entered_only_when_specified_directly(fake_clamd_server, tmp_path, monkeypatch, caplog):
    # たどる途中ではローカルのディスクのマウントにだけ入る。ネットワークのマウントは、その場所を直接指定したときだけ検査する
    for rel in ("top.txt", "disk/a.txt", "nas/b.txt", "nas/inner/c.txt"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x")
    real = _os.path.realpath(str(tmp_path))
    mounts = {
        _os.path.join(real, "disk"): "ext4",
        _os.path.join(real, "nas"): "nfs4",
        _os.path.join(real, "nas", "inner"): "cifs",
    }
    monkeypatch.setattr(_sd, "_read_mount_types", lambda: dict(mounts))

    with caplog.at_level(_logging.INFO, logger="clamav_frontend"):
        scanned = _scan_paths(fake_clamd_server, [tmp_path])
    assert sorted(scanned) == sorted([str(tmp_path / "top.txt"), str(tmp_path / "disk" / "a.txt")])
    # 数えるときと検査するときの 2 回たどっても、入らなかったログは 1 回だけ
    assert sum("nfs4" in r.getMessage() for r in caplog.records) == 1

    scanned = _scan_paths(fake_clamd_server, [tmp_path / "nas"])
    assert scanned == [str(tmp_path / "nas" / "b.txt")]  # 中に重ねた cifs には入らない


def test_same_folder_seen_from_two_places_is_scanned_once(fake_clamd_server, tmp_path, monkeypatch):
    # バインドマウントなどで同じフォルダが 2 か所に見えても、検査は 1 回
    for rel in ("orig/a.txt", "view/a.txt"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x")
    monkeypatch.setattr(_sd, "_read_mount_types", lambda: {})
    real_stat = _os.stat
    orig = str(tmp_path / "orig")
    view = str(tmp_path / "view")

    def fake_stat(path, *args, **kwargs):
        return real_stat(orig if str(path) == view else path, *args, **kwargs)

    monkeypatch.setattr(_sd.os, "stat", fake_stat)
    assert len(_scan_paths(fake_clamd_server, [tmp_path])) == 1


def test_without_mount_list_folders_are_walked_as_before(fake_clamd_server, tmp_path, monkeypatch):
    # マウントの一覧が無いとき（Windows・読めないとき）は、これまでどおりたどる
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("x")
    monkeypatch.setattr(_sd, "_read_mount_types", lambda: None)
    assert _scan_paths(fake_clamd_server, [tmp_path]) == [str(tmp_path / "sub" / "a.txt")]


def test_stop_while_waiting_does_not_report_unexpected_error(fake_clamd_server, tmp_path, caplog):
    # 応答待ちに停止して通信が打ち切られ、想定外の例外（OSError など）になっても、E9001 として扱わない
    # （ERROR のログ・エラーの数・「想定外のエラー」の結果を残さない。README_DEV §100）
    import logging

    for i in range(3):
        (tmp_path / f"f{i}.txt").write_text(str(i))
    worker = ScanWorker(_service_for(fake_clamd_server), [str(tmp_path)])

    def scan_one(_path):
        worker.stop()
        raise OSError(9, "Bad file descriptor")

    worker._scan_one = scan_one
    scanned, summary = [], {}
    worker.file_scanned.connect(lambda *args: scanned.append(args))
    worker.finished.connect(summary.update)

    with caplog.at_level(logging.ERROR, logger="clamav_frontend"):
        worker.run()

    assert summary["cancelled"] is True
    assert summary["errors"] == 0
    assert scanned == []
    assert not [r for r in caplog.records if getattr(r, "error_code", None) == "E9001"]
