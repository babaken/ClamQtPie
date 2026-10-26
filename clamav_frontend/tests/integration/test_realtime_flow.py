"""
テスト仕様書3.2節: RT-I01, RT-I02 に対応
実際のwatchdog Observerを使い、ファイル作成が検知・隔離まで到達することを確認する。
"""
import time

from core.clamav_service import ClamAVService
from core.config_model import ClamdConfig, RealtimeConfig
from core.exclude_service import ExcludeService
from core.quarantine_service import QuarantineService
from core.realtime_manager import RealtimeManager
from core.watch_target_service import WatchTargetService
from db.database import DatabaseService
from tests.conftest import FAKE_INFECTED_CONTENT


def test_realtime_detection_reaches_quarantine(fake_clamd_server, tmp_path, qtbot):
    # RT-I01: 実際にファイルを作成・変更し、検知〜隔離まで到達することを確認
    watch_dir = tmp_path / "watched"
    watch_dir.mkdir()
    eicar_path = watch_dir / "eicar.txt"

    fake_clamd_server.response_map["SCAN"] = f"{eicar_path}: Eicar-Test-Signature FOUND"

    db_service = DatabaseService(tmp_path / "app.db")
    db_service.connect()

    clamd_config = ClamdConfig(
        connection_type="tcp", tcp_host=fake_clamd_server.host, tcp_port=fake_clamd_server.port
    )
    clamav_service = ClamAVService(clamd_config)
    watch_target_service = WatchTargetService(db_service)
    exclude_service = ExcludeService(db_service)
    quarantine_service = QuarantineService(db_service, tmp_path / "quarantine")
    realtime_config = RealtimeConfig(windows_debounce_sec=0.2, scan_worker_threads=1)

    manager = RealtimeManager(
        clamav_service, watch_target_service, exclude_service, quarantine_service, realtime_config
    )

    detected_events = []
    manager.detected.connect(lambda path, virus: detected_events.append((path, virus)))

    watch_target_service.add(str(watch_dir))
    assert manager.start() is True

    with qtbot.waitSignal(manager.detected, timeout=5000) as blocker:
        # 判定はfake_clamd_serverの応答で決まるため中身はダミー（EICARは常駐AVに消される）
        eicar_path.write_text(FAKE_INFECTED_CONTENT)

    manager.stop()

    assert blocker.args[1] == "Eicar-Test-Signature"

    # default_action="quarantine"（既定値）のため、隔離まで到達しているはず
    records = quarantine_service.list_all()
    assert len(records) == 1
    assert not eicar_path.exists()

    db_service.close()


def test_watch_target_removed_folder_disables_target(fake_clamd_server, tmp_path, qtbot):
    # RT-I02: 監視対象パス削除時にwatch_target.enabledが0になることを確認
    watch_dir = tmp_path / "to_delete"
    watch_dir.mkdir()

    db_service = DatabaseService(tmp_path / "app.db")
    db_service.connect()

    clamd_config = ClamdConfig(
        connection_type="tcp", tcp_host=fake_clamd_server.host, tcp_port=fake_clamd_server.port
    )
    clamav_service = ClamAVService(clamd_config)
    watch_target_service = WatchTargetService(db_service)
    exclude_service = ExcludeService(db_service)
    quarantine_service = QuarantineService(db_service, tmp_path / "quarantine")
    realtime_config = RealtimeConfig(windows_debounce_sec=0.2, scan_worker_threads=1)

    manager = RealtimeManager(
        clamav_service, watch_target_service, exclude_service, quarantine_service, realtime_config
    )

    lost_events = []
    manager.watch_target_lost.connect(lambda path: lost_events.append(path))

    record = watch_target_service.add(str(watch_dir))

    # 監視開始前にフォルダ自体を削除しておく（スケジュール時に消失を検知する経路を確認）
    import shutil

    shutil.rmtree(watch_dir)

    manager.start()
    manager.stop()

    assert lost_events == [str(watch_dir)]
    targets = watch_target_service.list_all()
    assert targets[0].id == record.id
    assert targets[0].enabled is False

    db_service.close()
